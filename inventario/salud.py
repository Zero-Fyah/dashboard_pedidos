"""
salud.py — Cobertura, movimiento y riesgo de quiebre por referencia (DEC-049).

Responde tres preguntas sobre el nivel de inventario: ¿alcanza?, ¿se está
moviendo?, ¿hay algo detenido?

Se calcula en el ciclo del scheduler y se persiste, igual que el cruce de
inventario (DEC-043). Motivo medido: la agregación de demanda sobre las
840.000 líneas cuesta **22,2 s en SQL y 10,4 s en pandas** — inviable para
una página, irrelevante dentro de una corrida que ya toma 14 s. Se probaron
índices sobre `pedidos(fecha)`, `lineas_pedido(referencia, almacen)` y
`subpedidos(id_pedido, numero_subpedido)`: **no cambiaron el tiempo** (el
costo es el join de 840.000 filas, no la búsqueda), así que se descartaron
en vez de dejar índices que solo agregan peso a la escritura.

**Alcance:** el mismo que el cruce de inventario — catálogo sin `Arena` y
almacén Bogotá (DEC-041), para que las dos vistas hablen de lo mismo.

**Vigencia (DEC-065):** cada referencia se marca `Activo` o
`Descontinuado` según el propio admin. Se calcula para todas, pero la
salud se lee sobre las vigentes: el producto apagado no es un problema de
abastecimiento y mezclarlo hacía que la categoría más grande de la página
—564 referencias, 39% del catálogo— fuera 96% catálogo muerto.
"""

import datetime as dt
import logging
import sqlite3

import pandas as pd

from comun import (
    COBERTURA_ALTA_D,
    COBERTURA_RIESGO_D,
    VENTANA_DEMANDA_D,
    VIGENCIA_ACTIVO,
    VIGENCIA_DESCONTINUADO,
    familia_de,
)
from inventario.normalizador import vigencia_por_referencia

logger = logging.getLogger("inventario.salud")

VENTANA_CORTA_D = 30

# Los umbrales de cobertura y la ventana de demanda viven en `comun/` desde
# DEC-066/DEC-119: dejaron de ser defaults de implementación (o, en el caso
# de la ventana, la necesita también comun/arena.py) y la página de Salud
# los cita al usuario. Se reexportan acá porque este módulo es donde se
# clasifica con ellos, y para no romper a quien ya los importaba de acá.
#
# Sigue sin existir un indicador de "exceso sobre el máximo objetivo": eso
# necesita un stock objetivo por referencia, que el negocio no tiene definido.
__all__ = [
    "COBERTURA_ALTA_D",
    "COBERTURA_RIESGO_D",
    "ESTADO_ALTA",
    "ESTADO_NORMAL",
    "ESTADO_QUIEBRE",
    "ESTADO_RIESGO",
    "ESTADO_SIN_DEMANDA",
    "ESTADO_SIN_STOCK_NI_DEMANDA",
    "SIN_MOVIMIENTO_D",
    "VENTANA_CORTA_D",
    "VENTANA_DEMANDA_D",
    "calcular_salud",
]

# Un producto sin salidas en este plazo entra en la lista de detenidos.
SIN_MOVIMIENTO_D = 180

# "Sin stock" a secas mezcla dos cosas muy distintas y una de ellas no es
# accionable. Medido sobre datos reales: de 721 referencias en cero, solo
# 152 tuvieron demanda en los últimos 90 días — esas son un quiebre real.
# Las otras 569 (410 sin una sola venta histórica) son catálogo muerto:
# limpieza de maestro, no un problema de abastecimiento.
ESTADO_QUIEBRE = "Quiebre"
ESTADO_SIN_STOCK_NI_DEMANDA = "Sin stock ni demanda"
ESTADO_RIESGO = "Riesgo de quiebre"
ESTADO_NORMAL = "Normal"
ESTADO_ALTA = "Cobertura alta"
ESTADO_SIN_DEMANDA = "Sin demanda"

# Colombia opera en UTC-5 sin horario de verano (AUD-B6). `pedidos.fecha`
# es fecha local, así que "hoy" tiene que serlo también.
_OFFSET_CO_H = 5


def _hoy_colombia() -> dt.date:
    return (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=_OFFSET_CO_H)).date()


def _demanda_por_referencia(con: sqlite3.Connection, almacen: str) -> pd.DataFrame:
    """Agrega unidades vendidas y última salida por referencia.

    Se resuelve en pandas y no en SQL porque se midió: 10,4 s contra 22,2 s
    sobre los mismos datos.

    **Excluye los subpedidos cancelados**: una venta que se canceló no es
    demanda, y contarla inflaría la cobertura justo en las referencias con
    más cancelaciones.
    """
    lineas = pd.read_sql(
        """SELECT id_pedido, numero_subpedido, referencia, almacen, cantidad_comprada
           FROM lineas_pedido
           WHERE referencia IS NOT NULL AND referencia != ''""",
        con,
    )
    pedidos = pd.read_sql("SELECT id_pedido, fecha FROM pedidos", con)
    subpedidos = pd.read_sql("SELECT id_pedido, numero_subpedido, estado FROM subpedidos", con)

    m = lineas.merge(pedidos, on="id_pedido").merge(
        subpedidos, on=["id_pedido", "numero_subpedido"]
    )
    m = m[(m["almacen"] == almacen) & (m["estado"].str.lower() != "cancelado")]
    m["referencia"] = m["referencia"].astype(str).str.strip()
    return m


