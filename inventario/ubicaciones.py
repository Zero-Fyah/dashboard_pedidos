"""
ubicaciones.py — Línea SKU-posición: la unidad de conteo (DEC-057).

Hasta DEC-051 el cruce de inventario agregaba Bochica **por referencia** y
tiraba el detalle por ubicación. Ese detalle es justamente la unidad de
trabajo del plan de inventario del área: no se cuenta "una referencia", se
cuenta *un ID dentro de una posición concreta* — una línea SKU-posición.

Este módulo la reconstruye y la enriquece con lo que el dashboard ya sabe y
el conteo físico no: qué tan crítico es ese producto (ABC global), qué tan
predecible es su demanda (XYZ), cuánto vale lo que hay en esa posición y
hace cuánto que la referencia no sale.

**Lo que este módulo NO hace, a propósito:** no calcula exactitud de
inventario (IRA) ni estados de madurez de la ubicación. Ambos exigen
*eventos de conteo físico* — quién contó, cuándo y qué encontró — que hoy
no entran al pipeline: el scheduler solo trae el catálogo del admin y el
inventario de Bochica. Fabricar un IRA sin conteos sería inventar el dato
que el plan justamente pretende construir. Lo que sí se puede dar, y es lo
que se da, es **la priorización del trabajo de conteo y la verificación de
los supuestos de dimensionamiento**.
"""

import logging

import pandas as pd

from comun import familia_de

logger = logging.getLogger("inventario.ubicaciones")

TIPO_ALTURA = "Altura"
TIPO_PICKING = "Picking"

SIN_ROTACION = "Sin rotación"

# Orden de prioridad de conteo. `Sin rotación` va al final por clase, pero
# no se ignora: dentro de su bloque manda el valor, que es lo que rescata
# la posición de bajo movimiento y alto valor.
ORDEN_CLASE = ("A", "B", "C", SIN_ROTACION)

_COLUMNAS = [
    "ubicacion",
    "tipo",
    "rack",
    "posicion",
    "nivel",
    "id_especificacion",
    "referencia",
    "familia",
    "cantidad",
    "clase",
    "xyz",
    "clase_posicion",
    "precio_unitario",
    "valor_linea",
    "dias_sin_salida",
    "prioridad",
]


