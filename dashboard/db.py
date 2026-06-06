import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent.parent / "data" / "pedidos.db"
ESTADOS_CERRADOS = ("completado", "cancelado", "comentado")

try:
    _con = sqlite3.connect(DB_PATH)
    _cols = [r[1] for r in _con.execute("PRAGMA table_info(lineas_pedido)")]
    _NUM_COLS_EXIST = "monto_pagar_num" in _cols
    _VIEW_CONSOLIDADO_EXISTS = (
        _con.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='view' AND name='v_inventario_comprometido'"
        ).fetchone()[0]
        > 0
    )
    _con.close()
except Exception:
    _NUM_COLS_EXIST = False
    _VIEW_CONSOLIDADO_EXISTS = False

COLS_CONSOLIDADO = [
    "Producto", "Referencia", "Presentación", "Almacén",
    "Estado subpedido", "Comprometido", "Entregado",
    "Pendiente", "Pedidos con stock",
]


def _check_db() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Base de datos no encontrada: {DB_PATH.resolve()}\n"
            "Ejecuta el scraper primero: py scraper/scraper_principal.py --desde 2026-05-01"
        )


def _conn() -> sqlite3.Connection:
    _check_db()
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=7200, show_spinner=False)
def get_opciones_filtro() -> tuple[list[str], list[str], list[str], list[str]]:
    con = _conn()
    try:
        estados = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT estado FROM subpedidos "
                "WHERE estado IS NOT NULL AND estado != '' ORDER BY estado"
            )
        ]
        almacenes = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT almacen FROM lineas_pedido "
                "WHERE almacen IS NOT NULL AND almacen != '' ORDER BY almacen"
            )
        ]
        tipos = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT tipo_subpedido FROM subpedidos "
                "WHERE tipo_subpedido IS NOT NULL AND tipo_subpedido != '' "
                "ORDER BY tipo_subpedido"
            )
        ]
    finally:
        con.close()
    return ["Todos", "Abiertos", "Cerrados"], estados, almacenes, tipos


@st.cache_data(ttl=7200, show_spinner=False)
def get_consolidado(
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
) -> pd.DataFrame:
    con = _conn()
    view_exists = (
        con.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='view' AND name='v_inventario_comprometido'"
        ).fetchone()[0]
        > 0
    )
    if not view_exists:
        con.close()
        return pd.DataFrame(columns=COLS_CONSOLIDADO)

    conditions = ["nombre_producto IS NOT NULL", "nombre_producto != ''"]
    params: list = []

    if estados_sub:
        placeholders = ",".join("?" * len(estados_sub))
        conditions.append(f"estado IN ({placeholders})")
        params.extend(estados_sub)

    if almacenes:
        placeholders = ",".join("?" * len(almacenes))
        conditions.append(f"almacen IN ({placeholders})")
        params.extend(almacenes)

    where_clause = " AND ".join(conditions)

    sql = f"""
        SELECT
            nombre_producto                  AS "Producto",
            referencia                       AS "Referencia",
            presentacion                     AS "Presentación",
            almacen                          AS "Almacén",
            estado                           AS "Estado subpedido",
            SUM(cantidad_comprometida_total) AS "Comprometido",
            SUM(cantidad_entregada_total)    AS "Entregado",
            SUM(cantidad_pendiente)          AS "Pendiente",
            SUM(pedidos_activos)             AS "Pedidos con stock"
        FROM v_inventario_comprometido
        WHERE {where_clause}
        GROUP BY nombre_producto, referencia, presentacion, almacen, estado
        ORDER BY SUM(cantidad_pendiente) DESC
    """

    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    return df


