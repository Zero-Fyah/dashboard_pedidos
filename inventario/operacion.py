"""
operacion.py — Tiempos de ciclo y productividad del almacén (DEC-054).

Mide qué tan rápido avanza un subpedido por el proceso y cuánto procesa
cada operario. Sale íntegramente de datos que el scraper ya recolecta: las
marcas de tiempo de `subpedidos` y sus campos `alistador`/`inspector`.

**Qué significa cada marca — verificado contra `registro_operaciones`, no
asumido por su nombre:**

| Campo | Lo que realmente marca |
|---|---|
| `inicio_alistamiento` | El subpedido **entra a la cola**, no cuando el alistador empieza (HAL-013) |
| `inicio_inspeccion` | El alistamiento físico **terminó** y arranca la inspección |
| `inspeccion_completada` | La inspección terminó |
| `alistamiento_completado` | ⚠️ **Mal nombrado**: coincide con `inspeccion_completada`, no con el fin del picking. No se usa |

De ahí salen los tres intervalos que sí significan algo:

    cola_y_picking = inicio_inspeccion    − inicio_alistamiento
    inspeccion     = inspeccion_completada − inicio_inspeccion
    ciclo_total    = inspeccion_completada − inicio_alistamiento

`cola_y_picking` se llama así a propósito: **no es tiempo de picking**.
Incluye la espera en cola, que con una mediana de ~17 h la domina. Llamarlo
"tiempo de alistamiento" invitaría a leer como lentitud del operario algo
que es, en su mayor parte, espera.

**Lo que NO se puede medir** con los datos actuales, y por eso no está: las
unidades por hora-hombre (no hay registro de horas trabajadas),
el *dock-to-stock* (no hay datos de recepción) y la exactitud de *put-away*
(no hay registro de ubicación asignada contra real).
"""

import logging
import sqlite3

import pandas as pd

logger = logging.getLogger("inventario.operacion")

# Placeholder del sistema origen para "sin dato" (DEC-025).
_VACIOS = ("-", "")

_COLUMNAS = [
    "id_pedido",
    "numero_subpedido",
    "dia",
    "alistador",
    "n_alistadores",
    "inspector",
    "lineas",
    "unidades",
    "cola_y_picking_h",
    "inspeccion_h",
    "ciclo_total_h",
]


def calcular_operacion(con: sqlite3.Connection) -> pd.DataFrame:
    """Tiempos de ciclo por subpedido, con su carga y sus operarios.

    Se persiste al detalle (una fila por subpedido, ~27.000) en vez de
    agregado: es poco volumen y deja que la vista agrupe por día, por
    operario o por lo que haga falta sin volver a calcular.

    Args:
        con: Conexión de lectura a pedidos.db.

    Returns:
        DataFrame con una fila por subpedido que tenga las tres marcas.
    """
    sub = pd.read_sql(
        """SELECT id_pedido, numero_subpedido, alistador, inspector,
                  inicio_alistamiento, inicio_inspeccion, inspeccion_completada
           FROM subpedidos""",
        con,
    )
    carga = pd.read_sql(
        """SELECT id_pedido, numero_subpedido,
                  COUNT(*)                  AS lineas,
                  SUM(cantidad_comprada)    AS unidades
           FROM lineas_pedido
           GROUP BY id_pedido, numero_subpedido""",
        con,
    )

    df = sub.merge(carga, on=["id_pedido", "numero_subpedido"], how="left")
    for columna in ("inicio_alistamiento", "inicio_inspeccion", "inspeccion_completada"):
        df[columna] = pd.to_datetime(df[columna].where(~df[columna].isin(_VACIOS)), errors="coerce")
    df = df.dropna(subset=["inicio_alistamiento", "inicio_inspeccion", "inspeccion_completada"])

    hora = 3600
    df["cola_y_picking_h"] = (
        df["inicio_inspeccion"] - df["inicio_alistamiento"]
    ).dt.total_seconds() / hora
    df["inspeccion_h"] = (
        df["inspeccion_completada"] - df["inicio_inspeccion"]
    ).dt.total_seconds() / hora
    df["ciclo_total_h"] = (
        df["inspeccion_completada"] - df["inicio_alistamiento"]
    ).dt.total_seconds() / hora

    # Marcas fuera de orden: 4 casos de 27.286 medidos. Se descartan en vez
    # de arrastrar duraciones negativas que romperían cualquier promedio.
    antes = len(df)
    df = df[(df["cola_y_picking_h"] >= 0) & (df["ciclo_total_h"] >= 0)]
    if antes - len(df):
        logger.info(
            "calcular_operacion: %d subpedidos descartados por marcas fuera de orden",
            antes - len(df),
        )

    # El día se toma del fin de la inspección: es cuando el trabajo quedó
    # hecho, que es lo que se quiere contar en la productividad diaria.
    df["dia"] = df["inspeccion_completada"].dt.strftime("%Y-%m-%d")
    df["alistador"] = df["alistador"].where(~df["alistador"].isin(_VACIOS))
    df["inspector"] = df["inspector"].where(~df["inspector"].isin(_VACIOS))
    df["n_alistadores"] = (
        df["alistador"].fillna("").str.count(",").add(1).where(df["alistador"].notna(), 0)
    )
    for columna in ("lineas", "unidades"):
        df[columna] = df[columna].fillna(0).astype(float)

    logger.info(
        "calcular_operacion: %d subpedidos · %d días · mediana de ciclo %.1f h",
        len(df),
        df["dia"].nunique(),
        df["ciclo_total_h"].median(),
    )
    return df[_COLUMNAS]


