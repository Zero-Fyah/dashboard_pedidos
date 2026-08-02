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

import datetime as dt
import logging

import pandas as pd

from comun import (
    SUFIJO_CLASE_HEREDADA,
    VIGENCIA_ACTIVO,
    VIGENCIA_DESCONTINUADO,
    familia_de,
)
from inventario.normalizador import ADMIN_ACTIVO, ADMIN_DESCONTINUADO

logger = logging.getLogger("inventario.ubicaciones")

TIPO_ALTURA = "Altura"
TIPO_PICKING = "Picking"

SIN_ROTACION = "Sin rotación"

# Orden de prioridad de conteo. `Sin rotación` va al final por clase, pero
# no se ignora: dentro de su bloque manda el valor, que es lo que rescata
# la posición de bajo movimiento y alto valor.
ORDEN_CLASE = ("A", "B", "C", SIN_ROTACION)

# Sufijo para la clase heredada de la referencia. Un ID que no está en el
# puente de atribución (DEC-053) es **invisible** para el ABC por ID, y eso
# no es lo mismo que no venderse: medido, el 92,3% de los ID que caían en
# "Sin rotación" simplemente no eran atribuibles, y el 57,9% de esas líneas
# pertenecía a referencias con ventas en los últimos 90 días. Tratarlos como
# producto muerto los mandaba al fondo de la cola sin evidencia.
#
# La referencia sí se puede clasificar siempre (no necesita el puente), así
# que se hereda su clase y se marca el origen para no fingir precisión que
# no hay. La causa de fondo es el código de barras no único por ID, que ya
# está en Tareas como hallazgo (DEC-047).
#
# DEC-067: el sufijo se importa de `comun/`. El dashboard decide con él qué
# entra a la cola de conteo, así que no puede vivir solo acá.
SUFIJO_HEREDADA = SUFIJO_CLASE_HEREDADA

# Orden físico de los racks en el plano de bodega (DEC-078). Los pares
# consecutivos comparten pasillo: A|B, C|D, … O|P — verificado contra las
# columnas de la hoja `Plano Layout`, donde cada `PASILLO` cae exactamente
# entre el fin de un rack y el comienzo del siguiente.
RACKS_EN_PLANO = "ABCDEFGHIJKLMNOP"

# Un rack que el plano no dibuja (`PU`, o uno nuevo) va al final del
# recorrido: mejor visible y último que fuera de orden o desaparecido.
_PASILLO_DESCONOCIDO = 99

