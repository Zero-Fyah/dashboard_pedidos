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
from comun import (
    ESTADOS_CERRADOS,
    ESTADOS_DESPACHADOS,
    ESTADOS_PREVIOS_PICKING,
    PRODUCTOS_RELLENO,
    VIGENCIA_ACTIVO,
    clasificar_modalidad_arena,
)

DB_PATH = Path(__file__).parent.parent / "data" / "pedidos.db"
# Lista de estados de cierre lista para interpolar en IN (...) / NOT IN (...).
# sorted() para SQL determinístico (frozenset no garantiza orden).
_cerr = ",".join(f"'{e}'" for e in sorted(ESTADOS_CERRADOS))
# DEC-069: cerrados menos cancelado — un subpedido cancelado no salió corto.
_despachados = ",".join(f"'{e}'" for e in sorted(ESTADOS_DESPACHADOS))

# DEC-045/DEC-109: el subfiltro "previos a picking" son DOS condiciones: el
# subpedido está en uno de los 3 estados de ESTADOS_PREVIOS_PICKING **y** su
# alistamiento físico no terminó. Con solo `inicio_inspeccion='-'` se
# colarían los cancelados (5.738 nunca se alistaron porque murieron antes) y
# algunos completados. DEC-109 angostó la lista de estados de las 12 de
# ESTADOS_ACTIVOS_INVENTARIO a estas 3: "pendiente de entrega" y "en
# inspección" ya pasaron por alistamiento, y "pendiente de confirmación"
# todavía no compromete mercancía — ninguno de los tres debería tener nada
# físicamente en picking.
_previos_picking = ",".join(f"'{e}'" for e in ESTADOS_PREVIOS_PICKING)