@st.cache_data(ttl=7200, show_spinner=False)
def get_pedidos(
    estado_pedido: str,
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
) -> pd.DataFrame:
    filtro_estado_sub_en_join = ""
    params_join_sub: list = []
    if estados_sub:
        placeholders = ",".join("?" * len(estados_sub))
        filtro_estado_sub_en_join = f"AND s.estado IN ({placeholders})"
        params_join_sub = list(estados_sub)

    filtro_almacen_en_join = ""
    params_join_alm: list = []
    if almacenes:
        placeholders = ",".join("?" * len(almacenes))
        filtro_almacen_en_join = f"AND l.almacen IN ({placeholders})"
        params_join_alm = list(almacenes)

    if estado_pedido == "Abiertos":
        filtro_estado_pedido = """AND EXISTS (
            SELECT 1 FROM subpedidos s2
            WHERE s2.id_pedido = p.id_pedido
              AND LOWER(s2.estado) NOT IN ('completado', 'cancelado', 'comentado')
        )"""
    elif estado_pedido == "Cerrados":
        filtro_estado_pedido = """AND NOT EXISTS (
            SELECT 1 FROM subpedidos s2
            WHERE s2.id_pedido = p.id_pedido
              AND LOWER(s2.estado) NOT IN ('completado', 'cancelado', 'comentado')
        )"""
    else:
        filtro_estado_pedido = ""

    sql = f"""
        SELECT
            p.id_pedido                         AS "ID Pedido",
            p.fecha                             AS Fecha,
            p.servicio_cliente                  AS Cliente,
            p.vendedor                          AS Vendedor,
            p.forma_pago                        AS "Forma de pago",
            p.metodo_entrega                    AS "Método entrega",
            p.destinatario                      AS Destinatario,
            p.hay_diferencia                    AS _hay_diferencia,
            COUNT(DISTINCT s.numero_subpedido)  AS Subpedidos,
            GROUP_CONCAT(DISTINCT s.estado)     AS "Estados subpedidos"
        FROM pedidos p
        JOIN subpedidos s ON p.id_pedido = s.id_pedido
                         {filtro_estado_sub_en_join}
        JOIN lineas_pedido l ON l.id_pedido = s.id_pedido
                            AND l.numero_subpedido = s.numero_subpedido
                            {filtro_almacen_en_join}
        WHERE p.scraping_completo = 1
          {filtro_estado_pedido}
        GROUP BY p.id_pedido
        ORDER BY p.fecha DESC, p.id_pedido DESC
    """

    params = params_join_sub + params_join_alm
    con = _conn()
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    df["⚠ Diferencia"] = df.pop("_hay_diferencia").map({1: "Sí", 0: ""})
    return df