_COLUMNAS = [
    "ubicacion",
    "tipo",
    "rack",
    "posicion",
    "nivel",
    "id_especificacion",
    "referencia",
    "familia",
    # DEC-072: 1 si el ID está en el catálogo del admin. El alcance de
    # trabajo son esos; el resto se ve pero no se cuenta.
    "en_catalogo",
    "cantidad",
    "clase",
    "origen_clase",
    "xyz",
    "clase_posicion",
    "precio_unitario",
    "valor_linea",
    "dias_sin_salida",
    "prioridad",
    # DEC-078: orden de visita física, derivado del plano.
    "orden_recorrido",
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

    # ── Respaldo por referencia, para los ID no atribuibles ──────────
    # El nivel de referencia no depende del puente, así que cubre a los ID
    # que el ABC por ID no puede ver. Se marca de dónde salió la clase.
    refs = abc[abc["nivel"] == "referencia"][["clave", "abc"]].copy()
    refs["clave"] = refs["clave"].astype(str)
    lineas = lineas.merge(
        refs.rename(columns={"clave": "referencia", "abc": "_clase_ref"}),
        on="referencia",
        how="left",
    )
    sin_clase_propia = lineas["clase"].isna()
    heredable = sin_clase_propia & lineas["_clase_ref"].isin(["A", "B", "C"])
    lineas.loc[heredable, "clase"] = lineas.loc[heredable, "_clase_ref"] + SUFIJO_HEREDADA
    # Lo que queda sin clase por ningún camino sí es producto sin rotación:
    # ni el ID ni su referencia vendieron. Es el caso que el barrido
    # dirigido existe para cubrir, porque la operación no lo va a tocar sola.
    lineas["clase"] = lineas["clase"].fillna(SIN_ROTACION)
    lineas["origen_clase"] = "ID"
    lineas.loc[heredable, "origen_clase"] = "Referencia"
    lineas.loc[lineas["clase"] == SIN_ROTACION, "origen_clase"] = "Sin ventas"
    lineas = lineas.drop(columns="_clase_ref")

    # ── ¿El catálogo del admin reconoce este ID? (DEC-072) ───────────
    # El alcance de trabajo son los ID que publica `admin_inventario`. Lo
    # que Bochica reporta bajo un ID que el catálogo no tiene existe
    # físicamente y ocupa espacio, pero **no se puede contar**: nadie sabe
    # qué producto es, ni qué clase tiene, ni cuánto vale.
    #
    # Se marca en vez de descartarse. Borrarlo dejaría 387 posiciones
    # figurando vacías cuando están ocupadas, y el mapa de bodega mentiría
    # sobre la utilización del espacio.
    ids_catalogo = set(df_admin["id_especificacion"].astype(str))
    lineas["en_catalogo"] = lineas["id_especificacion"].isin(ids_catalogo).astype(int)
    fuera = lineas["en_catalogo"] == 0
    if fuera.any():
        logger.info(
            "calcular_ubicaciones: %d de %d líneas con ID fuera del catálogo admin "
            "(%d ID · %d posiciones · %.0f unidades) — quedan marcadas, no se descartan",
            int(fuera.sum()),
            len(lineas),
            lineas.loc[fuera, "id_especificacion"].nunique(),
            lineas.loc[fuera, "ubicacion"].nunique(),
            float(lineas.loc[fuera, "cantidad"].sum()),
        )

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
    #
    # La clase heredada va justo detrás de la propia: un "A por referencia"
    # sigue siendo prioritario, pero con menos certeza que un A confirmado,
    # así que no le gana el turno.
    rango = {}
    for base in ("A", "B", "C"):
        rango[base] = len(rango)
        rango[base + SUFIJO_HEREDADA] = len(rango)
    rango[SIN_ROTACION] = len(rango)
    orden_clases = sorted(rango, key=rango.get)
    lineas["_rango"] = lineas["clase"].map(rango).fillna(len(rango)).astype(int)
    por_posicion = lineas.groupby("ubicacion")["_rango"].min()
    lineas["clase_posicion"] = (
        lineas["ubicacion"].map(por_posicion).map(dict(enumerate(orden_clases)))
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
    lineas["_rango_pos"] = lineas["clase_posicion"].map(rango).fillna(len(rango)).astype(int)
    lineas["_valor_pos"] = lineas["ubicacion"].map(lineas.groupby("ubicacion")["valor_linea"].sum())
    lineas = lineas.sort_values(
        ["_rango_pos", "_valor_pos", "ubicacion", "valor_linea"],
        ascending=[True, False, True, False],
    ).reset_index(drop=True)
    lineas["prioridad"] = lineas.index + 1

    # DEC-078: el par conceptual de `prioridad`. Una dice **qué** contar
    # primero; la otra, **en qué orden caminar** lo que ya se eligió. No
    # compiten: la hoja de conteo selecciona por prioridad y recorre por
    # ruta.
    lineas["orden_recorrido"] = _orden_recorrido(lineas)

    total = len(lineas)
    altura = lineas[lineas["tipo"] == TIPO_ALTURA]
    logger.info(
        "calcular_ubicaciones: %d líneas SKU-posición (%d en altura sobre %d posiciones) · "
        "A+B %.1f%% de altura · heredadas %.1f%% · sin rotación %.1f%% · valorizadas %.1f%%",
        total,
        len(altura),
        altura["ubicacion"].nunique(),
        altura["clase"].str.startswith(("A", "B")).mean() * 100 if len(altura) else 0,
        (altura["origen_clase"] == "Referencia").mean() * 100 if len(altura) else 0,
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


def _orden_recorrido(lineas: pd.DataFrame) -> pd.Series:
    """Orden de visita física, derivado de la geometría del plano (DEC-078).

    La hoja `Plano Layout` muestra la bodega como es: **8 pasillos, cada uno
    entre un par de racks consecutivos** —A|B, C|D, … O|P— con las posiciones
    numeradas a lo largo del pasillo. Verificado contra las columnas del plano,
    no supuesto por el nombre.

    El orden que sale de acá es una **serpentina**: el pasillo impar se
    recorre de la posición 1 hacia la 50 y el par de vuelta, porque quien
    termina el pasillo 1 en la posición 50 entra al pasillo 2 por ese mismo
    extremo. Recorrerlos todos en el mismo sentido obliga a volver caminando
    en vacío la longitud del rack en cada cambio.

    Dentro de una posición se cuentan las dos caras del pasillo (los dos
    racks) y dentro de cada cara se sube por altura.

    Un rack fuera de A-P —`PU`, o uno nuevo— va al final en vez de romper el
    orden o desaparecer.

    Returns:
        Serie de enteros ordenables alineada al índice de `lineas`.
    """
    indice = {r: i for i, r in enumerate(RACKS_EN_PLANO)}
    idx = lineas["rack"].astype(str).str.strip().str.upper().map(indice)

    posicion = pd.to_numeric(lineas["posicion"], errors="coerce").fillna(0).astype(int)
    altura = pd.to_numeric(lineas["nivel"], errors="coerce").fillna(0).astype(int)

    pasillo = (idx // 2 + 1).fillna(_PASILLO_DESCONOCIDO)
    # Cara del pasillo: 0 = rack izquierdo del par, 1 = derecho.
    cara = (idx % 2).fillna(0)

    # Serpentina: los pasillos pares se recorren al revés.
    tope = int(posicion.max()) if len(posicion) else 0
    posicion_ruta = posicion.where(pasillo % 2 == 1, tope - posicion)

    orden = (
        pasillo.astype(int) * 1_000_000
        + posicion_ruta.astype(int) * 1_000
        + cara.astype(int) * 100
        + altura
    )
    return orden.astype(int)


def sin_ubicacion_conocida(df_admin: pd.DataFrame, lineas: pd.DataFrame) -> pd.DataFrame:
    """Producto que el admin dice que existe y que nadie puede ubicar (DEC-073).

    El plan de conteo manda gente a posiciones **donde el sistema ya dice que
    hay algo**, así que por construcción no puede descubrir lo que el sistema
    no ve. Esto es la otra mitad: los ID cuyo catálogo declara inventario y
    que no aparecen en **ninguna** posición de Bochica.

    Medido: 288 ID con **151.345 unidades** que el admin da por existentes y
    que el mapa de bodega no muestra en ningún lado. No es un error de datos
    a corregir en pantalla — es producto que está físicamente en alguna parte
    y hay que salir a buscarlo.

    **Se incluyen los dos estados de `producto_activo`.** Que una referencia
    esté descontinuada no la hace menos física: 102 de esos 288 ID lo están, y
    son justamente los que nadie va a mirar si no aparecen en una lista.

    Args:
        df_admin: Catálogo del admin ya filtrado por alcance.
        lineas: Resultado de `calcular_ubicaciones()`.

    Returns:
        Una fila por ID sin ubicación, ordenada por unidades declaradas.
    """
    columnas = ["id_especificacion", "referencia", "nombre_comercial", "vigencia", "inventario"]
    if df_admin.empty:
        return pd.DataFrame(columns=columnas)

    cat = df_admin.copy()
    cat["id_especificacion"] = cat["id_especificacion"].astype(str)
    cat["inventario"] = pd.to_numeric(cat["inventario"], errors="coerce").fillna(0.0)
    # Un ID puede venir en varias filas del catálogo; el inventario es el
    # mismo dato repetido por almacén, así que se toma el máximo y no la suma.
    cat = cat.groupby("id_especificacion", as_index=False).agg(
        referencia=("referencia", "first"),
        nombre_comercial=("nombre_comercial", "first"),
        producto_activo=("producto_activo", "first"),
        inventario=("inventario", "max"),
    )

    ubicados = set(lineas["id_especificacion"].astype(str)) if not lineas.empty else set()
    fuera = cat[~cat["id_especificacion"].isin(ubicados) & (cat["inventario"] > 0)].copy()
    if fuera.empty:
        return pd.DataFrame(columns=columnas)

    fuera["vigencia"] = fuera["producto_activo"].map(
        {ADMIN_ACTIVO: VIGENCIA_ACTIVO, ADMIN_DESCONTINUADO: VIGENCIA_DESCONTINUADO}
    )
    fuera = fuera.sort_values("inventario", ascending=False)

    logger.info(
        "sin_ubicacion_conocida: %d ID con inventario declarado y ninguna posición "
        "(%.0f unidades · %d vigentes · %d descontinuados)",
        len(fuera),
        float(fuera["inventario"].sum()),
        int((fuera["vigencia"] == VIGENCIA_ACTIVO).sum()),
        int((fuera["vigencia"] == VIGENCIA_DESCONTINUADO).sum()),
    )
    return fuera[columnas]


def mapa_posiciones(
    lineas: pd.DataFrame,
    layout: pd.DataFrame,
    conteos: pd.DataFrame | None = None,
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Una fila por posición **activa del layout**, ocupada o no.

    `calcular_ubicaciones()` solo conoce lo que tiene inventario, así que no
    puede responder "qué tan llena está la bodega": le falta el denominador.
    Acá el universo lo pone el layout y las líneas aportan el contenido, de
    modo que **las posiciones vacías existen como filas con cero** — que es
    justamente la mitad de la información en un mapa de ocupación.

    Args:
        lineas: Resultado de `calcular_ubicaciones()`.
        layout: Resultado de `layout.cargar_layout()`.
        conteos: Resultado de `conteos.evaluar_conteos()`, para fechar el
            último conteo de cada posición.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        Una fila por posición activa, con su contenido agregado y la
        antigüedad de su último conteo.
    """
    columnas = [
        "ubicacion",
        "tipo",
        "rack",
        "posicion",
        "nivel",
        "lineas",
        "unidades",
        "valor",
        "clase_posicion",
        "ocupada",
        "ultimo_conteo",
        "dias_desde_conteo",
    ]
    if layout.empty:
        return pd.DataFrame(columns=columnas)

    activas = layout[layout["activa"].astype(str).str.strip().str.upper().isin({"SI", "SÍ"})][
        ["ubicacion", "tipo", "rack", "posicion", "altura"]
    ].copy()
    activas = activas.rename(columns={"altura": "nivel"})

    if lineas.empty:
        contenido = pd.DataFrame(
            columns=["ubicacion", "lineas", "unidades", "valor", "clase_posicion"]
        )
    else:
        contenido = lineas.groupby("ubicacion", as_index=False).agg(
            lineas=("id_especificacion", "count"),
            unidades=("cantidad", "sum"),
            valor=("valor_linea", "sum"),
            clase_posicion=("clase_posicion", "first"),
        )

    mapa = activas.merge(contenido, on="ubicacion", how="left")
    for columna in ("lineas", "unidades", "valor"):
        mapa[columna] = mapa[columna].fillna(0.0)
    mapa["ocupada"] = (mapa["lineas"] > 0).astype(int)
    mapa["clase_posicion"] = mapa["clase_posicion"].fillna("Vacía")

    # ── Antigüedad del último conteo (KPI de la sección 12 del plan) ──
    # Una posición nunca contada queda en NaN, **no en un número grande**:
    # "hace 9.999 días" entraría en cualquier promedio y lo arruinaría, y
    # "nunca" no es un valor de la misma escala que "hace 30 días".
    referencia = pd.Timestamp(hoy or dt.date.today())
    if conteos is not None and not conteos.empty:
        ultimo = conteos.groupby("ubicacion")["fecha"].max()
        mapa["ultimo_conteo"] = mapa["ubicacion"].map(ultimo)
        fechas = pd.to_datetime(mapa["ultimo_conteo"], errors="coerce")
        mapa["dias_desde_conteo"] = (referencia - fechas).dt.days
    else:
        mapa["ultimo_conteo"] = None
        mapa["dias_desde_conteo"] = pd.NA

    ocupadas = int(mapa["ocupada"].sum())
    logger.info(
        "mapa_posiciones: %d posiciones activas · %d ocupadas (%.1f%%) · %d vacías",
        len(mapa),
        ocupadas,
        ocupadas / len(mapa) * 100 if len(mapa) else 0,
        len(mapa) - ocupadas,
    )
    return mapa[columnas]