# DEC-115/DEC-117: líneas de relleno para el monto mínimo, listas para
# interpolar en IN (...) / NOT IN (...) — mismo patrón que _cerr arriba.
_relleno = ",".join(f"'{p}'" for p in PRODUCTOS_RELLENO)

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
def get_opciones_comerciales() -> dict[str, list[str]]:
    """Valores disponibles para los filtros globales (DEC-101).

    Se excluyen los placeholders del origen (`-`, vacío) porque ofrecerlos como
    opción de filtro no sirve para nada: nadie quiere ver "los pedidos cuyo
    vendedor es un guion".

    Returns:
        Un dict listo para `filtros.barra_lateral()`, con las cuatro
        dimensiones ordenadas alfabéticamente.
    """
    con = _conn()
    try:
        columnas = {
            "vendedores": "vendedor",
            "clientes": "nombre_empresa",
            "canales": "metodo_entrega",
            "formas_pago": "forma_pago",
        }
        salida: dict[str, list[str]] = {}
        for clave, col in columnas.items():
            salida[clave] = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT {col} FROM pedidos "  # noqa: S608
                    f"WHERE {col} IS NOT NULL AND TRIM({col}) NOT IN ('', '-') "
                    f"ORDER BY {col}"
                )
            ]
    finally:
        con.close()
    return salida


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

    DEC-065: si falta `vigencia`, se asume todo vigente. La columna llega a
    las bases existentes por la migración de `inventario.persistencia`, que
    corre en la próxima corrida del scheduler; entre el deploy y esa corrida
    la página tiene que abrir igual. El default es `Activo` —no NULL— para
    que degrade al comportamiento previo a DEC-065 (catálogo completo) en
    vez de mostrar una página vacía.
    """
    if not _objeto_existe("v_inventario_salud", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        df = pd.read_sql("SELECT * FROM v_inventario_salud", con)
    finally:
        con.close()
    if "vigencia" not in df.columns:
        df["vigencia"] = VIGENCIA_ACTIVO
    else:
        df["vigencia"] = df["vigencia"].fillna(VIGENCIA_ACTIVO)
    return df


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_arena_inventario() -> pd.DataFrame:
    """Inventario de la categoría Arena por código de barras/modalidad/ciudad (DEC-118)."""
    if not _objeto_existe("v_arena_inventario", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_arena_inventario", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_arena_balance() -> pd.DataFrame:
    """Proporción histórica de bolsas vendidas por modalidad (DEC-118) — medida, no asumida.

    Se mide sobre `lineas_pedido`, que trae `referencia` con el esquema
    previo a la migración a `PRA ARENA TONELADA`/`PRA13` nacionales —
    `comun.clasificar_modalidad_arena()` reconoce ambos esquemas (100% de
    cobertura verificada sobre 113.807 líneas de Arena, DEC-118).
    `cantidad_comprada` es cantidad de bolsas, no kilogramos — verificado
    que las tres modalidades usan la misma unidad antes de compararlas.

    Returns:
        Una fila por modalidad (`modalidad`, `bolsas`), más `cobertura`
        (fracción de líneas que el clasificador reconoció) repetida en
        cada fila, para que la página la declare junto a la cifra.
    """
    if not _objeto_existe("arena_inventario"):
        return pd.DataFrame()
    con = _conn()
    try:
        codigos = pd.read_sql("SELECT DISTINCT codigo_barras FROM arena_inventario", con)
        if codigos.empty:
            return pd.DataFrame()
        marcas = ",".join("?" * len(codigos))
        lp = pd.read_sql(
            f"SELECT referencia, cantidad_comprada FROM lineas_pedido "
            f"WHERE codigo_barras IN ({marcas})",
            con,
            params=codigos["codigo_barras"].tolist(),
        )
    finally:
        con.close()

    if lp.empty:
        return pd.DataFrame()

    lp["modalidad"] = lp["referencia"].map(clasificar_modalidad_arena)
    lp["cantidad_comprada"] = pd.to_numeric(lp["cantidad_comprada"], errors="coerce")
    cobertura = float(lp["modalidad"].notna().mean())
    resumen = (
        lp[lp["modalidad"].notna()]
        .groupby("modalidad")["cantidad_comprada"]
        .sum()
        .rename("bolsas")
        .reset_index()
    )
    resumen["cobertura"] = cobertura
    return resumen


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_despacho_diario() -> pd.DataFrame:
    """Cumplimiento de despacho por día — la mitad "in full" de E.1 (DEC-065).

    La auditoría del 2026-07-29 dio E.1 por bloqueada "sin fecha
    comprometida (OTIF)". Cierto para la mitad *on time*; falso para la
    mitad *in full*, que solo necesita comparar lo pedido con lo
    entregado — y eso ya está en `detalle_diferencias` desde siempre.

    `hay_diferencia` es el marcador del pedido: el sistema origen factura
    de menos cuando algo no se despachó completo. Medido sobre la base
    real: 4.264 de 29.427 pedidos, todos en la misma dirección (nunca se
    facturó de más).

    Returns:
        Una fila por fecha con pedidos, pedidos con faltante y el monto
        que no se despachó. Vacío si el ETL todavía no creó la VIEW.
    """
    if not _objeto_existe("v_diferencias_resumen", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        # DEC-067: el monto entra por un LEFT JOIN contra una subconsulta ya
        # agregada. La versión anterior usaba una subconsulta **correlacionada**
        # —se ejecutaba una vez por cada fecha del GROUP BY— y tardaba 29,6 s
        # para devolver 211 filas. Así son 0,4 s: 74× más rápido, resultado
        # idéntico (verificado con DataFrame.equals, no a ojo).
        return pd.read_sql(
            """SELECT p.fecha,
                      COUNT(*) AS pedidos,
                      SUM(CASE WHEN p.hay_diferencia = 1 THEN 1 ELSE 0 END)
                          AS pedidos_con_faltante,
                      COALESCE(MAX(d.monto), 0) AS monto_no_despachado
                 FROM pedidos p
                 LEFT JOIN (
                      SELECT fecha, SUM(monto_diferencia_num) AS monto
                        FROM v_diferencias_resumen
                    GROUP BY fecha
                 ) d ON d.fecha = p.fecha
                WHERE p.scraping_completo = 1
                  AND p.fecha IS NOT NULL AND p.fecha != ''
             GROUP BY p.fecha
             ORDER BY p.fecha""",
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_despacho_faltantes(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Detalle línea a línea de lo que no se despachó (DEC-069).

    **Sale de `lineas_pedido`, no de `detalle_diferencias`.** La versión de
    DEC-065 usaba esta última, que no tiene `referencia` ni `codigo_barras`:
    dejaba decir *cuánto* faltó pero no *qué SKU, de qué clase, desde qué
    posición* — justo lo único que convierte el dato en una acción de bodega.
    `lineas_pedido` los tiene en el 100% de las líneas con faltante (medido:
    4.782 de 4.782) y da cifras equivalentes.

    Se cuentan solo los subpedidos **despachados**: un cancelado no salió
    corto, no salió. En un subpedido todavía abierto, `entregada < comprada`
    es lo normal, no un faltante.

    Se acota por rango en SQL, no en pandas: traer los 7 meses cuesta 2,0 s
    contra 0,6 s del mes que la página muestra por defecto (DEC-069).

    Args:
        fecha_desde: Fecha inicial inclusive (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive (YYYY-MM-DD).

    Returns:
        Una fila por línea con faltante, con fecha, referencia y código de
        barras para cruzar contra el catálogo, el ABC y las ubicaciones.
        Vacío si el scraper todavía no creó las tablas.
    """
    if not _objeto_existe("lineas_pedido"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            f"""SELECT p.fecha,
                       l.id_pedido,
                       l.numero_subpedido,
                       l.referencia,
                       l.codigo_barras,
                       l.nombre_producto,
                       l.almacen,
                       l.cantidad_comprada,
                       COALESCE(l.cantidad_entregada, 0) AS cantidad_entregada,
                       l.cantidad_comprada - COALESCE(l.cantidad_entregada, 0) AS faltante
                  FROM lineas_pedido l
                  JOIN subpedidos s ON s.id_pedido = l.id_pedido
                                   AND s.numero_subpedido = l.numero_subpedido
                  JOIN pedidos p    ON p.id_pedido = l.id_pedido
                 WHERE LOWER(s.estado) IN ({_despachados})
                   -- DEC-103: COALESCE obligatorio. `NULL < 10` no es true, así
                   -- que una línea de la que no se entregó **nada** quedaría
                   -- fuera del listado de faltantes — el peor lugar donde
                   -- perderla. Es el mismo FIX C-4 de 2026-07-01, que vivía en
                   -- `v_inventario_comprometido` y se destapó acá al migrar su
                   -- test en vez de borrarlo con la VIEW.
                   AND COALESCE(l.cantidad_entregada, 0) < l.cantidad_comprada
                   AND p.fecha BETWEEN ? AND ?""",  # noqa: S608
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_despacho_lineas_diario(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Fill rate por línea y por unidad, por fecha (DEC-069).

    El indicador por pedido es el más exigente —una sola línea corta arruina
    el pedido entero— y por eso da 85,5% mientras el de línea da 99,27%. Los
    dos son correctos y estándar; publicar solo el primero hace ver un
    problema sistémico donde hay uno concentrado.

    Args:
        fecha_desde: Fecha inicial inclusive (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive (YYYY-MM-DD).

    Returns:
        Una fila por fecha con líneas despachadas, líneas con faltante,
        unidades pedidas y unidades faltantes. Vacío si el scraper todavía
        no creó las tablas.
    """
    if not _objeto_existe("lineas_pedido"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            f"""SELECT p.fecha,
                       COUNT(*) AS lineas,
                       -- DEC-103: sin COALESCE, una línea con entregada NULL
                       -- cae al ELSE y no se cuenta como faltante.
                       SUM(CASE WHEN COALESCE(l.cantidad_entregada, 0)
                                     < l.cantidad_comprada
                                THEN 1 ELSE 0 END) AS lineas_con_faltante,
                       SUM(l.cantidad_comprada) AS unidades_pedidas,
                       SUM(l.cantidad_comprada - COALESCE(l.cantidad_entregada, 0))
                           AS unidades_faltantes
                  FROM lineas_pedido l
                  JOIN subpedidos s ON s.id_pedido = l.id_pedido
                                   AND s.numero_subpedido = l.numero_subpedido
                  JOIN pedidos p    ON p.id_pedido = l.id_pedido
                 WHERE LOWER(s.estado) IN ({_despachados})
                   AND p.fecha BETWEEN ? AND ?
              GROUP BY p.fecha
              ORDER BY p.fecha""",  # noqa: S608
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_entregas(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Compromiso de entrega contra entrega real — la mitad *on time* (DEC-095).

    `CLAUDE.md` daba esta mitad de E.1 por imposible "sin fecha comprometida en
    el origen". El compromiso está en `pedidos.hora_entrega` desde siempre
    (DEC-093) y la entrega real en el evento `Entrega` de
    `registro_operaciones`; nadie los había cruzado.

    **El evento de entrega está validado contra el origen** (DEC-094): coincide
    al segundo en 3 de 3 pedidos sondeados, no se marca en lote —se distribuye
    como jornada real, 1 evento en domingo sobre 25.495— y su tiempo
    envío→entrega se separa por canal como manda la física (Ruta 2,0 d,
    Transportadora 5,0 d). No es un sello administrativo.

    Se toma `MIN(momento)`: 1.685 pedidos tienen más de un evento `Entrega`
    —reintentos tras una entrega fallida— y lo que se mide es cuándo llegó, no
    cuántas veces se intentó.

    El `LEFT JOIN` contra la entrega es deliberado: un pedido **prometido y no
    entregado** es el caso accionable de hoy, y un `INNER JOIN` lo borraría.

    La clasificación no se hace acá sino en `comun.entregas`, que es puro y
    testeable; esta capa solo trae el texto crudo del compromiso y el contexto.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con el compromiso sin parsear, el momento de
        entrega (NaN si no hay), las reprogramaciones y el valor del pedido.
    """
    if not _objeto_existe("registro_operaciones"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT p.id_pedido,
                      p.fecha,
                      p.metodo_entrega,
                      p.forma_pago,
                      p.nombre_empresa,
                      p.vendedor,
                      p.hora_entrega,
                      p.despachador,
                      e.entregado_en,
                      COALESCE(u.reprogramaciones, 0) AS reprogramaciones,
                      COALESCE(v.valor, 0)            AS valor
                 FROM pedidos p
                 LEFT JOIN (
                     SELECT id_pedido, MIN(momento) AS entregado_en
                       FROM registro_operaciones
                      WHERE accion = 'Entrega'
                   GROUP BY id_pedido
                 ) e ON e.id_pedido = p.id_pedido
                 LEFT JOIN (
                     SELECT id_pedido, COUNT(*) AS reprogramaciones
                       FROM registro_operaciones
                      WHERE accion = 'Actualizar hora de entrega'
                   GROUP BY id_pedido
                 ) u ON u.id_pedido = p.id_pedido
                 LEFT JOIN (
                     SELECT id_pedido, MAX(monto_final_num) AS valor
                       FROM estadisticas_monto
                      WHERE concepto_base = 'Total a pagar / Total final a pagar'
                   GROUP BY id_pedido
                 ) v ON v.id_pedido = p.id_pedido
                WHERE p.scraping_completo = 1
                  AND p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_scorecard() -> dict[str, float | None]:
    """Cifras de pedidos, servicio y caja para la portada (DEC-102).

    El scorecard tenía **siete métricas y las siete eran de inventario**,
    ignorando 29.931 pedidos, $84.088 M y 227.666 eventos operacionales.

    **Por qué una consulta propia y no reusar las de las páginas de detalle.**
    Reusarlas costaba 6,6 s en frío —`get_entregas` 3,1 s, `get_ciclo_pedidos`
    2,4 s— y la portada es lo primero que se abre, sin caché caliente. Esta
    consulta agrega en SQL y tarda una fracción.

    **El riesgo de esa decisión es el que DEC-067 documentó**: dos definiciones
    del mismo indicador que divergen en silencio (155 contra 81 quiebres, 1.817
    contra 1.516 posiciones). Por eso cada cifra usa **exactamente el mismo
    predicado** que su página de detalle, y `test_scorecard_coincide_con_detalle`
    lo verifica contra las funciones reales. Si alguien cambia una definición y
    no la otra, la suite cae.

    El *on time* se calcula como `DATE(entrega) <= SUBSTR(hora_entrega, 1, 10)`,
    que es la traducción literal de `comun.entregas.clasificar_por_dia()` y
    funciona con los dos formatos fechados porque ambos empiezan por
    `YYYY-MM-DD`. Verificado: **1.809 de 20.069 por las dos vías**.

    Returns:
        Dict con las cifras. Un valor `None` significa "no medible", nunca cero.
    """
    con = _conn()
    try:
        fila = con.execute(
            f"""SELECT
                  (SELECT COUNT(*) FROM (
                      SELECT id_pedido FROM subpedidos GROUP BY id_pedido
                       HAVING SUM(CASE WHEN LOWER(estado) NOT IN ({_cerr})
                                  THEN 1 ELSE 0 END) > 0
                  ))                                                AS abiertos,
                  (SELECT COALESCE(SUM(v.valor), 0) FROM (
                      SELECT id_pedido FROM subpedidos GROUP BY id_pedido
                       HAVING SUM(CASE WHEN LOWER(estado) NOT IN ({_cerr})
                                  THEN 1 ELSE 0 END) > 0
                   ) a
                   JOIN (SELECT id_pedido, MAX(monto_final_num) AS valor
                           FROM estadisticas_monto
                          WHERE concepto_base = 'Total a pagar / Total final a pagar'
                       GROUP BY id_pedido) v ON v.id_pedido = a.id_pedido)
                                                                    AS valor_retenido,
                  (SELECT COUNT(*) FROM pedidos
                    WHERE scraping_completo = 1)                    AS pedidos_total,
                  (SELECT SUM(CASE WHEN hay_diferencia = 1 THEN 1 ELSE 0 END)
                     FROM pedidos WHERE scraping_completo = 1)      AS pedidos_con_faltante,
                  (SELECT COUNT(*) FROM pedidos p
                     JOIN (SELECT id_pedido, MIN(momento) AS m FROM registro_operaciones
                            WHERE accion = 'Entrega' GROUP BY id_pedido) e
                       ON e.id_pedido = p.id_pedido
                    WHERE p.hora_entrega LIKE '____-__-__%')        AS entregas_medibles,
                  (SELECT SUM(CASE WHEN DATE(e.m) <= SUBSTR(p.hora_entrega, 1, 10)
                                   THEN 1 ELSE 0 END)
                     FROM pedidos p
                     JOIN (SELECT id_pedido, MIN(momento) AS m FROM registro_operaciones
                            WHERE accion = 'Entrega' GROUP BY id_pedido) e
                       ON e.id_pedido = p.id_pedido
                    WHERE p.hora_entrega LIKE '____-__-__%')        AS entregas_a_tiempo
            """  # noqa: S608
        ).fetchone()
    finally:
        con.close()

    abiertos, valor, total, con_falt, medibles, a_tiempo = fila
    return {
        "pedidos_abiertos": abiertos or 0,
        "valor_retenido": valor or 0.0,
        "fill_rate": (100.0 * (total - (con_falt or 0)) / total) if total else None,
        "on_time": (100.0 * (a_tiempo or 0) / medibles) if medibles else None,
        "entregas_medibles": medibles or 0,
    }


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_riesgo_por_referencia() -> pd.DataFrame:
    """Venta diaria en riesgo por referencia, para valorizar las alertas (DEC-102).

    **El precio no puede salir de `inventario_salud`.** Ahí `valor_venta` es el
    valor del stock, y una referencia en quiebre tiene stock cero: las 15 alertas
    de quiebre traen `valor_venta = 0` y `disponible = 0`, así que dividir una
    por otra da `NULL`. El precio real sale de `lineas_pedido` — lo que la
    empresa efectivamente cobra por esa referencia.

    `demanda_diaria × precio_medio` es lo que cuesta **cada día** de quiebre.
    Reordena las alertas por completo: por unidades la primera es `PS03-85G`
    (3.104), por plata es `PS13-60U` con **$1,57 M/día**, y `PP129` —0,3
    unidades diarias a $374 mil— entra al top 3 siendo invisible en el ranking
    por volumen.

    Returns:
        Una fila por referencia con demanda diaria, precio medio y riesgo diario.
    """
    if not _objeto_existe("inventario_salud"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT s.referencia,
                      s.demanda_diaria,
                      AVG(l.precio_unitario_num)                    AS precio_medio,
                      s.demanda_diaria * AVG(l.precio_unitario_num) AS riesgo_diario
                 FROM inventario_salud s
                 JOIN lineas_pedido l ON l.referencia = s.referencia
                                     AND l.precio_unitario_num > 0
             GROUP BY s.referencia, s.demanda_diaria""",
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_excepciones(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Excepciones del proceso: faltantes, entregas fallidas y ajustes (DEC-099).

    `registro_operaciones` emite 19 acciones distintas y el dashboard leía
    cuatro. Las que faltaban describen **lo que sale mal**, que es justo lo que
    un área de operaciones necesita ver:

    | Acción | Pedidos | Qué es |
    |---|---|---|
    | `Alistamiento con faltantes` | 3.605 | la bodega no pudo completar |
    | `Faltantes aprobados` | 3.606 | alguien aceptó despachar corto |
    | `Faltantes no aprobados` | 404 | alguien lo rechazó |
    | `Cancelar entrega` | 316 | la entrega falló |
    | `Modificar cantidad de entrega manualmente` | 24 | ajuste fuera de proceso |

    El monto sale de `v_gestion_diferencias_num` y el desglose por línea de
    `v_detalle_diferencias_num` — las dos VIEWs que DEC-065 dio por conectadas
    sin estarlo (DEC-094, Corrección 2).

    **`Usuario realizó pedido` con `tipo_usuario = 'member'`** marca los pedidos
    que hizo el cliente y no un vendedor: son **26.892 de 29.931 (89,8%)**, con
    4.354 usuarios distintos. Es un hecho estructural del canal que no estaba en
    ninguna pantalla.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con las marcas de tiempo de cada excepción, el monto
        de la diferencia y si lo hizo el cliente.
    """
    if not _objeto_existe("v_gestion_diferencias_num", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH ev AS (
                   SELECT id_pedido,
                          MIN(CASE WHEN accion = 'Alistamiento con faltantes'
                              THEN momento END) AS faltante_en,
                          MIN(CASE WHEN accion = 'Faltantes aprobados'
                              THEN momento END) AS aprobado_en,
                          MIN(CASE WHEN accion = 'Faltantes no aprobados'
                              THEN momento END) AS rechazado_en,
                          MIN(CASE WHEN accion = 'Cancelar entrega'
                              THEN momento END) AS entrega_cancelada_en,
                          MIN(CASE WHEN accion = 'Entrega' THEN momento END) AS entregado_en,
                          MAX(CASE WHEN accion = 'Entrega' THEN momento END)
                              AS ultima_entrega_en,
                          SUM(CASE WHEN accion = 'Modificar cantidad de entrega manualmente'
                              THEN 1 ELSE 0 END) AS ajustes_manuales,
                          MAX(CASE WHEN accion = 'Modificar cantidad de entrega manualmente'
                              THEN usuario END) AS ajustado_por,
                          MAX(CASE WHEN accion = 'Usuario realizó pedido'
                                    AND tipo_usuario = 'member'
                              THEN 1 ELSE 0 END) AS autogestion
                     FROM registro_operaciones
                 GROUP BY id_pedido
               ),
               lineas_dif AS (
                   SELECT id_pedido,
                          COUNT(*)                    AS productos_con_faltante,
                          GROUP_CONCAT(DISTINCT tipo) AS lineas_afectadas
                     FROM v_detalle_diferencias_num
                 GROUP BY id_pedido
               )
               SELECT p.id_pedido, p.fecha, p.nombre_empresa, p.metodo_entrega,
                      p.vendedor, p.forma_pago,
                      e.faltante_en, e.aprobado_en, e.rechazado_en,
                      e.entrega_cancelada_en, e.entregado_en, e.ultima_entrega_en,
                      COALESCE(e.ajustes_manuales, 0) AS ajustes_manuales,
                      e.ajustado_por,
                      COALESCE(e.autogestion, 0)      AS autogestion,
                      g.monto_diferencia,
                      COALESCE(d.productos_con_faltante, 0) AS productos_con_faltante,
                      d.lineas_afectadas
                 FROM pedidos p
                 -- LEFT y no INNER: 412 pedidos no tienen ninguna fila en
                 -- `registro_operaciones`, y excluirlos inflaría el porcentaje
                 -- de autogestión de 89,8% a 91,1% sin que nada lo delate.
                 LEFT JOIN ev e       ON e.id_pedido = p.id_pedido
                 LEFT JOIN v_gestion_diferencias_num g ON g.id_pedido = p.id_pedido
                 LEFT JOIN lineas_dif d                ON d.id_pedido = p.id_pedido
                WHERE p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_comprometido() -> pd.DataFrame:
    """Mercancía comprometida en pedidos abiertos, contra el stock (DEC-098).

    Es la "Vista de inventario comprometido" que `integral.md` pide desde el
    primer día — el puente entre pedidos e inventario.

    **No usa `v_inventario_comprometido`, y la razón es un defecto medido.** Esa
    VIEW calcula `cantidad_comprada − cantidad_entregada` sobre subpedidos
    **activos**, siguiendo la definición literal de `integral.md`. El problema es
    que el origen puebla `cantidad_entregada = cantidad_comprada` desde el
    momento en que se crea la línea: en subpedidos abiertos **el 99,3% de las
    líneas tiene las dos cantidades iguales**, y `cantidades_definitivas` vale 0
    en 2.081 de 2.085 subpedidos activos —el propio scraper sabe que esas
    cantidades no son finales—.

    Consecuencia medida: la VIEW reporta **251 unidades pendientes** cuando el
    comprometido real de los 1.812 pedidos abiertos es de **497.929**. La resta
    solo tiene sentido en subpedidos ya despachados, que es donde sí la usa el
    fill rate (DEC-069); sobre los abiertos da casi cero siempre.

    Acá se toma `cantidad_comprada` de los subpedidos abiertos, que es lo que la
    bodega tiene efectivamente reservado.

    Returns:
        Una fila por referencia con lo comprometido, el stock disponible, el
        faltante para cubrirlo y su estado de salud. Vacío si falta el ETL.
    """
    if not _objeto_existe("v_lineas_pedido_num", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            f"""WITH comprometido AS (
                    SELECT l.referencia,
                           MIN(l.nombre_producto)      AS producto,
                           COUNT(*)                    AS lineas,
                           COUNT(DISTINCT l.id_pedido) AS pedidos,
                           SUM(l.cantidad_comprada)    AS comprometido,
                           SUM(l.monto_final)          AS valor
                      FROM v_lineas_pedido_num l
                      JOIN subpedidos s ON s.id_pedido        = l.id_pedido
                                       AND s.numero_subpedido = l.numero_subpedido
                     WHERE LOWER(s.estado) NOT IN ({_cerr})
                       AND l.nombre_producto IS NOT NULL
                       AND l.nombre_producto <> ''
                  GROUP BY l.referencia
                )
                SELECT c.referencia,
                       c.producto,
                       c.lineas,
                       c.pedidos,
                       c.comprometido,
                       c.valor,
                       sa.disponible,
                       sa.estado    AS estado_salud,
                       sa.familia,
                       MAX(c.comprometido - COALESCE(sa.disponible, 0), 0) AS faltante
                  FROM comprometido c
                  LEFT JOIN inventario_salud sa ON sa.referencia = c.referencia""",  # noqa: S608
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ventas(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Facturación por pedido, con su descomposición monetaria (DEC-097).

    Sale de `v_estadisticas_monto_num`, una de las dos VIEWs que DEC-065 dejó
    **reservadas con decisión escrita** y que el ETL venía reconstruyendo cada
    hora sin ningún consumidor. Los 15 conceptos tienen cobertura del 100% de
    los pedidos.

    **`cancelado` no es opcional.** De los $84.093 M facturados, **$11,9 mil
    millones (14,1%) son pedidos cuyos subpedidos están todos cancelados**.
    Publicar el bruto como "ventas" sobreestimaría la facturación en un 14%, así
    que la columna viaja con los datos y la página parte las dos cifras. Se
    marca cancelado solo cuando **todos** los subpedidos lo están: hay 131
    pedidos parcialmente cancelados que no son ni una cosa ni la otra.

    Se usa `monto_final` y no `monto_pagar`: el primero es lo que quedó después
    de los faltantes de despacho, o sea lo realmente facturado.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con vendedor, cliente, canal y los cinco montos que
        componen la factura.
    """
    if not _objeto_existe("v_estadisticas_monto_num", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH cancelado AS (
                   SELECT id_pedido
                     FROM subpedidos
                 GROUP BY id_pedido
                   HAVING SUM(CASE WHEN LOWER(estado) = 'cancelado' THEN 1 ELSE 0 END)
                          = COUNT(*)
               ),
               montos AS (
                   SELECT id_pedido,
                          MAX(CASE WHEN concepto_base =
                                'Total a pagar / Total final a pagar'
                              THEN monto_final END) AS total,
                          MAX(CASE WHEN concepto_base = 'Total antes de IVA'
                              THEN monto_final END) AS antes_iva,
                          MAX(CASE WHEN concepto_base = 'Total precio original'
                              THEN monto_final END) AS precio_lista,
                          MAX(CASE WHEN concepto_base = 'Total descuento'
                              THEN monto_final END) AS descuento,
                          MAX(CASE WHEN concepto_base = 'Total IVA'
                              THEN monto_final END) AS iva
                     FROM v_estadisticas_monto_num
                 GROUP BY id_pedido
               )
               SELECT p.id_pedido, p.fecha, p.vendedor, p.nombre_empresa, p.nit,
                      p.metodo_entrega, p.forma_pago,
                      CASE WHEN x.id_pedido IS NOT NULL THEN 1 ELSE 0 END AS cancelado,
                      m.total, m.antes_iva, m.precio_lista, m.descuento, m.iva
                 FROM pedidos p
                 JOIN montos m     ON m.id_pedido = p.id_pedido
                 LEFT JOIN cancelado x ON x.id_pedido = p.id_pedido
                WHERE p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ventas_por_linea(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Mix por línea de producto, desde `lineas_pedido.tipo` (DEC-097).

    **No se deriva del IVA.** Esa vía parecía natural —el origen publica `IVA
    arena para gatos`, `IVA accesorios` e `IVA alimentos`— pero solo cubre el
    **75,4%** de la base antes de IVA: el 24,6% restante es producto sin IVA que
    no queda atribuido a ninguna línea. `lineas_pedido.tipo` trae las mismas
    tres categorías **al 100% y a nivel de línea**, sin dividir por una tasa.

    Tampoco se usa `familia`: es un código de bodega (PS, PA, PB…), no una
    categoría comercial, y cubre apenas el 28% del valor.

    Los pedidos totalmente cancelados quedan fuera, igual que en `get_ventas()`.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por fecha y línea, con líneas, unidades y valor.
    """
    if not _num_cols_exist():
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH cancelado AS (
                   SELECT id_pedido
                     FROM subpedidos
                 GROUP BY id_pedido
                   HAVING SUM(CASE WHEN LOWER(estado) = 'cancelado' THEN 1 ELSE 0 END)
                          = COUNT(*)
               )
               SELECT p.fecha,
                      l.tipo                    AS linea,
                      COUNT(*)                  AS lineas,
                      SUM(l.cantidad_comprada)  AS unidades,
                      SUM(l.monto_final_num)    AS valor
                 FROM lineas_pedido l
                 JOIN pedidos p        ON p.id_pedido = l.id_pedido
                 LEFT JOIN cancelado x ON x.id_pedido = l.id_pedido
                WHERE p.fecha BETWEEN ? AND ?
                  AND x.id_pedido IS NULL
             GROUP BY p.fecha, l.tipo""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_descuentos_por_tipo(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Descuento concedido por tipo, a nivel de línea (DEC-097).

    Sale de `v_descuentos_lineas` (DEC-035), la otra VIEW que el ETL
    reconstruía cada hora sin consumidor. El `descuento_tipo` es texto compuesto
    del origen: una línea puede acumular varios (`Almacen9% | Tipo de cambio3%`),
    y se respeta la combinación tal como viene en vez de partirla — separarla
    repartiría el monto entre categorías con un criterio inventado.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por tipo de descuento, con líneas afectadas y monto concedido.
    """
    if not _objeto_existe("v_descuentos_lineas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT descuento_tipo,
                      COUNT(*)                      AS lineas,
                      SUM(monto_descuento_total)    AS monto,
                      COUNT(DISTINCT id_pedido)     AS pedidos
                 FROM v_descuentos_lineas
                WHERE fecha BETWEEN ? AND ?
             GROUP BY descuento_tipo
             ORDER BY monto DESC""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ciclo_pedidos(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Ciclo de vida de cada pedido, leído del timeline (DEC-096).

    `timeline_pedido` tenía **172.954 filas y cero consumidores** hasta la
    auditoría de DEC-094. Es la sección 1 de las ocho que captura el scraper y
    la fuente de la "Vista de ciclos operacionales" que `integral.md` pide desde
    el principio.

    **El timeline solo registra pasos completados** (`completado = 1` en el
    100% de las filas) y está en orden cronológico perfecto —cero violaciones
    sobre 172.954—. De ahí sale la propiedad que hace útil esta consulta: el
    último paso de un pedido abierto **es su estado actual**, y la antigüedad de
    ese paso es cuánto lleva ahí parado.

    **Reescrita a `GROUP BY` a propósito.** La versión con
    `ROW_NUMBER() OVER (PARTITION BY ...)` tardaba 10,4 s: las funciones de
    ventana no aprovechan el índice. Con el agregado más el
    `idx_timeline_pedido_paso` que DEC-096 agregó baja a **2,0 s**, con
    resultado idéntico —verificado con `DataFrame.equals`, no a ojo, igual que
    en DEC-067—.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con su estado actual, cuándo entró en él, cuántos
        pasos lleva, si sigue abierto y cuándo se entregó (NaN si no).
    """
    if not _objeto_existe("timeline_pedido"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            f"""WITH resumen AS (
                    SELECT id_pedido,
                           MIN(fecha_hora) AS inicio,
                           COUNT(*)        AS pasos,
                           MAX(paso)       AS ultimo_paso
                      FROM timeline_pedido
                  GROUP BY id_pedido
                ),
                abiertos AS (
                    SELECT id_pedido
                      FROM subpedidos
                  GROUP BY id_pedido
                    HAVING SUM(CASE WHEN LOWER(estado) NOT IN ({_cerr})
                                    THEN 1 ELSE 0 END) > 0
                ),
                entrega AS (
                    SELECT id_pedido, MIN(fecha_hora) AS entregado_en
                      FROM timeline_pedido
                     WHERE titulo = 'Recibido y recibido'
                  GROUP BY id_pedido
                )
                SELECT r.id_pedido,
                       pe.fecha,
                       pe.metodo_entrega,
                       pe.nombre_empresa,
                       pe.vendedor,
                       pe.forma_pago,
                       t.titulo         AS estado_actual,
                       r.inicio,
                       t.fecha_hora     AS ultimo_evento,
                       r.pasos,
                       e.entregado_en,
                       CASE WHEN a.id_pedido IS NOT NULL THEN 1 ELSE 0 END AS abierto,
                       COALESCE(v.valor, 0) AS valor
                  FROM resumen r
                  JOIN timeline_pedido t ON t.id_pedido = r.id_pedido
                                        AND t.paso      = r.ultimo_paso
                  JOIN pedidos pe        ON pe.id_pedido = r.id_pedido
                  LEFT JOIN abiertos a   ON a.id_pedido  = r.id_pedido
                  LEFT JOIN entrega  e   ON e.id_pedido  = r.id_pedido
                  LEFT JOIN (
                      SELECT id_pedido, MAX(monto_final_num) AS valor
                        FROM estadisticas_monto
                       WHERE concepto_base = 'Total a pagar / Total final a pagar'
                    GROUP BY id_pedido
                  ) v ON v.id_pedido = r.id_pedido
                 WHERE pe.fecha BETWEEN ? AND ?""",  # noqa: S608
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ciclo_transiciones(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Cuánto tarda cada salto entre dos estados consecutivos (DEC-096).

    Responde "dónde se va el tiempo", que es distinto de "cuánto tarda el
    pedido": un ciclo de 9 días puede ser diez etapas de un día o una de siete.

    Se devuelven las duraciones crudas y la mediana se calcula en pandas —
    SQLite no tiene percentiles y la media miente con colas largas como estas
    (hay transiciones de meses).

    **Un mismo par origen→destino puede repetirse dentro de un pedido**: los de
    varios subpedidos recorren el timeline más de una vez (hay 3.455 saltos
    `confirmando → confirmando`). Cada repetición cuenta como una observación,
    que es lo correcto para medir duración de etapa.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por salto entre pasos consecutivos, con origen, destino y horas.
    """
    if not _objeto_existe("timeline_pedido"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT a.titulo AS origen,
                      b.titulo AS destino,
                      (JULIANDAY(b.fecha_hora) - JULIANDAY(a.fecha_hora)) * 24.0 AS horas
                 FROM timeline_pedido a
                 JOIN timeline_pedido b ON b.id_pedido = a.id_pedido
                                       AND b.paso      = a.paso + 1
                 JOIN pedidos p         ON p.id_pedido  = a.id_pedido
                WHERE p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_auditoria_pago(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Ciclo comprobante → auditoría, por pedido (DEC-082).

    Sale **de datos que ya estaban**: `registro_operaciones` registra
    `Subir comprobante de pago` y `Auditoría de pago` con usuario y hora
    desde el 2026-01-23. Nadie los había cruzado.

    El `LEFT JOIN` es deliberado: un comprobante subido y **nunca auditado**
    es el caso que más importa —es una cola de trabajo invisible— y un
    `INNER JOIN` lo borraría justo a él.

    Se toma el primer comprobante y la primera auditoría posterior: un
    pedido puede tener varios comprobantes (el DOM muestra "1 envíos · 2
    comprobantes") y lo que se mide es cuánto tardó en revisarse, no cuántas
    veces se subió.

    **Veredicto (DEC-083).** `registro_operaciones.referencia` trae `Aprobado`
    o `Rechazado` en las filas de auditoría. Solo está poblada **desde el
    2026-04-10**: antes de esa fecha `con_veredicto` vale 0 y la tasa de
    rechazo no es medible. La página lo dice en vez de rellenar con ceros,
    que darían un rechazo artificialmente bajo en el primer trimestre.

    Las subidas se pre-agregan en una CTE **antes** de unirlas con las
    auditorías. Sin eso, un pedido con 2 comprobantes y 2 auditorías produce
    4 filas y los `SUM()` cuentan doble; el `MIN()` original era inmune al
    problema, los contadores nuevos no lo son.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con comprobante: cuándo se subió, quién y cuándo
        auditó, las horas entre ambos (NaN si sigue sin auditar) y el conteo
        de auditorías, rechazos y aprobaciones.
    """
    if not _objeto_existe("registro_operaciones"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH subidas AS (
                   SELECT id_pedido,
                          MIN(momento) AS subido_en,
                          MIN(usuario) AS subido_por,
                          COUNT(*)     AS comprobantes
                     FROM registro_operaciones
                    WHERE accion = 'Subir comprobante de pago'
                 GROUP BY id_pedido
               )
               SELECT p.id_pedido,
                      p.fecha,
                      p.forma_pago,
                      s.subido_en,
                      s.subido_por,
                      s.comprobantes,
                      MIN(a.momento)  AS auditado_en,
                      MIN(a.usuario)  AS auditado_por,
                      COUNT(a.momento) AS auditorias,
                      SUM(CASE WHEN a.referencia = 'Rechazado' THEN 1 ELSE 0 END)
                          AS rechazos,
                      SUM(CASE WHEN a.referencia = 'Aprobado'  THEN 1 ELSE 0 END)
                          AS aprobaciones,
                      SUM(CASE WHEN a.referencia IN ('Aprobado', 'Rechazado')
                               THEN 1 ELSE 0 END) AS con_veredicto
                 FROM subidas s
                 JOIN pedidos p ON p.id_pedido = s.id_pedido
                 LEFT JOIN registro_operaciones a
                        ON a.id_pedido = s.id_pedido
                       AND a.accion    = 'Auditoría de pago'
                       AND a.momento  >= s.subido_en
                WHERE p.fecha BETWEEN ? AND ?
             GROUP BY p.id_pedido, p.fecha, p.forma_pago,
                      s.subido_en, s.subido_por, s.comprobantes""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_motivos_cancelacion(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Pedidos cancelados con su motivo escrito a mano (DEC-085).

    No confundir con `get_cancelaciones()`, que es de DEC-063 y mira otra
    cosa: los subpedidos **alistados** que después se cancelaron, desde el
    ángulo del inventario. Esta mira el **porqué** de la cancelación, sobre
    todos los pedidos y no solo los alistados.

    El motivo vive en `registro_operaciones.referencia` de las filas de
    `Cancelar pedido` y nunca se había leído. La clasificación no se hace
    acá sino en `comun.motivos`, que es una función pura y testeable; esta
    capa solo trae el texto crudo y el contexto del pedido.

    **Un pedido puede tener varios eventos de cancelación** (5.784 eventos
    sobre 4.947 pedidos). Se toma el primero: es el que explica por qué murió,
    los siguientes son reintentos administrativos.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido cancelado, con el motivo crudo, el valor del
        pedido y los días que pasaron entre el pedido y su cancelación.
    """
    if not _objeto_existe("registro_operaciones"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH primera AS (
                   SELECT id_pedido, MIN(momento) AS momento
                     FROM registro_operaciones
                    WHERE accion = 'Cancelar pedido'
                 GROUP BY id_pedido
               )
               SELECT p.id_pedido,
                      p.fecha,
                      p.forma_pago,
                      p.nombre_empresa,
                      r.momento                       AS cancelado_en,
                      r.usuario                       AS cancelado_por,
                      r.referencia                    AS motivo,
                      JULIANDAY(r.momento) - JULIANDAY(p.fecha) AS dias,
                      (SELECT MAX(em.monto_final_num)
                         FROM estadisticas_monto em
                        WHERE em.id_pedido = p.id_pedido
                          AND em.concepto_base =
                              'Total a pagar / Total final a pagar') AS valor
                 FROM primera pr
                 JOIN registro_operaciones r
                       ON r.id_pedido = pr.id_pedido
                      AND r.momento   = pr.momento
                      AND r.accion    = 'Cancelar pedido'
                 JOIN pedidos p ON p.id_pedido = pr.id_pedido
                WHERE p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_pedidos_impagos() -> pd.DataFrame:
    """Pedidos vivos de pago inmediato con saldo pendiente (DEC-085).

    Es la mitad accionable del análisis de cancelaciones. Medido: el 94% de
    las cancelaciones por falta de pago ocurre entre el día 3 y el 7, con
    mediana en 5 y sin variar mes a mes. Con esa regularidad, un pedido vivo,
    impago y de 5 días **es predecible**, y la mercancía que retiene está por
    liberarse.

    Solo `Pago inmediato`: el crédito no debe nada hasta su vencimiento y
    contra entrega se cobra fuera de este campo (el 8,4% de cobertura medido
    en DEC-084 lo confirma). Meterlos daría una lista de falsos positivos.

    **El saldo sale del origen cuando existe (DEC-089).** La tarjeta
    «Operación de pago» trae `pago_saldo` calculado por el sistema; solo
    existe desde el 2026-07-16, así que fuera de ese rango se cae a la
    derivación `total − pagado`. Medido sobre los 833 pedidos vivos con
    tarjeta: la derivación marca 230 impagos y el origen 224 — los 6 que
    sobran son pedidos que el origen da por **`Pagado`**, y no hay ninguno
    en la dirección contraria. La columna `fuente` dice cuál se usó, porque
    una cifra derivada y una del origen no son la misma clase de dato.

    Returns:
        Una fila por pedido vivo impago, con antigüedad en días, la mercancía
        comprometida (líneas, unidades, referencias) y el origen del saldo.
    """
    if not _num_cols_exist():
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH montos AS (
                   SELECT id_pedido,
                          MAX(CASE WHEN concepto_base =
                                'Total a pagar / Total final a pagar'
                              THEN monto_final_num END) AS total,
                          MAX(CASE WHEN concepto_base = 'Monto pagado'
                              THEN monto_pagar_num  END) AS pagado
                     FROM estadisticas_monto
                 GROUP BY id_pedido
               ),
               vivos AS (
                   SELECT id_pedido
                     FROM subpedidos
                 GROUP BY id_pedido
                   HAVING SUM(CASE WHEN LOWER(estado) NOT IN
                            ('completado', 'cancelado', 'comentado')
                          THEN 1 ELSE 0 END) > 0
               ),
               base AS (
                   SELECT p.id_pedido,
                          p.fecha,
                          p.nombre_empresa,
                          p.nit,
                          p.pago_estado,
                          COALESCE(p.pago_total_num, m.total) AS total,
                          COALESCE(p.pago_pagado_num, m.pagado) AS pagado,
                          -- DEC-089: el saldo del origen manda; la
                          -- derivación solo cubre lo anterior al 2026-07-16.
                          COALESCE(p.pago_saldo_num,
                                   m.total - COALESCE(m.pagado, 0)) AS saldo,
                          CASE WHEN p.pago_saldo_num IS NOT NULL
                               THEN 'origen' ELSE 'derivado' END AS fuente
                     FROM pedidos p
                     JOIN vivos  v ON v.id_pedido = p.id_pedido
                     JOIN montos m ON m.id_pedido = p.id_pedido
                    WHERE p.scraping_completo = 1
                      AND p.forma_pago = 'Pago inmediato'
                      AND COALESCE(p.pago_total_num, m.total) IS NOT NULL
                      AND COALESCE(p.pago_saldo_num,
                                   m.total - COALESCE(m.pagado, 0)) > 1
               )
               SELECT b.id_pedido,
                      b.fecha,
                      b.nombre_empresa,
                      b.nit,
                      b.pago_estado,
                      b.fuente,
                      b.total,
                      b.pagado,
                      b.saldo,
                      COUNT(l.id)                          AS lineas,
                      COALESCE(SUM(CAST(l.cantidad_comprada AS REAL)), 0)
                                                           AS unidades,
                      COUNT(DISTINCT l.referencia)         AS referencias,
                      CAST(JULIANDAY('now', '-5 hours') - JULIANDAY(b.fecha)
                           AS INTEGER)                     AS dias
                 FROM base b
                 LEFT JOIN lineas_pedido l ON l.id_pedido = b.id_pedido
             GROUP BY b.id_pedido, b.fecha, b.nombre_empresa, b.nit,
                      b.pago_estado, b.fuente, b.total, b.pagado, b.saldo""",
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_estado_pago(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Estado y saldo de pago **calculados por el origen** (DEC-089).

    Sale de la tarjeta «Operación de pago», que el scraper captura desde
    DEC-087. Es la respuesta del propio sistema a "cuánto falta cobrar", sin
    derivación: sobre los mismos pedidos, el `Total` coincide al 100% con
    `estadisticas_monto` pero el `Monto pagado` solo al 92,1%, y donde
    difieren la tarjeta no produce **ningún** ratio pagado/total mayor a 2
    contra los 4 —de hasta 39,8×— del campo viejo.

    **No sirve para el saldo a favor:** `pago_saldo` nunca es negativo, mide
    lo que falta por cobrar topado en cero. De los 172 pedidos con
    `pagado > total`, los 172 tienen saldo 0. Esa pregunta la sigue
    respondiendo `get_saldo_a_favor()` sobre `gestion_diferencias`.

    **Cobertura:** solo pedidos desde el 2026-07-16; DEC-087 verificó que el
    origen no renderiza la tarjeta para los anteriores (0 de 24 en una prueba
    sobre enero). La página tiene que decir el rango, no dar a entender que
    cubre todo.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con tarjeta, con estado, montos y el número de
        comprobantes registrados.
    """
    if not _num_cols_exist():
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT p.id_pedido,
                      p.fecha,
                      p.forma_pago,
                      p.nombre_empresa,
                      p.pago_estado,
                      p.pago_total_num  AS total,
                      p.pago_pagado_num AS pagado,
                      p.pago_saldo_num  AS saldo,
                      p.pago_progreso,
                      COALESCE(r.comprobantes, 0) AS comprobantes,
                      COALESCE(r.rechazados, 0)   AS rechazados,
                      COALESCE(r.sin_revisar, 0)  AS sin_revisar,
                      r.metodos
                 FROM pedidos p
                 LEFT JOIN (
                     SELECT id_pedido,
                            COUNT(*) AS comprobantes,
                            SUM(CASE WHEN estado_revision = 'Rechazado'
                                     THEN 1 ELSE 0 END) AS rechazados,
                            SUM(CASE WHEN COALESCE(estado_revision, '') = ''
                                     THEN 1 ELSE 0 END) AS sin_revisar,
                            GROUP_CONCAT(DISTINCT metodo_pago) AS metodos
                       FROM registros_pago
                   GROUP BY id_pedido
                 ) r ON r.id_pedido = p.id_pedido
                WHERE p.pago_estado IS NOT NULL
                  AND p.pago_estado <> ''
                  AND p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_comprobantes(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Comprobantes de pago, uno por fila (DEC-090).

    **No es una conciliación bancaria** y conviene decirlo: hay una sola
    cuenta receptora —los 2.117 comprobantes con cuenta van todos a la misma;
    los otros 220 traen `-` porque son pagos en línea— y el pipeline no ve
    ningún extracto contra el cual cruzar. Lo que entrega es **el lado del
    libro**: qué dice el sistema que entró, cuándo, por qué canal y si alguien
    lo verificó.

    Lo accionable son los **544 sin revisar**: plata que el cliente dice haber
    pagado y que nadie verificó. Es la misma cola que DEC-082 encontró
    contando eventos, pero por comprobante y con su monto.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por comprobante, con canal, cuenta, montos, revisor y estado.
    """
    if not _objeto_existe("registros_pago"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT r.id_pedido,
                      p.fecha,
                      p.nombre_empresa,
                      r.secuencia,
                      r.metodo_pago,
                      r.cuenta_receptora,
                      r.monto_comprobante_num AS monto_comprobante,
                      r.monto_pago_num        AS monto_pago,
                      r.hora_pago,
                      r.fecha_envio,
                      CASE WHEN COALESCE(r.estado_revision, '') = ''
                           THEN 'Sin revisar' ELSE r.estado_revision END AS estado,
                      r.fecha_revision,
                      -- El origen usa '-' como placeholder de "todavía nadie".
                      CASE WHEN COALESCE(r.revisor, '-') = '-' THEN ''
                           ELSE r.revisor END AS revisor,
                      r.observaciones
                 FROM registros_pago r
                 JOIN pedidos p ON p.id_pedido = r.id_pedido
                WHERE p.fecha BETWEEN ? AND ?""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_credito_abierto() -> pd.DataFrame:
    """Crédito que el sistema sigue mostrando abierto, con su vencimiento (DEC-086).

    **No usa `Monto pagado`.** Para crédito ese campo no discrimina: la
    mediana de `pagado/facturado` es 0% tanto en los pedidos con crédito
    abierto como en los ya saldados. La señal que sí funciona es el
    comprobante — los `Completado` que conservan los campos de crédito tienen
    comprobante subido el **100%** de las veces, contra el **5,2%** de los
    `Entregado sin liquidar`.

    Definición, tres señales independientes y ninguna monetaria:

    1. el pedido conserva `vencimiento_credito` (el origen lo mantiene
       mientras el crédito está vivo);
    2. ningún subpedido está cancelado;
    3. nunca se subió un comprobante de pago.

    Es la lectura **conservadora**: sus 543 pedidos son un subconjunto
    estricto de los 714 que daría filtrar por saldo.

    **Ausencia de comprobante no es prueba de impago** — un cliente grande
    puede pagar por transferencia sin que nadie cargue nada. Ningún cliente
    está en 0%, pero los dos grandes retailers cargan a un cuarto de la tasa
    de los medianos; la página lo advierte junto a la tabla.

    Returns:
        Una fila por pedido con crédito abierto, con los días de atraso
        (negativos si todavía no vence) y el valor del pedido.
    """
    if not _num_cols_exist():
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """WITH pagados AS (
                   SELECT DISTINCT id_pedido
                     FROM registro_operaciones
                    WHERE accion = 'Subir comprobante de pago'
               ),
               cancelados AS (
                   SELECT DISTINCT id_pedido
                     FROM subpedidos
                    WHERE LOWER(estado) = 'cancelado'
               )
               SELECT p.id_pedido,
                      p.fecha,
                      p.nit,
                      p.nombre_empresa,
                      p.dias_credito,
                      p.inicio_credito,
                      p.vencimiento_credito,
                      (SELECT MAX(em.monto_final_num)
                         FROM estadisticas_monto em
                        WHERE em.id_pedido = p.id_pedido
                          AND em.concepto_base =
                              'Total a pagar / Total final a pagar') AS valor,
                      CAST(JULIANDAY('now', '-5 hours')
                           - JULIANDAY(p.vencimiento_credito) AS INTEGER) AS atraso
                 FROM pedidos p
                WHERE p.forma_pago = 'Pago a crédito'
                  AND p.scraping_completo = 1
                  AND p.vencimiento_credito IS NOT NULL
                  AND p.vencimiento_credito <> ''
                  AND p.id_pedido NOT IN (SELECT id_pedido FROM pagados)
                  AND p.id_pedido NOT IN (SELECT id_pedido FROM cancelados)""",
            con,
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_saldo_a_favor(fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Saldo a favor del cliente, calculado por el origen (DEC-084).

    Mecanismo: el cliente paga primero; si el pedido sale corto, el monto
    facturado baja y la diferencia le queda **a favor**. `gestion_diferencias`
    trae la reconciliación ya hecha por el sistema origen —no derivada acá— y
    su identidad se cumple entera: `final = total − diferencia` en 4.401 de
    4.401 filas.

    **`anomalo` marca, no descarta.** 16 pedidos tienen `pagado > 2 × total`
    —el mayor pagó 107 veces el pedido— y cargan el 79% del saldo bruto. Un
    saldo a favor de 107 veces no existe: es un dato malo del origen. Se
    devuelven marcados para que la página publique las dos cifras por
    separado; promediarlos convertiría una cifra accionable en ruido.

    Args:
        fecha_desde: Fecha inicial inclusive del pedido (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive.

    Returns:
        Una fila por pedido con saldo distinto de cero, con el signo en
        `saldo` (positivo = a favor del cliente) y la marca `anomalo`.
    """
    if not _objeto_existe("gestion_diferencias"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql(
            """SELECT g.id_pedido,
                      p.fecha,
                      p.nit,
                      p.nombre_empresa,
                      p.forma_pago,
                      g.total_pagar_pedido_num AS total,
                      g.monto_final_pagar_num  AS facturado,
                      g.monto_pagado_num       AS pagado,
                      g.monto_diferencia_num   AS faltante,
                      g.monto_pagado_num - g.monto_final_pagar_num AS saldo,
                      CASE WHEN g.total_pagar_pedido_num > 0
                            AND g.monto_pagado_num > 2 * g.total_pagar_pedido_num
                           THEN 1 ELSE 0 END AS anomalo
                 FROM gestion_diferencias g
                 JOIN pedidos p ON p.id_pedido = g.id_pedido
                WHERE p.fecha BETWEEN ? AND ?
                  AND g.monto_pagado_num  IS NOT NULL
                  AND g.monto_final_pagar_num IS NOT NULL
                  AND ABS(g.monto_pagado_num - g.monto_final_pagar_num) > 1""",
            con,
            params=(fecha_desde, fecha_hasta),
        )
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_sin_ubicacion() -> pd.DataFrame:
    """Producto que el admin declara y que no está en ninguna posición (DEC-073).

    Es lo que el plan de conteo por construcción no puede descubrir: la cola
    manda gente a posiciones donde el sistema **ya dice que hay algo**.
    """
    if not _objeto_existe("v_inventario_sin_ubicacion", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_sin_ubicacion ORDER BY inventario DESC", con)
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
def get_operacion_ciclos() -> pd.DataFrame:
    """Tiempos de ciclo y carga por subpedido (DEC-054)."""
    if not _objeto_existe("v_operacion_ciclos", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_operacion_ciclos", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_operacion_ventanas() -> pd.DataFrame:
    """Ventana activa por alistador y día (DEC-055)."""
    if not _objeto_existe("v_operacion_ventanas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_operacion_ventanas", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_cancelaciones() -> pd.DataFrame:
    """Subpedidos alistados que después se cancelaron (DEC-063)."""
    if not _objeto_existe("v_cancelaciones_alistadas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_cancelaciones_alistadas", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_alertas() -> pd.DataFrame:
    """Excepciones abiertas y resueltas, con su antigüedad (DEC-059)."""
    if not _objeto_existe("v_alertas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_alertas", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_faltantes_clasificados() -> pd.DataFrame:
    """Pedidos/subpedidos con diferencia registrada, clasificados en 3 grupos
    (DEC-115/DEC-117): "Interno" (relleno de monto mínimo, sin faltante
    real), "Real" (faltante físico de unidades) y "Monetario" (diferencia
    sin faltante de mercancía).

    La clasificación se construye entera desde `lineas_pedido` —
    `gestion_diferencias`/`detalle_diferencias` no tienen columna de
    subpedido (limitación del origen, no del scraper), así que no sirven
    para atribuir a nivel subpedido. `lineas_pedido` sí trae
    `numero_subpedido`, y es indexada por `(id_pedido, numero_subpedido)`.

    Devuelve una fila por subpedido afectado, salvo el grupo "Monetario"
    en un pedido con más de un subpedido: como el origen no identifica cuál
    causó la diferencia, esa fila queda a nivel pedido
    (`numero_subpedido` es `None`, `atribuible` es `False`).

    Columnas: `id_pedido`, `numero_subpedido`, `fecha`, `grupo`,
    `unidades_faltantes` (solo grupo "Real"), `monto_diferencia`,
    `cancelado` (algún subpedido del pedido en estado Cancelado),
    `atribuible` (False solo para "Monetario" con varios subpedidos).
    """
    if not _objeto_existe("gestion_diferencias") or not _objeto_existe("lineas_pedido"):
        return pd.DataFrame()
    con = _conn()
    try:
        crudo = pd.read_sql(
            f"""
            WITH universo AS (
                SELECT p.id_pedido, p.fecha, gd.monto_diferencia
                FROM pedidos p
                JOIN gestion_diferencias gd ON gd.id_pedido = p.id_pedido
                WHERE p.hay_diferencia = 1
            ),
            -- Filtra lineas_pedido (875k+ filas) al universo ANTES de
            -- agregar: sin este paso, el GROUP BY de abajo escanea la tabla
            -- completa por índice en vez de buscar por id_pedido (medido:
            -- 60s vs 0,3s sobre datos reales, 2026-08-14).
            lineas_universo AS (
                SELECT lp.id_pedido, lp.numero_subpedido, lp.nombre_producto,
                       lp.cantidad_comprada, lp.cantidad_entregada
                FROM lineas_pedido lp
                JOIN universo u ON u.id_pedido = lp.id_pedido
            ),
            lineas_reales AS (
                SELECT id_pedido, numero_subpedido,
                       SUM(CAST(cantidad_comprada AS REAL)) comprada,
                       SUM(CAST(cantidad_entregada AS REAL)) entregada
                FROM lineas_universo
                WHERE nombre_producto NOT IN ({_relleno})
                GROUP BY id_pedido, numero_subpedido
            ),
            lineas_relleno AS (
                SELECT DISTINCT id_pedido, numero_subpedido
                FROM lineas_universo
                WHERE nombre_producto IN ({_relleno})
            ),
            subpedidos_universo AS (
                SELECT sp.id_pedido, sp.numero_subpedido, sp.estado
                FROM subpedidos sp
                JOIN universo u ON u.id_pedido = sp.id_pedido
            ),
            conteo AS (
                SELECT id_pedido, COUNT(*) n_subpedidos,
                       MAX(CASE WHEN estado = 'Cancelado' THEN 1 ELSE 0 END) algun_cancelado
                FROM subpedidos_universo
                GROUP BY id_pedido
            )
            SELECT
                su.id_pedido,
                su.numero_subpedido,
                u.fecha,
                u.monto_diferencia,
                cs.n_subpedidos,
                cs.algun_cancelado,
                lr.comprada,
                lr.entregada,
                (lrel.id_pedido IS NOT NULL) AS tiene_relleno
            FROM subpedidos_universo su
            JOIN universo u ON u.id_pedido = su.id_pedido
            JOIN conteo cs ON cs.id_pedido = su.id_pedido
            LEFT JOIN lineas_reales lr
                ON lr.id_pedido = su.id_pedido AND lr.numero_subpedido = su.numero_subpedido
            LEFT JOIN lineas_relleno lrel
                ON lrel.id_pedido = su.id_pedido AND lrel.numero_subpedido = su.numero_subpedido
            """,
            con,
        )
    finally:
        con.close()

    if crudo.empty:
        return pd.DataFrame()

    faltantes = (crudo["comprada"].fillna(0) - crudo["entregada"].fillna(0)).round(6)
    crudo["unidades_faltantes"] = faltantes
    crudo["es_real"] = faltantes != 0
    crudo["grupo_subpedido"] = None
    crudo.loc[crudo["es_real"], "grupo_subpedido"] = "Real"
    crudo.loc[~crudo["es_real"] & crudo["tiene_relleno"].astype(bool), "grupo_subpedido"] = (
        "Interno"
    )

    # Vectorizado — un loop fila por fila sobre ~4.600 pedidos medía ~16s
    # sobre datos reales (2026-08-14); esto baja el total a ~3s.
    con_senal = crudo["grupo_subpedido"].notna()
    # transform("count") cuenta valores no nulos por grupo (semántica de
    # pandas) — evita una lambda de Python por grupo.
    tiene_senal_pedido = crudo.groupby("id_pedido")["grupo_subpedido"].transform("count") > 0

    # "Real"/"Interno": una fila por subpedido con señal propia.
    grupo_a = crudo.loc[con_senal].copy()
    grupo_a["grupo"] = grupo_a["grupo_subpedido"]
    grupo_a.loc[grupo_a["grupo"] != "Real", "unidades_faltantes"] = 0
    grupo_a["atribuible"] = True

    # "Monetario": ningún subpedido del pedido tiene señal de cantidad.
    # Colapsa a una fila por pedido — fecha/monto/n_subpedidos son iguales
    # en todas sus filas, así que `first()` no pierde información salvo
    # numero_subpedido, que se resuelve aparte.
    sin_senal_pedidos = crudo.loc[~tiene_senal_pedido]
    grupo_b = sin_senal_pedidos.groupby("id_pedido", as_index=False).first()
    un_solo_subpedido = grupo_b["n_subpedidos"] == 1
    grupo_b["numero_subpedido"] = grupo_b["numero_subpedido"].where(un_solo_subpedido)
    grupo_b["grupo"] = "Monetario"
    grupo_b["unidades_faltantes"] = 0
    grupo_b["atribuible"] = un_solo_subpedido

    columnas = [
        "id_pedido",
        "numero_subpedido",
        "fecha",
        "grupo",
        "unidades_faltantes",
        "monto_diferencia",
        "algun_cancelado",
        "atribuible",
    ]
    resultado = pd.concat([grupo_a[columnas], grupo_b[columnas]], ignore_index=True)
    resultado["cancelado"] = resultado["algun_cancelado"].astype(bool)
    return resultado.drop(columns="algun_cancelado")


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_corridas() -> pd.DataFrame:
    """Historial de corridas, para las tendencias del scorecard (DEC-059)."""
    if not _objeto_existe("v_inventario_corridas", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_corridas ORDER BY id", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_conteos_archivos() -> pd.DataFrame:
    """Hojas de conteo vistas alguna vez, activas o anuladas (DEC-058)."""
    if not _objeto_existe("v_conteos_archivos", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_conteos_archivos", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ira() -> pd.DataFrame:
    """IRA por clase, calculado por el pipeline sobre los conteos (DEC-058)."""
    if not _objeto_existe("v_conteos_ira", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_conteos_ira", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_ira_periodo() -> pd.DataFrame:
    """Serie temporal del IRA por clase (DEC-062)."""
    if not _objeto_existe("v_conteos_ira_periodo", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_conteos_ira_periodo ORDER BY periodo", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_conformidad() -> pd.DataFrame:
    """Conformidad por posición, separada por tipo de ubicación (DEC-062)."""
    if not _objeto_existe("v_conteos_conformidad", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_conteos_conformidad", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_conteos() -> pd.DataFrame:
    """Historial de conteos físicos ingeridos desde Excel (DEC-058)."""
    if not _objeto_existe("v_conteos", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_conteos", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_posiciones() -> pd.DataFrame:
    """Posiciones activas del layout, ocupadas y vacías (DEC-061)."""
    if not _objeto_existe("v_inventario_posiciones", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_posiciones", con)
    finally:
        con.close()


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def get_inventario_ubicaciones() -> pd.DataFrame:
    """Líneas SKU-posición con su prioridad de conteo (DEC-057)."""
    if not _objeto_existe("v_inventario_ubicaciones", "view"):
        return pd.DataFrame()
    con = _conn()
    try:
        return pd.read_sql("SELECT * FROM v_inventario_ubicaciones", con)
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

    El `LEFT JOIN` contra `catalogo_productos` aporta el ID de
    especificación, que `lineas_pedido` no tiene. Es LEFT y no INNER a
    propósito: las líneas sin ID en el catálogo (códigos legados, pares con
    `id_producto` ambiguo, o pares con `id_especificacion` ambiguo —
    DEC-111) deben seguir viéndose, con el ID vacío, en vez de desaparecer
    de un consolidado que el analista va a sumar. Y la tabla puente guarda
    a lo sumo una fila por par `(referencia, código de barras)`, así que el
    join **no puede multiplicar filas** — verificado: 840.179 líneas antes
    y después.

    Args:
        fecha_desde: Fecha inicial inclusive (YYYY-MM-DD).
        fecha_hasta: Fecha final inclusive (YYYY-MM-DD).
        estado_pedido: "Todos", "Abiertos" o "Cerrados" — clasifica el
            pedido padre según tenga o no algún subpedido abierto.
        almacenes: Almacenes a incluir; vacío = todos.
        solo_previos_picking: Si True, deja solo subpedidos en uno de los 3
            estados de `comun.ESTADOS_PREVIOS_PICKING` (comprometidos, sin
            alistar) y cuyo alistamiento físico no terminó
            (`inicio_inspeccion = '-'`, DEC-039 pregunta 5 corregida por
            HAL-013). DEC-109: definición angostada respecto a la que usa
            el cruce de inventario (`ESTADOS_ACTIVOS_INVENTARIO`, 12
            estados) — ya no es la misma.
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
        filtros.append(f"LOWER(s.estado) IN ({_previos_picking}) AND s.inicio_inspeccion = '-'")

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
                       SUM(CASE WHEN c.id_especificacion IS NULL THEN 1 ELSE 0 END)
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
                   c.id_especificacion  AS "ID de especificación",
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