@st.cache_data(ttl=7200, show_spinner=False)
def get_pedidos_activos(
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
    tipos_sub: tuple[str, ...],
) -> pd.DataFrame:
    """Retorna pedidos con al menos un subpedido abierto, con métricas de tiempo.

    Incluye días desde creación (antigüedad) y días desde última actualización
    (detección de pedidos estancados). Aplica filtros de estado de subpedido,
    almacén y tipo de subpedido.

    Los filtros determinan qué pedidos aparecen, pero los conteos de subpedidos
    reflejan siempre el total real del pedido (no solo los subpedidos filtrados).

    Args:
        estados_sub: Tupla de estados de subpedido a incluir. Vacío = todos.
        almacenes: Tupla de almacenes a incluir. Vacío = todos.
        tipos_sub: Tupla de tipos de subpedido a incluir. Vacío = todos.

    Returns:
        DataFrame con una fila por pedido activo, ordenado por días_abierto DESC.
    """
    # Todos los filtros van como EXISTS en WHERE para no distorsionar
    # los agregados COUNT/SUM que deben reflejar el pedido completo.
    params: list = []

    filtro_tipo = ""
    if tipos_sub:
        placeholders = ",".join("?" * len(tipos_sub))
        filtro_tipo = f"""AND EXISTS (
            SELECT 1 FROM subpedidos st
            WHERE st.id_pedido = p.id_pedido
              AND st.tipo_subpedido IN ({placeholders})
        )"""
        params.extend(tipos_sub)

    filtro_estado = ""
    if estados_sub:
        placeholders = ",".join("?" * len(estados_sub))
        filtro_estado = f"""AND EXISTS (
            SELECT 1 FROM subpedidos se
            WHERE se.id_pedido = p.id_pedido
              AND se.estado IN ({placeholders})
        )"""
        params.extend(estados_sub)

    filtro_almacen = ""
    if almacenes:
        placeholders = ",".join("?" * len(almacenes))
        filtro_almacen = f"""AND EXISTS (
            SELECT 1 FROM lineas_pedido l
            WHERE l.id_pedido = p.id_pedido
              AND l.almacen IN ({placeholders})
        )"""
        params.extend(almacenes)

    sql = f"""
        SELECT
            p.id_pedido                                         AS "ID Pedido",
            p.fecha                                             AS "Fecha creación",
            p.servicio_cliente                                  AS "Cliente",
            p.vendedor                                          AS "Vendedor",
            p.forma_pago                                        AS "Forma de pago",
            p.metodo_entrega                                    AS "Método entrega",
            p.destinatario                                      AS "Destinatario",
            p.hay_diferencia                                    AS _hay_diferencia,
            CAST(
                JULIANDAY(DATE('now')) - JULIANDAY(p.fecha)
                AS INTEGER
            )                                                   AS "Días abierto",
            CAST(
                JULIANDAY(DATE('now')) - JULIANDAY(DATE(p.actualizado_en))
                AS INTEGER
            )                                                   AS "Días sin mov.",
            COUNT(DISTINCT s.numero_subpedido)                  AS "Subpedidos",
            SUM(CASE WHEN LOWER(s.estado) NOT IN
                ('completado','cancelado','comentado')
                THEN 1 ELSE 0 END)                              AS "Sub. abiertos",
            GROUP_CONCAT(DISTINCT s.estado)                     AS "Estados"
        FROM pedidos p
        JOIN subpedidos s ON s.id_pedido = p.id_pedido
        WHERE p.scraping_completo = 1
          AND EXISTS (
              SELECT 1 FROM subpedidos s2
              WHERE s2.id_pedido = p.id_pedido
                AND LOWER(s2.estado) NOT IN ('completado','cancelado','comentado')
          )
          {filtro_tipo}
          {filtro_estado}
          {filtro_almacen}
        GROUP BY p.id_pedido
        ORDER BY CAST(
            JULIANDAY(DATE('now')) - JULIANDAY(p.fecha) AS INTEGER
        ) DESC, p.id_pedido DESC
    """

    con = _conn()
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    df["⚠ Dif."] = df.pop("_hay_diferencia").map({1: "Sí", 0: ""})
    return df