def calcular_salud(
    df_admin: pd.DataFrame,
    con: sqlite3.Connection,
    *,
    almacen: str = "Bogotá",
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Calcula cobertura, movimiento y riesgo por referencia.

    Args:
        df_admin: Catálogo del admin **ya filtrado** por
            `filtrar_alcance_admin()`.
        con: Conexión de lectura a pedidos.db.
        almacen: Almacén cuya demanda se mide. Debe coincidir con el del
            catálogo filtrado.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        DataFrame por `referencia` con vigencia, disponible, valor, demanda
        de ambas ventanas, cobertura, antigüedad de la última salida y
        estado. Incluye las descontinuadas: la separación es del consumidor
        (la página muestra las vigentes y las otras en un bloque aparte),
        no de esta función, que no borra datos medidos.
    """
    referencia_hoy = hoy or _hoy_colombia()
    desde_larga = (referencia_hoy - dt.timedelta(days=VENTANA_DEMANDA_D)).isoformat()
    desde_corta = (referencia_hoy - dt.timedelta(days=VENTANA_CORTA_D)).isoformat()

    admin = df_admin.copy()
    admin["referencia"] = admin["referencia"].astype(str).str.strip()
    # El valor se calcula fila por fila y después se suma: una referencia
    # agrupa varias especificaciones con precios distintos, así que
    # promediar el precio daría un número que no corresponde a nada.
    admin["valor_fila"] = admin["inventario"] * admin["precio"]
    stock = admin.groupby("referencia", as_index=False).agg(
        disponible=("inventario", "sum"), valor_venta=("valor_fila", "sum")
    )

    mov = _demanda_por_referencia(con, almacen)
    agregados = mov.groupby("referencia", as_index=False).agg(ultima_salida=("fecha", "max"))
    for etiqueta, desde in (("demanda_90d", desde_larga), ("demanda_30d", desde_corta)):
        ventana = (
            mov[mov["fecha"] >= desde]
            .groupby("referencia", as_index=False)["cantidad_comprada"]
            .sum()
            .rename(columns={"cantidad_comprada": etiqueta})
        )
        agregados = agregados.merge(ventana, on="referencia", how="left")

    df = stock.merge(agregados, on="referencia", how="left")
    for col in ("demanda_90d", "demanda_30d"):
        df[col] = df[col].fillna(0)

    # DEC-065: el producto descontinuado no es un problema de salud de
    # inventario, y mezclarlo con el vigente rompía la página. Medido: 540
    # de las 564 referencias en "Sin stock ni demanda" ya estaban marcadas
    # como descontinuadas en el propio admin — el 96% de la categoría más
    # grande era catálogo apagado presentado como alarma.
    df["vigencia"] = (
        df["referencia"].map(vigencia_por_referencia(admin)).fillna(VIGENCIA_DESCONTINUADO)
    )

    df["familia"] = df["referencia"].map(familia_de)
    df["demanda_diaria"] = df["demanda_90d"] / VENTANA_DEMANDA_D
    # Sin demanda no hay cobertura que calcular: dejarlo en NULL es honesto,
    # poner infinito o cero inventaría un dato.
    #
    # El cero se reemplaza por NaN ANTES de dividir, no se enmascara después:
    # con `.where()` pandas evalúa la división completa primero y revienta con
    # ZeroDivisionError si ninguna referencia tuvo demanda en la ventana — un
    # ciclo sin ventas tumbaría la corrida entera.
    df["dias_cobertura"] = df["disponible"] / df["demanda_diaria"].replace(0, float("nan"))
    df["dias_sin_salida"] = (
        pd.to_datetime(referencia_hoy) - pd.to_datetime(df["ultima_salida"], errors="coerce")
    ).dt.days

    df["estado"] = [
        _clasificar(disp, cob, dem)
        for disp, cob, dem in zip(
            df["disponible"], df["dias_cobertura"], df["demanda_90d"], strict=True
        )
    ]

    # DEC-065: los conteos se reportan sobre el universo vigente, que es el
    # que la página muestra. Contarlos sobre el total mezclaba producto
    # apagado y hacía ver el quiebre más grande de lo que es.
    vigente = df[df["vigencia"] == VIGENCIA_ACTIVO]
    logger.info(
        "calcular_salud: %d referencias vigentes de %d "
        "(%d descontinuadas) · %d en quiebre · %d en riesgo · %d sin movimiento >%dd",
        len(vigente),
        len(df),
        int((df["vigencia"] == VIGENCIA_DESCONTINUADO).sum()),
        int((vigente["estado"] == ESTADO_QUIEBRE).sum()),
        int((vigente["estado"] == ESTADO_RIESGO).sum()),
        int((vigente["dias_sin_salida"] > SIN_MOVIMIENTO_D).sum()),
        SIN_MOVIMIENTO_D,
    )
    return df[
        [
            "referencia",
            "vigencia",
            "familia",
            "disponible",
            "valor_venta",
            "demanda_30d",
            "demanda_90d",
            "demanda_diaria",
            "dias_cobertura",
            "ultima_salida",
            "dias_sin_salida",
            "estado",
        ]
    ].sort_values("referencia")


def _clasificar(disponible: float, cobertura: float, demanda: float) -> str:
    """Traduce cobertura y stock a un estado legible.

    El orden importa. Estar en cero manda sobre todo lo demás, pero se
    parte en dos según haya o no demanda: un quiebre con demanda es un
    problema de abastecimiento, y uno sin demanda es catálogo por limpiar.
    Igual con el stock parado: no es lo mismo "tengo para mucho tiempo"
    que "nadie lo pide".
    """
    if disponible <= 0:
        return ESTADO_QUIEBRE if demanda > 0 else ESTADO_SIN_STOCK_NI_DEMANDA
    if demanda <= 0:
        return ESTADO_SIN_DEMANDA
    if cobertura < COBERTURA_RIESGO_D:
        return ESTADO_RIESGO
    if cobertura > COBERTURA_ALTA_D:
        return ESTADO_ALTA
    return ESTADO_NORMAL
