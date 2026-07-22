import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# AUD-M5/M8 (auditoría 2026-07-01): ESTADOS_CERRADOS vive en el módulo
# común — único origen de verdad. El insert de sys.path permite resolver
# `comun` cuando Streamlit ejecuta la app con dashboard/ como directorio
# del script.
sys.path.insert(0, str(Path(__file__).parent.parent))
from comun import ESTADOS_CERRADOS

DB_PATH = Path(__file__).parent.parent / "data" / "pedidos.db"
# Lista de estados de cierre lista para interpolar en IN (...) / NOT IN (...).
# sorted() para SQL determinístico (frozenset no garantiza orden).
_cerr = ",".join(f"'{e}'" for e in sorted(ESTADOS_CERRADOS))

# AUD-B6: Colombia opera en UTC-5 sin horario de verano desde 1993 — un
# offset fijo es correcto todo el año y no depende de la zona horaria
# configurada en el SO donde corre el dashboard (a diferencia del
# modificador 'localtime' de SQLite). `pedidos.fecha` es la fecha que
# muestra la SPA (hora local Colombia, sin TZ explícita) mientras que
# `estado_cambiado_en` se guarda en UTC (persistencia.py) — antes
# `JULIANDAY(DATE('now'))` (UTC) se restaba contra `p.fecha` (local),
# desviando "Días abierto"/"Días sin mov." hasta 1 día durante la ventana
# diaria de 5h en que la fecha UTC ya cambió y la de Colombia todavía no.
_HOY_CO = "DATE('now', '-5 hours')"

# AUD-M12: único TTL para todo el módulo — antes 7200s en las queries y
# 300s en los checks de esquema, una inconsistencia sin motivo (el peor
# caso de datos viejos llegaba a ~4h: caché + ciclo del scheduler). Con el
# scheduler corriendo cada 1h (DEC-030), 600s deja los datos del
# dashboard a lo sumo ~70 min desactualizados en el peor caso.
_CACHE_TTL_S = 600


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def _num_cols_exist() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        # AUD-M6 (defensa secundaria): timeout=5 — la causa raíz de la
        # ventana "no such view" quedó resuelta en el ETL (DEC-019); esto
        # cubre residuales tipo "database is locked" por contención normal
        # con el ETL/scraper escribiendo, en vez de fallar de inmediato.
        con = sqlite3.connect(DB_PATH, timeout=5)
        cols = [r[1] for r in con.execute("PRAGMA table_info(lineas_pedido)")]
        con.close()
        return "monto_pagar_num" in cols
    except Exception:
        return False


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def _view_consolidado_exists() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        exists = (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='view' AND name='v_inventario_comprometido'"
            ).fetchone()[0]
            > 0
        )
        con.close()
        return exists
    except Exception:
        return False


COLS_CONSOLIDADO = [
    "Producto",
    "Referencia",
    "Presentación",
    "Almacén",
    "Estado subpedido",
    "Comprometido",
    "Entregado",
    "Pendiente",
    "Pedidos con stock",
]


def _check_db() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Base de datos no encontrada: {DB_PATH.resolve()}\n"
            "Ejecuta el scraper primero: py scraper/scraper_principal.py --desde 2026-01-01"
        )