@st.cache_data(ttl=7200, show_spinner=False)
def get_detalle_operacional(
    id_pedido: str,
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
    tipos_sub: tuple[str, ...],
) -> pd.DataFrame:
    """Retorna subpedidos y líneas de un pedido con filtros operacionales.

    Args:
        id_pedido: ID del pedido a detallar.
        estados_sub: Tupla de estados de subpedido a incluir. Vacío = todos.
        almacenes: Tupla de almacenes a incluir. Vacío = todos.
        tipos_sub: Tupla de tipos de subpedido a incluir. Vacío = todos.

    Returns:
        DataFrame con una fila por línea de producto, ordenado por subpedido
        y nombre de producto.
    """
    filtro_tipo = ""
    params_tipo: list = []
    if tipos_sub:
        placeholders = ",".join("?" * len(tipos_sub))
        filtro_tipo = f"AND s.tipo_subpedido IN ({placeholders})"
        params_tipo = list(tipos_sub)

    filtro_estado = ""
    params_estado: list = []
    if estados_sub:
        placeholders = ",".join("?" * len(estados_sub))
        filtro_estado = f"AND s.estado IN ({placeholders})"
        params_estado = list(estados_sub)

    filtro_almacen = ""
    params_almacen: list = []
    if almacenes:
        placeholders = ",".join("?" * len(almacenes))
        filtro_almacen = f"AND l.almacen IN ({placeholders})"
        params_almacen = list(almacenes)

    monto_a_pagar = "l.monto_pagar_num" if _NUM_COLS_EXIST else "NULL"
    monto_final   = "l.monto_final_num"  if _NUM_COLS_EXIST else "NULL"

    sql = f"""
        SELECT
            s.numero_subpedido          AS "Subpedido",
            s.tipo_subpedido            AS "Tipo",
            s.estado                    AS "Estado subpedido",
            s.alistador                 AS "Alistador",
            s.inspector                 AS "Inspector",
            l.almacen                   AS "Almacén",
            l.nombre_producto           AS "Producto",
            l.referencia                AS "Referencia",
            l.presentacion              AS "Presentación",
            l.cantidad_comprada         AS "Comprometido",
            l.cantidad_entregada        AS "Entregado",
            (l.cantidad_comprada
             - l.cantidad_entregada)    AS "Pendiente",
            {monto_a_pagar}             AS "Monto a pagar",
            {monto_final}               AS "Monto final",
            l.observaciones             AS "Observaciones"
        FROM lineas_pedido l
        JOIN subpedidos s
            ON l.id_pedido = s.id_pedido
            AND l.numero_subpedido = s.numero_subpedido
            {filtro_tipo}
            {filtro_estado}
        WHERE l.id_pedido = ?
          {filtro_almacen}
          AND l.nombre_producto IS NOT NULL
          AND l.nombre_producto != ''
        ORDER BY s.numero_subpedido, l.nombre_producto
    """

    params = params_tipo + params_estado + [id_pedido] + params_almacen
    con = _conn()
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    return df


@st.cache_data(ttl=7200, show_spinner=False)
def get_detalle_pedido(
    id_pedido: str,
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
) -> pd.DataFrame:
    filtro_estado_sub_en_join = ""
    params_sub: list = []
    if estados_sub:
        placeholders = ",".join("?" * len(estados_sub))
        filtro_estado_sub_en_join = f"AND s.estado IN ({placeholders})"
        params_sub = list(estados_sub)

    filtro_almacen_en_where = ""
    params_alm: list = []
    if almacenes:
        placeholders = ",".join("?" * len(almacenes))
        filtro_almacen_en_where = f"AND l.almacen IN ({placeholders})"
        params_alm = list(almacenes)

    monto_a_pagar = "l.monto_pagar_num" if _NUM_COLS_EXIST else "NULL"
    monto_final = "l.monto_final_num" if _NUM_COLS_EXIST else "NULL"

    sql = f"""
        SELECT
            s.numero_subpedido              AS Subpedido,
            s.estado                        AS "Estado subpedido",
            s.alistador                     AS Alistador,
            s.inspector                     AS Inspector,
            l.almacen                       AS Almacén,
            l.nombre_producto               AS Producto,
            l.referencia                    AS Referencia,
            l.presentacion                  AS Presentación,
            l.cantidad_comprada             AS Comprometido,
            l.cantidad_entregada            AS Entregado,
            (l.cantidad_comprada
             - l.cantidad_entregada)        AS Pendiente,
            {monto_a_pagar}                 AS "Monto a pagar",
            {monto_final}                   AS "Monto final",
            l.observaciones                 AS Observaciones
        FROM lineas_pedido l
        JOIN subpedidos s ON l.id_pedido = s.id_pedido
                         AND l.numero_subpedido = s.numero_subpedido
                         {filtro_estado_sub_en_join}
        WHERE l.id_pedido = ?
          {filtro_almacen_en_where}
          AND l.nombre_producto IS NOT NULL
          AND l.nombre_producto != ''
        ORDER BY s.numero_subpedido, l.nombre_producto
    """

    params = params_sub + [id_pedido] + params_alm
    con = _conn()
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    return df