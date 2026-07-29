"""
cancelaciones.py — Mercancía alistada que terminó cancelada (DEC-063).

Un subpedido que se alistó por completo y después se canceló describe
mercancía que **salió físicamente de su posición y nunca se despachó**.
Entre que salió y que alguien registró la cancelación, el sistema no sabe
dónde está.

**Ojo con el encuadre, porque el obvio es falso.** La lectura intuitiva
—"se alistó y enseguida se canceló, el producto está en el piso de
despacho"— no resiste la medición: el **86%** de estos subpedidos se cancela
**más de un mes después** de cerrado el alistamiento, y solo el 2% el mismo
día (mediana: 2.166 horas ≈ 90 días).

O sea que no es un evento operativo de devolución inmediata: es un **rezago
administrativo**. Pedidos que se alistaron, nunca se despacharon, y meses
después alguien los cerró como cancelados. El hallazgo no es "hay que
devolver el producto hoy" sino "hubo mercancía en estado indeterminado
durante meses", que es una explicación candidata de las diferencias que el
conteo va a encontrar.

**Alcance.** Solo cuenta lo que está en el alcance del plan de inventario.
Sin filtrar, el 79% de las unidades es arena por tonelada y otros
almacenes —115.979 de 147.435— y tapa por completo la señal real.
"""

import logging

import pandas as pd

logger = logging.getLogger("inventario.cancelaciones")

_COLUMNAS = [
    "id_pedido",
    "numero_subpedido",
    "mes",
    "alistador",
    "cierre_alistamiento",
    "cancelado_en",
    "dias_hasta_cancelacion",
    "lineas",
    "unidades",
    "valor",
]


def calcular_cancelaciones(con, df_admin: pd.DataFrame) -> pd.DataFrame:
    """Subpedidos alistados por completo que después se cancelaron.

    Args:
        con: Conexión de lectura a pedidos.db.
        df_admin: Catálogo del admin **ya filtrado por alcance**, para
            excluir arena y otros almacenes.

    Returns:
        Una fila por subpedido, con el volumen que se alistó y cuánto tardó
        en registrarse la cancelación.
    """
    crudo = pd.read_sql(
        """SELECT s.id_pedido, s.numero_subpedido, s.alistador,
                  s.alistamiento_completado, s.estado_cambiado_en,
                  TRIM(l.referencia) AS referencia,
                  l.cantidad_comprada, l.monto_final_num,
                  SUBSTR(p.fecha, 1, 7) AS mes
           FROM subpedidos s
           JOIN lineas_pedido l
             ON l.id_pedido = s.id_pedido AND l.numero_subpedido = s.numero_subpedido
           JOIN pedidos p ON p.id_pedido = s.id_pedido
           WHERE LOWER(s.estado) = 'cancelado'
             AND s.alistamiento_completado IS NOT NULL
             AND s.alistamiento_completado NOT IN ('-', '')""",
        con,
    )
    if crudo.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    en_alcance = set(df_admin["referencia"].astype(str).str.strip())
    crudo = crudo[crudo["referencia"].isin(en_alcance)]
    if crudo.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    # `utc=True` en ambas: `estado_cambiado_en` viene con zona y
    # `alistamiento_completado` sin ella, y restarlas crudas revienta.
    for columna in ("alistamiento_completado", "estado_cambiado_en"):
        crudo[columna] = pd.to_datetime(crudo[columna], errors="coerce", utc=True)

    agrupado = crudo.groupby(["id_pedido", "numero_subpedido"], as_index=False).agg(
        mes=("mes", "first"),
        alistador=("alistador", "first"),
        cierre_alistamiento=("alistamiento_completado", "min"),
        cancelado_en=("estado_cambiado_en", "max"),
        lineas=("referencia", "size"),
        unidades=("cantidad_comprada", "sum"),
        valor=("monto_final_num", "sum"),
    )
    agrupado["dias_hasta_cancelacion"] = (
        agrupado["cancelado_en"] - agrupado["cierre_alistamiento"]
    ).dt.total_seconds() / 86400
    for columna in ("cierre_alistamiento", "cancelado_en"):
        agrupado[columna] = agrupado[columna].dt.strftime("%Y-%m-%d")

    tardias = (agrupado["dias_hasta_cancelacion"] > 30).mean() * 100
    logger.info(
        "calcular_cancelaciones: %d subpedidos alistados y luego cancelados · "
        "%.0f unidades · %.1f%% se cancelaron con más de un mes de rezago",
        len(agrupado),
        agrupado["unidades"].sum(),
        tardias,
    )
    return agrupado[_COLUMNAS].sort_values("unidades", ascending=False).reset_index(drop=True)