def _conn() -> sqlite3.Connection:
    _check_db()
    # AUD-M6 (defensa secundaria): timeout=5 — espera hasta 5s si la DB
    # está momentáneamente bloqueada (ETL/scraper escribiendo) en vez de
    # lanzar "database is locked" de inmediato. La causa raíz de la
    # ventana "no such view" ya quedó resuelta en el ETL (DEC-019); esto
    # cubre el residual de contención normal entre lector y escritor.
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ultima_actualizacion() -> str | None:
    """MAX(actualizado_en) de pedidos — AUD-M12: indicador de frescura de
    datos para el header del dashboard, dado que no hay botón de refresco
    manual y el caché puede mostrar datos de hasta ~70 min de antigüedad."""
    con = _conn()
    try:
        row = con.execute("SELECT MAX(actualizado_en) FROM pedidos").fetchone()
    finally:
        con.close()
    return row[0] if row else None


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
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


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_consolidado(
    estados_sub: tuple[str, ...],
    almacenes: tuple[str, ...],
) -> pd.DataFrame:
    con = _conn()
    try:
        view_exists = (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='view' AND name='v_inventario_comprometido'"
            ).fetchone()[0]
            > 0
        )
        if not view_exists:
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
        return df
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
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
        filtro_estado_pedido = f"""AND EXISTS (
            SELECT 1 FROM subpedidos s2
            WHERE s2.id_pedido = p.id_pedido
              AND LOWER(s2.estado) NOT IN ({_cerr})
        )"""
    elif estado_pedido == "Cerrados":
        filtro_estado_pedido = f"""AND NOT EXISTS (
            SELECT 1 FROM subpedidos s2
            WHERE s2.id_pedido = p.id_pedido
              AND LOWER(s2.estado) NOT IN ({_cerr})
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
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    df["⚠ Diferencia"] = df.pop("_hay_diferencia").map({1: "Sí", 0: ""})
    return df


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
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
                JULIANDAY({_HOY_CO}) - JULIANDAY(p.fecha)
                AS INTEGER
            )                                                   AS "Días abierto",
            CAST(
                JULIANDAY({_HOY_CO})
                - JULIANDAY(DATE(
                    MIN(CASE
                        WHEN LOWER(s.estado) NOT IN ({_cerr})
                        THEN s.estado_cambiado_en
                        ELSE NULL
                    END),
                    '-5 hours'
                ))
                AS INTEGER
            )                                                   AS "Días sin mov.",
            (
                SELECT sub2.estado
                FROM subpedidos sub2
                WHERE sub2.id_pedido = p.id_pedido
                  AND LOWER(sub2.estado) NOT IN ({_cerr})
                ORDER BY sub2.estado_cambiado_en ASC NULLS LAST,
                         sub2.numero_subpedido   ASC
                LIMIT 1
            )                                                   AS "Estado estancado",
            COUNT(DISTINCT s.numero_subpedido)                  AS "Subpedidos",
            SUM(CASE WHEN LOWER(s.estado) NOT IN
                ({_cerr})
                THEN 1 ELSE 0 END)                              AS "Sub. abiertos",
            GROUP_CONCAT(DISTINCT s.estado)                     AS "Estados"
        FROM pedidos p
        JOIN subpedidos s ON s.id_pedido = p.id_pedido
        WHERE p.scraping_completo = 1
          AND EXISTS (
              SELECT 1 FROM subpedidos s2
              WHERE s2.id_pedido = p.id_pedido
                AND LOWER(s2.estado) NOT IN ({_cerr})
          )
          {filtro_tipo}
          {filtro_estado}
          {filtro_almacen}
        GROUP BY p.id_pedido
        ORDER BY CAST(
            JULIANDAY({_HOY_CO}) - JULIANDAY(p.fecha) AS INTEGER
        ) DESC, p.id_pedido DESC
    """

    con = _conn()
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    df["⚠ Dif."] = df.pop("_hay_diferencia").map({1: "Sí", 0: ""})
    return df


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
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

    _use_view = _num_cols_exist()
    _lineas_src = "v_lineas_pedido_num" if _use_view else "lineas_pedido"
    monto_a_pagar = "l.monto_pagar" if _use_view else "NULL"
    monto_final = "l.monto_final" if _use_view else "NULL"

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
        FROM {_lineas_src} l
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
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    return df


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
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

    _use_view = _num_cols_exist()
    _lineas_src = "v_lineas_pedido_num" if _use_view else "lineas_pedido"
    monto_a_pagar = "l.monto_pagar" if _use_view else "NULL"
    monto_final = "l.monto_final" if _use_view else "NULL"

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
        FROM {_lineas_src} l
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
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    return df