# ─────────────────────────────────────────────
# Ventana activa — capacidad del equipo (DEC-055)
# ─────────────────────────────────────────────

# Un día con uno o dos cierres no define una jornada, y una ventana muy
# corta da ritmos absurdos al dividir. Con estos mínimos se descarta el
# 26,3% de los días-alistador, que concentran solo el 3,7% de las líneas:
# se pierden jornadas marginales, no volumen.
MIN_CIERRES_JORNADA = 3
MIN_VENTANA_H = 0.5

_COLUMNAS_VENTANAS = [
    "usuario",
    "dia",
    "primer_cierre",
    "ultimo_cierre",
    "ventana_h",
    "subpedidos",
    "lineas",
    "unidades",
    "utilizable",
]


def calcular_ventanas(con: sqlite3.Connection) -> pd.DataFrame:
    """Ventana activa por alistador y día, a partir del log de operaciones.

    `registro_operaciones` resultó estar **a nivel de subpedido**: su campo
    `referencia` es el `numero_subpedido` (verificado: 25.044 de 25.047
    eventos casan con un subpedido real). Eso da un cierre fechado por
    subpedido, con el usuario que lo registró.

    **Por qué la ventana y no el hueco entre cierres.** El intento directo
    —medir el tiempo entre cierres consecutivos del mismo alistador— da
    números imposibles: mediana de 0,81 minutos por línea y un percentil 25
    de 8 segundos. La causa es que **el registro se hace en ráfagas**: el
    15,6% de los cierres ocurre a menos de 60 segundos del anterior. Ese
    hueco mide tecleo, no picking.

    La ventana del primer al último cierre del día esquiva el problema
    porque promedia sobre la jornada entera. **Validación:** la ventana
    mediana da 6,8 h y por alistador queda entre 7,1 y 8,2 h — reproduce un
    turno real, que es lo que da confianza en que mide algo.

    ⚠️ **Sirve para el equipo, no para comparar personas.** El evento trae
    un solo `usuario` pero el 52,3% de los subpedidos tuvo varios
    alistadores, así que el **83,8% de las líneas** se le acredita a quien
    cerró, no a quien picó. Al sumar el equipo el error se cancela (cada
    subpedido se cuenta una vez); a nivel individual, no.

    Args:
        con: Conexión de lectura a pedidos.db.

    Returns:
        DataFrame por `(usuario, dia)` con la ventana y lo cerrado en ella.
    """
    eventos = pd.read_sql(
        """SELECT ro.id_pedido, ro.referencia AS numero_subpedido,
                  ro.momento, ro.usuario
           FROM registro_operaciones ro
           JOIN subpedidos s
             ON s.id_pedido = ro.id_pedido
            AND s.numero_subpedido = ro.referencia
           WHERE ro.accion LIKE 'Alistamiento%'
             AND ro.usuario IS NOT NULL AND ro.usuario != ''""",
        con,
    )
    carga = pd.read_sql(
        """SELECT id_pedido, numero_subpedido,
                  COUNT(*) AS lineas, SUM(cantidad_comprada) AS unidades
           FROM lineas_pedido GROUP BY id_pedido, numero_subpedido""",
        con,
    )
    eventos["momento"] = pd.to_datetime(eventos["momento"], errors="coerce")
    eventos = eventos.dropna(subset=["momento"])
    if eventos.empty:
        return pd.DataFrame(columns=_COLUMNAS_VENTANAS)

    eventos = eventos.merge(carga, on=["id_pedido", "numero_subpedido"], how="left").fillna(
        {"lineas": 0, "unidades": 0}
    )
    eventos["usuario"] = eventos["usuario"].str.strip()
    eventos["dia"] = eventos["momento"].dt.strftime("%Y-%m-%d")

    v = eventos.groupby(["usuario", "dia"], as_index=False).agg(
        primer_cierre=("momento", "min"),
        ultimo_cierre=("momento", "max"),
        subpedidos=("momento", "size"),
        lineas=("lineas", "sum"),
        unidades=("unidades", "sum"),
    )
    v["ventana_h"] = (v["ultimo_cierre"] - v["primer_cierre"]).dt.total_seconds() / 3600
    v["utilizable"] = (
        (v["subpedidos"] >= MIN_CIERRES_JORNADA) & (v["ventana_h"] >= MIN_VENTANA_H)
    ).astype(int)
    for columna in ("primer_cierre", "ultimo_cierre"):
        v[columna] = v[columna].dt.strftime("%Y-%m-%d %H:%M:%S")

    utiles = v[v["utilizable"] == 1]
    logger.info(
        "calcular_ventanas: %d días-alistador (%d utilizables, %.1f%% de las líneas) · "
        "ventana mediana %.1f h",
        len(v),
        len(utiles),
        utiles["lineas"].sum() / max(v["lineas"].sum(), 1) * 100,
        utiles["ventana_h"].median() if len(utiles) else 0,
    )
    return v[_COLUMNAS_VENTANAS]
