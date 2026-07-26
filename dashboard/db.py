import io
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
from comun import ESTADOS_ACTIVOS_INVENTARIO, ESTADOS_CERRADOS

DB_PATH = Path(__file__).parent.parent / "data" / "pedidos.db"
# Lista de estados de cierre lista para interpolar en IN (...) / NOT IN (...).
# sorted() para SQL determinístico (frozenset no garantiza orden).
_cerr = ",".join(f"'{e}'" for e in sorted(ESTADOS_CERRADOS))

# DEC-045: el subfiltro "previos a picking" replica la definición de
# DEC-039 pregunta 5 (corregida por HAL-013), que son DOS condiciones:
# el subpedido está activo para inventario **y** su alistamiento físico no
# terminó. Con solo `inicio_inspeccion='-'` se colarían los cancelados
# (5.738 nunca se alistaron porque murieron antes) y algunos completados.
_activos_inv = ",".join(f"'{e}'" for e in ESTADOS_ACTIVOS_INVENTARIO)

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


def _objeto_existe(nombre: str, tipo: str = "table") -> bool:
    """True si existe la tabla o VIEW indicada."""
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        existe = (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type=? AND name=?", (tipo, nombre)
            ).fetchone()[0]
            > 0
        )
        con.close()
        return existe
    except Exception:
        return False


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_corrida() -> dict | None:
    """Metadatos de la última corrida del cruce de inventario (DEC-043).

    Trae la frescura de las tres fuentes y los agregados, para el encabezado
    de la vista. `None` si el cruce nunca corrió.
    """
    if not _objeto_existe("v_inventario_corridas", "view"):
        return None
    con = _conn()
    try:
        df = pd.read_sql("SELECT * FROM v_inventario_corridas ORDER BY id DESC LIMIT 1", con)
    finally:
        con.close()
    return None if df.empty else df.iloc[0].to_dict()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_tendencia() -> pd.DataFrame:
    """Sobrante en altura por día, para seguir si crece o se estabiliza (DEC-052).

    Una fila por día, con la **última corrida** de cada uno. El scheduler
    corre cada hora, así que promediar mezclaría fotos del mismo día;
    quedarse con la última da el estado con que cerró la jornada.

    La fecha se pasa a hora de Colombia (UTC−5, AUD-B6): `ejecutado_en` se
    guarda en UTC, y sin convertir las corridas de la noche caerían en el
    día siguiente.
    """
    if not _objeto_existe("v_inventario_corridas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT DATE(ejecutado_en, '-5 hours') AS dia,
                      MAX(ejecutado_en)              AS ultima_corrida,
                      COUNT(*)                       AS corridas,
                      sobrante_referencias,
                      sobrante_unidades,
                      inventario_teorico,
                      bochica_altura
               FROM v_inventario_corridas
               WHERE sobrante_unidades IS NOT NULL
               GROUP BY dia
               ORDER BY dia""",
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_comparacion() -> pd.DataFrame:
    """Comparación por referencia: teórico vs. lo que reporta Bochica (DEC-041).

    `picking_estimado` = `inventario_teorico` − `bochica_altura`. Negativo
    significa que la fuente confiable (altura) ya supera al teórico sin
    contar picking — es un hallazgo a explicar, no una métrica cerrada
    (decisión del Arquitecto, DEC-043).
    """
    if not _objeto_existe("v_inventario_comparacion", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_comparacion", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_salud() -> pd.DataFrame:
    """Cobertura, movimiento y riesgo por referencia (DEC-049).

    Lo calcula el scheduler porque agregar la demanda sobre 840.000 líneas
    cuesta ~10 s: acá la consulta es inmediata.
    """
    if not _objeto_existe("v_inventario_salud", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_salud", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_abc() -> pd.DataFrame:
    """Clasificación ABC-XYZ por referencia (DEC-050)."""
    if not _objeto_existe("v_inventario_abc", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_abc", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_anomalias() -> pd.DataFrame:
    """Stock ubicado donde el layout dice que no debería haber (DEC-041)."""
    if not _objeto_existe("v_inventario_anomalias", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT motivo, ubicacion, id_especificacion, cantidad
               FROM v_inventario_anomalias ORDER BY cantidad DESC""",
            con,
        )
    finally:
        con.close()