def calcular_ubicaciones(
    bochica: pd.DataFrame,
    abc: pd.DataFrame,
    df_admin: pd.DataFrame,
    salud: pd.DataFrame,
) -> pd.DataFrame:
    """Construye las líneas SKU-posición con su prioridad de conteo.

    Args:
        bochica: Bochica ya clasificado por layout
            (`layout.clasificar_ubicaciones` + `solo_layout`), con `tipo`,
            `rack`, `posicion` y `altura`.
        abc: Resultado de `clasificacion.calcular_clasificacion()`. Se usa
            **el nivel `id_global`**, no el jerárquico: para decidir qué
            contar primero hace falta comparar cada ID contra todo el
            almacén, no contra los de su propia referencia. El jerárquico
            sigue sirviendo para slotting, que es otra pregunta.
        df_admin: Catálogo del admin filtrado por alcance, por el `precio`.
        salud: Resultado de `salud.calcular_salud()`, por `dias_sin_salida`.

    Returns:
        Una fila por `(ubicacion, id_especificacion)`, ordenada por
        prioridad de conteo (1 = contar primero).
    """
    if bochica.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    lineas = bochica.groupby(
        ["ubicacion", "id_especificacion", "referencia", "tipo", "rack", "posicion", "altura"],
        as_index=False,
    )["cantidad"].sum()
    lineas = lineas.rename(columns={"altura": "nivel"})
    lineas["id_especificacion"] = lineas["id_especificacion"].astype(str)
    lineas["familia"] = lineas["referencia"].map(familia_de)

    # ── Clase global del ID ──────────────────────────────────────────
    globales = abc[abc["nivel"] == "id_global"][["clave", "abc", "xyz"]].copy()
    globales["clave"] = globales["clave"].astype(str)
    lineas = lineas.merge(
        globales.rename(columns={"clave": "id_especificacion", "abc": "clase"}),
        on="id_especificacion",
        how="left",
    )
    # Un ID sin ventas en la ventana no aparece en el ABC. No es clase C:
    # es "la operación no lo va a tocar sola", que es precisamente el caso
    # que el barrido dirigido existe para cubrir.
    lineas["clase"] = lineas["clase"].fillna(SIN_ROTACION)

    # ── Valor de lo que hay en la posición ───────────────────────────
    precios = df_admin[["id_especificacion", "precio"]].copy()
    precios["id_especificacion"] = precios["id_especificacion"].astype(str)
    precios["precio"] = pd.to_numeric(precios["precio"], errors="coerce")
    precios = precios.groupby("id_especificacion", as_index=False)["precio"].max()
    lineas = lineas.merge(
        precios.rename(columns={"precio": "precio_unitario"}),
        on="id_especificacion",
        how="left",
    )
    lineas["valor_linea"] = lineas["cantidad"] * lineas["precio_unitario"].fillna(0.0)

    # ── Antigüedad, con el nombre honesto ────────────────────────────
    # No es "días desde el último conteo" —eso vive en el registro físico,
    # fuera de este pipeline—: es días sin salida de la referencia. Sirve
    # como señal de "la operación no ha pasado por acá", que es para lo que
    # el plan la usa.
    if not salud.empty:
        lineas = lineas.merge(
            salud[["referencia", "dias_sin_salida"]].drop_duplicates("referencia"),
            on="referencia",
            how="left",
        )
    else:
        lineas["dias_sin_salida"] = pd.NA

    # ── Clase de la posición: la más alta presente ───────────────────
    # Regla del plan de inventario: una sola línea A eleva toda la posición
    # a prioridad A, porque la visita física es a la posición, no a la línea.
    rango = {clase: i for i, clase in enumerate(ORDEN_CLASE)}
    lineas["_rango"] = lineas["clase"].map(rango).fillna(len(ORDEN_CLASE)).astype(int)
    por_posicion = lineas.groupby("ubicacion")["_rango"].min()
    lineas["clase_posicion"] = (
        lineas["ubicacion"].map(por_posicion).map(dict(enumerate(ORDEN_CLASE)))
    )

    # ── Prioridad ────────────────────────────────────────────────────
    # Deliberadamente NO es un score con pesos: la clase manda en bloque y
    # dentro del bloque manda el valor. Unos pesos inventados (0,5 clase +
    # 0,35 valor + …) darían un número con más precisión aparente que
    # fundamento, y nadie podría explicar por qué una posición quedó sobre
    # otra. Así el orden se explica en una frase.
    #
    # **La unidad de ordenamiento es la posición, no la línea.** Ordenar por
    # valor de línea suelto intercala líneas de posiciones distintas, y quien
    # sube a verificar una estiba cuenta de una vez todo lo que hay en ella:
    # dos visitas a la misma posición son un desperdicio que el plan señala
    # explícitamente. Por eso el valor que ordena es el de la posición
    # completa, y dentro de ella las líneas van de mayor a menor.
    lineas["_rango_pos"] = lineas["clase_posicion"].map(rango).fillna(len(ORDEN_CLASE)).astype(int)
    lineas["_valor_pos"] = lineas["ubicacion"].map(lineas.groupby("ubicacion")["valor_linea"].sum())
    lineas = lineas.sort_values(
        ["_rango_pos", "_valor_pos", "ubicacion", "valor_linea"],
        ascending=[True, False, True, False],
    ).reset_index(drop=True)
    lineas["prioridad"] = lineas.index + 1

    total = len(lineas)
    altura = lineas[lineas["tipo"] == TIPO_ALTURA]
    logger.info(
        "calcular_ubicaciones: %d líneas SKU-posición (%d en altura sobre %d posiciones) · "
        "A+B %.1f%% de altura · sin rotación %.1f%% · valorizadas %.1f%%",
        total,
        len(altura),
        altura["ubicacion"].nunique(),
        altura["clase"].isin(["A", "B"]).mean() * 100 if len(altura) else 0,
        (altura["clase"] == SIN_ROTACION).mean() * 100 if len(altura) else 0,
        lineas["precio_unitario"].notna().mean() * 100,
    )
    return lineas[_COLUMNAS]


def resumen_cobertura(lineas: pd.DataFrame, layout: pd.DataFrame) -> pd.DataFrame:
    """Universo de posiciones contra lo que realmente tiene inventario.

    Es la verificación que corrige el error de dimensionamiento más caro
    del plan: estimar el trabajo como *posiciones totales × densidad media*
    cuenta las posiciones vacías como si hubiera que contarlas.

    Args:
        lineas: Resultado de `calcular_ubicaciones()`.
        layout: Resultado de `layout.cargar_layout()`.

    Returns:
        Una fila por tipo de ubicación, con posiciones del layout, cuántas
        están ocupadas y cuántas líneas SKU-posición hay realmente.
    """
    activas = layout[layout["activa"].astype(str).str.strip().str.upper().isin({"SI", "SÍ"})]
    filas = []
    for tipo in (TIPO_ALTURA, TIPO_PICKING):
        del_tipo = lineas[lineas["tipo"] == tipo]
        ocupadas = del_tipo["ubicacion"].nunique()
        posiciones = int((activas["tipo"] == tipo).sum())
        filas.append(
            {
                "tipo": tipo,
                "posiciones_activas": posiciones,
                "posiciones_ocupadas": ocupadas,
                "posiciones_vacias": posiciones - ocupadas,
                "lineas": len(del_tipo),
                "lineas_por_ocupada": round(len(del_tipo) / ocupadas, 2) if ocupadas else 0.0,
                "valor": float(del_tipo["valor_linea"].sum()),
            }
        )
    return pd.DataFrame(filas)