def _tabla_existe(nombre: str) -> bool:
    """True si la tabla existe. Sin caché: se consulta al abrir la app."""
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        existe = (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (nombre,)
            ).fetchone()[0]
            > 0
        )
        con.close()
        return existe
    except Exception:
        return False


def get_hallazgos() -> pd.DataFrame:
    """Inconsistencias detectadas en la última corrida del scheduler (DEC-047).

    Solo trae las que **todavía tienen casos**: `inventario/persistencia.py`
    reescribe la tabla en cada corrida y no inserta los detectores que no
    encontraron nada. Por eso una tarea resuelta en el sistema
    administrativo desaparece sola.

    Sin `st.cache_data`: son pocas filas y cachearlas retrasaría hasta
    10 minutos el reflejo de una corrección recién hecha en el origen,
    que es justo lo que el módulo promete mostrar al día.
    """
    if not _tabla_existe("tareas_hallazgos"):
        return pd.DataFrame(
            columns=[
                "clave",
                "titulo",
                "explicacion",
                "categoria",
                "prioridad",
                "origen",
                "unidad",
                "cantidad",
                "medido_en",
            ]
        )
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT clave, titulo, explicacion, categoria, prioridad, origen,
                      unidad, cantidad, medido_en
               FROM tareas_hallazgos
               ORDER BY CASE prioridad WHEN 'Alta' THEN 0 WHEN 'Media' THEN 1 ELSE 2 END,
                        cantidad DESC""",
            con,
        )
    finally:
        con.close()


def get_detalle_hallazgo(clave: str) -> pd.DataFrame:
    """Listado de casos concretos de un hallazgo — el detalle de la tarea.

    El detalle se guarda serializado porque cada detector tiene sus
    propias columnas (un código de barras con sus IDs no se parece a un
    estado con su conteo). Son cientos de filas, así que deserializar
    completo es barato y evita inventar un esquema genérico que no le
    quede bien a ninguno.
    """
    if not _tabla_existe("tareas_hallazgos"):
        return pd.DataFrame()
    con = _conn()
    try:
        fila = con.execute(
            "SELECT detalle FROM tareas_hallazgos WHERE clave = ?", (clave,)
        ).fetchone()
    finally:
        con.close()
    if not fila or not fila[0]:
        return pd.DataFrame()
    return pd.read_json(io.StringIO(fila[0]), orient="split")


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_rango_fechas() -> tuple[str | None, str | None]:
    """Primera y última fecha con pedidos — para acotar el selector de rango."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT MIN(fecha), MAX(fecha) FROM pedidos WHERE fecha IS NOT NULL AND fecha != ''"
        ).fetchone()
    finally:
        con.close()
    return (row[0], row[1]) if row else (None, None)


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_pedidos_consolidado(
    fecha_desde: str,
    fecha_hasta: str,
    estado_pedido: str,
    almacenes: tuple[str, ...],
    solo_previos_picking: bool,
    limite: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Tabla consolidada de líneas de pedido, una fila por línea (DEC-045).

    El `LEFT JOIN` contra `catalogo_productos` aporta el ID del producto,
    que `lineas_pedido` no tiene. Es LEFT y no INNER a propósito: el 4,1%
    de líneas sin ID en el catálogo (códigos legados o pares ambiguos)
    debe seguir viéndose, con el ID vacío, en vez de desaparecer de un
    consolidado que el analista va a sumar. Y la tabla puente guarda solo
    pares inequívocos, así que el join **no puede multiplicar filas** —
    verificado: 840.179 líneas antes y después.

    Args:
        fecha_desde: Fecha inicial inclusive (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive (YYYY-MM-DD).
        estado_pedido: "Todos", "Abiertos" o "Cerrados" — clasifica el
            pedido padre según tenga o no algún subpedido abierto.
        almacenes: Almacenes a incluir; vacío = todos.
        solo_previos_picking: Si True, deja solo subpedidos cuyo
            alistamiento físico no terminó (`inicio_inspeccion = '-'`,
            DEC-039 pregunta 5 corregida por HAL-013) — la misma
            definición que usa el cruce de inventario.
        limite: Máximo de filas a traer. Los agregados se calculan en SQL
            sobre el conjunto completo, no sobre el recorte: mezclar un
            total con métricas derivadas de la muestra daría cifras que
            parecen del rango pedido pero son de las primeras N filas.

    Returns:
        `(DataFrame recortado, agregados del conjunto completo)`. Los
        agregados traen `lineas`, `pedidos`, `referencias`, `cantidad` y
        `sin_id`.
    """
    filtros = ["p.fecha BETWEEN ? AND ?"]
    params: list[object] = [fecha_desde, fecha_hasta]

    if estado_pedido == "Abiertos":
        filtros.append(
            f"EXISTS (SELECT 1 FROM subpedidos s2 WHERE s2.id_pedido = p.id_pedido "
            f"AND LOWER(s2.estado) NOT IN ({_cerr}))"
        )
    elif estado_pedido == "Cerrados":
        filtros.append(
            f"NOT EXISTS (SELECT 1 FROM subpedidos s2 WHERE s2.id_pedido = p.id_pedido "
            f"AND LOWER(s2.estado) NOT IN ({_cerr}))"
        )

    if almacenes:
        filtros.append(f"l.almacen IN ({','.join('?' * len(almacenes))})")
        params.extend(almacenes)

    if solo_previos_picking:
        filtros.append(f"LOWER(s.estado) IN ({_activos_inv}) AND s.inicio_inspeccion = '-'")

    where = " AND ".join(filtros)
    base = f"""
        FROM lineas_pedido l
        JOIN pedidos p    ON p.id_pedido = l.id_pedido
        JOIN subpedidos s ON s.id_pedido = l.id_pedido
                         AND s.numero_subpedido = l.numero_subpedido
        LEFT JOIN catalogo_productos c
               ON c.referencia    = TRIM(l.referencia)
              AND c.codigo_barras = TRIM(l.codigo_barras)
        WHERE {where}
    """

    con = _conn()
    try:
        fila = con.execute(
            f"""SELECT COUNT(*),
                       COUNT(DISTINCT p.id_pedido),
                       COUNT(DISTINCT l.referencia),
                       COALESCE(SUM(l.cantidad_comprada), 0),
                       SUM(CASE WHEN c.id_producto IS NULL THEN 1 ELSE 0 END)
                {base}""",
            params,
        ).fetchone()
        agregados = {
            "lineas": int(fila[0]),
            "pedidos": int(fila[1]),
            "referencias": int(fila[2]),
            "cantidad": float(fila[3]),
            "sin_id": int(fila[4]),
        }
        df = pd.read_sql(
            f"""
            SELECT p.fecha              AS "Fecha del pedido",
                   p.hora               AS "Hora",
                   p.id_pedido          AS "Pedido padre",
                   s.numero_subpedido   AS "Número de subpedido",
                   s.tipo_subpedido     AS "Tipo de subpedido",
                   s.estado             AS "Estado del subpedido",
                   l.almacen            AS "Almacén",
                   c.id_producto        AS "ID del producto",
                   l.referencia         AS "Referencia",
                   l.codigo_barras      AS "Código de barras",
                   l.presentacion       AS "Presentación",
                   l.cantidad_comprada  AS "Cantidad comprada"
            {base}
            ORDER BY p.fecha DESC, p.hora DESC, p.id_pedido, s.numero_subpedido
            LIMIT ?
            """,
            con,
            params=[*params, limite],
        )
    finally:
        con.close()
    return df, agregados


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
