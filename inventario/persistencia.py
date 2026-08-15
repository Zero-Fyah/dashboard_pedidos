"""
persistencia.py — La comparación de inventario aterriza en SQLite (DEC-043).

El dashboard **no importa este paquete ni lee Excel**: consume el
resultado por VIEW, igual que cualquier otro dato del proyecto. Calcular
en vivo costaba 14,19 s por request (13,83 s de parseo de Excel) y habría
sido el primer import cruzado entre etapas, contra DEC-022.

Este módulo es el punto de entrada del paso: `python -m
inventario.persistencia` carga las tres fuentes, calcula y escribe. Lo
invoca `scraper/actualizar_pedidos.bat` después de las dos descargas y
antes del ETL.

`inventario/` es dueño de sus tablas y de sus VIEWs — no pasa por el ETL,
que no tiene nada que normalizar acá. Son dos dueños de VIEWs en la misma
base (prefijo `v_inventario_*` propio, sin colisión con las 12 del ETL):
quien agregue una VIEW nueva debe mirar los dos módulos.
"""

import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from comun import get_db_path

logger = logging.getLogger("inventario.persistencia")

# Con el scheduler corriendo cada hora, una fuente de más de 3 h implica
# al menos dos descargas fallidas seguidas (DEC-043). El layout queda
# fuera del umbral a propósito: es manual, es normal que tenga semanas.
UMBRAL_DESACTUALIZADO_H = 3.0

_TABLAS = {
    "inventario_corridas": """
        CREATE TABLE IF NOT EXISTS inventario_corridas (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            ejecutado_en           TEXT    NOT NULL,
            admin_actualizado_en   TEXT,
            bochica_actualizado_en TEXT,
            layout_actualizado_en  TEXT,
            fuente_mas_vieja_h     REAL,
            datos_desactualizados  INTEGER NOT NULL DEFAULT 0,
            referencias            INTEGER,
            disponible_venta       REAL,
            vendido_no_alistado    REAL,
            inventario_teorico     REAL,
            bochica_altura         REAL,
            bochica_picking        REAL,
            bochica_paso           REAL,
            picking_estimado       REAL,
            referencias_negativas  INTEGER,
            unidades_negativas     REAL,
            sobrante_referencias   INTEGER,
            sobrante_unidades      REAL,
            anomalias_filas        INTEGER,
            anomalias_unidades     REAL,
            fuera_layout_lineas    INTEGER,
            fuera_layout_unidades  REAL
        )
    """,
    "inventario_comparacion": """
        CREATE TABLE IF NOT EXISTS inventario_comparacion (
            referencia          TEXT PRIMARY KEY,
            familia             TEXT,
            es_averia           INTEGER,
            disponible_venta    REAL,
            vendido_no_alistado REAL,
            inventario_teorico  REAL,
            bochica_altura      REAL,
            bochica_picking     REAL,
            bochica_paso        REAL,
            bochica_total       REAL,
            diferencia          REAL,
            sobrante_altura     REAL,
            picking_estimado    REAL,
            corrida_id          INTEGER
        )
    """,
    "catalogo_productos": """
        CREATE TABLE IF NOT EXISTS catalogo_productos (
            referencia        TEXT NOT NULL,
            codigo_barras     TEXT NOT NULL,
            id_producto       TEXT NOT NULL,
            id_especificacion TEXT,
            nombre_comercial  TEXT,
            corrida_id        INTEGER,
            PRIMARY KEY (referencia, codigo_barras)
        )
    """,
    "inventario_salud": """
        CREATE TABLE IF NOT EXISTS inventario_salud (
            referencia     TEXT PRIMARY KEY,
            vigencia       TEXT,
            familia        TEXT,
            disponible     REAL,
            valor_venta    REAL,
            demanda_30d    REAL,
            demanda_90d    REAL,
            demanda_diaria REAL,
            dias_cobertura REAL,
            ultima_salida  TEXT,
            dias_sin_salida REAL,
            estado         TEXT,
            corrida_id     INTEGER
        )
    """,
    "inventario_abc": """
        CREATE TABLE IF NOT EXISTS inventario_abc (
            nivel           TEXT NOT NULL,
            clave           TEXT NOT NULL,
            padre           TEXT,
            etiqueta        TEXT,
            valor_consumo   REAL,
            pct_valor       REAL,
            pct_acumulado   REAL,
            abc             TEXT,
            unidades        REAL,
            cv              REAL,
            meses_con_venta REAL,
            xyz             TEXT,
            celda           TEXT,
            politica        TEXT,
            corrida_id      INTEGER,
            PRIMARY KEY (nivel, clave)
        )
    """,
    "operacion_ciclos": """
        CREATE TABLE IF NOT EXISTS operacion_ciclos (
            id_pedido        TEXT NOT NULL,
            numero_subpedido TEXT NOT NULL,
            dia              TEXT,
            alistador        TEXT,
            n_alistadores    INTEGER,
            inspector        TEXT,
            lineas           REAL,
            unidades         REAL,
            cola_y_picking_h REAL,
            inspeccion_h     REAL,
            ciclo_total_h    REAL,
            corrida_id       INTEGER,
            PRIMARY KEY (id_pedido, numero_subpedido)
        )
    """,
    "operacion_ventanas": """
        CREATE TABLE IF NOT EXISTS operacion_ventanas (
            usuario       TEXT NOT NULL,
            dia           TEXT NOT NULL,
            primer_cierre TEXT,
            ultimo_cierre TEXT,
            ventana_h     REAL,
            subpedidos    INTEGER,
            lineas        REAL,
            unidades      REAL,
            utilizable    INTEGER,
            corrida_id    INTEGER,
            PRIMARY KEY (usuario, dia)
        )
    """,
    "tareas_hallazgos": """
        CREATE TABLE IF NOT EXISTS tareas_hallazgos (
            clave       TEXT PRIMARY KEY,
            titulo      TEXT NOT NULL,
            explicacion TEXT,
            categoria   TEXT,
            prioridad   TEXT,
            origen      TEXT,
            unidad      TEXT,
            cantidad    INTEGER NOT NULL,
            detalle     TEXT,
            medido_en   TEXT,
            corrida_id  INTEGER
        )
    """,
    "cancelaciones_alistadas": """
        CREATE TABLE IF NOT EXISTS cancelaciones_alistadas (
            id_pedido              TEXT NOT NULL,
            numero_subpedido       TEXT NOT NULL,
            mes                    TEXT,
            alistador              TEXT,
            cierre_alistamiento    TEXT,
            cancelado_en           TEXT,
            dias_hasta_cancelacion REAL,
            lineas                 INTEGER,
            unidades               REAL,
            valor                  REAL,
            corrida_id             INTEGER,
            PRIMARY KEY (id_pedido, numero_subpedido)
        )
    """,
    "inventario_sin_ubicacion": """
        CREATE TABLE IF NOT EXISTS inventario_sin_ubicacion (
            id_especificacion TEXT PRIMARY KEY,
            referencia        TEXT,
            nombre_comercial  TEXT,
            vigencia          TEXT,
            inventario        REAL,
            corrida_id        INTEGER
        )
    """,
    "inventario_posiciones": """
        CREATE TABLE IF NOT EXISTS inventario_posiciones (
            ubicacion      TEXT PRIMARY KEY,
            tipo           TEXT,
            rack           TEXT,
            posicion       INTEGER,
            nivel          INTEGER,
            lineas         INTEGER,
            unidades       REAL,
            valor          REAL,
            clase_posicion TEXT,
            ocupada        INTEGER,
            ultimo_conteo  TEXT,
            dias_desde_conteo REAL,
            corrida_id     INTEGER
        )
    """,
    "inventario_ubicaciones": """
        CREATE TABLE IF NOT EXISTS inventario_ubicaciones (
            ubicacion         TEXT NOT NULL,
            id_especificacion TEXT NOT NULL,
            tipo              TEXT,
            rack              TEXT,
            posicion          INTEGER,
            nivel             INTEGER,
            referencia        TEXT,
            familia           TEXT,
            en_catalogo       INTEGER,
            cantidad          REAL,
            clase             TEXT,
            origen_clase      TEXT,
            xyz               TEXT,
            clase_posicion    TEXT,
            precio_unitario   REAL,
            valor_linea       REAL,
            dias_sin_salida   REAL,
            prioridad         INTEGER,
            orden_recorrido   INTEGER,
            corrida_id        INTEGER,
            PRIMARY KEY (ubicacion, id_especificacion)
        )
    """,
    "alertas": """
        CREATE TABLE IF NOT EXISTS alertas (
            clave       TEXT PRIMARY KEY,
            tipo        TEXT,
            severidad   TEXT,
            entidad     TEXT,
            detalle     TEXT,
            valor       REAL,
            modulo      TEXT,
            primera_vez TEXT,
            visto_en    TEXT,
            corrida_id  INTEGER
        )
    """,
    "conteos_archivos": """
        CREATE TABLE IF NOT EXISTS conteos_archivos (
            archivo       TEXT PRIMARY KEY,
            filas         INTEGER,
            modificado_en TEXT,
            primera_vez   TEXT,
            visto_en      TEXT,
            corrida_id    INTEGER
        )
    """,
    "conteos_ira_periodo": """
        CREATE TABLE IF NOT EXISTS conteos_ira_periodo (
            periodo    TEXT NOT NULL,
            clase      TEXT NOT NULL,
            contadas   INTEGER,
            exactas    INTEGER,
            ira        REAL,
            corrida_id INTEGER,
            PRIMARY KEY (periodo, clase)
        )
    """,
    "conteos_conformidad": """
        CREATE TABLE IF NOT EXISTS conteos_conformidad (
            tipo         TEXT PRIMARY KEY,
            posiciones   INTEGER,
            conformes    INTEGER,
            conformidad  REAL,
            ultima_fecha TEXT,
            corrida_id   INTEGER
        )
    """,
    "conteos_ira": """
        CREATE TABLE IF NOT EXISTS conteos_ira (
            clase        TEXT PRIMARY KEY,
            contadas     INTEGER,
            exactas      INTEGER,
            ira          REAL,
            ultima_fecha TEXT,
            corrida_id   INTEGER
        )
    """,
    "conteos": """
        CREATE TABLE IF NOT EXISTS conteos (
            ubicacion         TEXT NOT NULL,
            id_especificacion TEXT NOT NULL,
            fecha             TEXT NOT NULL,
            contado_por       TEXT NOT NULL,
            cantidad_contada  REAL,
            cantidad_sistema  REAL,
            diferencia        REAL,
            diferencia_pct    REAL,
            clase             TEXT,
            tipo              TEXT,
            causa             TEXT,
            tolerancia        REAL,
            exacta            INTEGER,
            hallazgo          TEXT,
            lote              TEXT,
            vencimiento       TEXT,
            actividad_origen  TEXT,
            observacion       TEXT,
            archivo           TEXT,
            intento           INTEGER,
            estado_ciclo      TEXT,
            dias_abierto      REAL,
            corrida_id        INTEGER,
            PRIMARY KEY (ubicacion, id_especificacion, fecha, contado_por)
        )
    """,
    "inventario_anomalias": """
        CREATE TABLE IF NOT EXISTS inventario_anomalias (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            motivo            TEXT,
            ubicacion         TEXT,
            id_especificacion TEXT,
            cantidad          REAL,
            corrida_id        INTEGER
        )
    """,
}

_VIEWS = {
    "v_inventario_comparacion": """
        SELECT referencia, familia, es_averia,
               disponible_venta, vendido_no_alistado, inventario_teorico,
               bochica_altura, bochica_picking, bochica_paso, bochica_total,
               diferencia, sobrante_altura, picking_estimado
        FROM inventario_comparacion
    """,
    "v_inventario_salud": """
        SELECT referencia, vigencia, familia, disponible, valor_venta,
               demanda_30d, demanda_90d, demanda_diaria,
               dias_cobertura, ultima_salida, dias_sin_salida, estado
        FROM inventario_salud
    """,
    "v_inventario_abc": """
        SELECT nivel, clave, padre, etiqueta, valor_consumo, pct_valor,
               pct_acumulado, abc, unidades, cv, meses_con_venta, xyz,
               celda, politica
        FROM inventario_abc
    """,
    "v_operacion_ciclos": """
        SELECT id_pedido, numero_subpedido, dia, alistador, n_alistadores,
               inspector, lineas, unidades,
               cola_y_picking_h, inspeccion_h, ciclo_total_h
        FROM operacion_ciclos
    """,
    "v_operacion_ventanas": """
        SELECT usuario, dia, primer_cierre, ultimo_cierre, ventana_h,
               subpedidos, lineas, unidades, utilizable
        FROM operacion_ventanas
    """,
    "v_cancelaciones_alistadas": """
        SELECT id_pedido, numero_subpedido, mes, alistador,
               cierre_alistamiento, cancelado_en, dias_hasta_cancelacion,
               lineas, unidades, valor
        FROM cancelaciones_alistadas
    """,
    "v_inventario_sin_ubicacion": """
        SELECT id_especificacion, referencia, nombre_comercial, vigencia, inventario
        FROM inventario_sin_ubicacion
    """,
    "v_inventario_posiciones": """
        SELECT ubicacion, tipo, rack, posicion, nivel,
               lineas, unidades, valor, clase_posicion, ocupada,
               ultimo_conteo, dias_desde_conteo
        FROM inventario_posiciones
    """,
    "v_inventario_ubicaciones": """
        SELECT ubicacion, tipo, rack, posicion, nivel,
               id_especificacion, referencia, familia, en_catalogo, cantidad,
               clase, origen_clase, xyz, clase_posicion,
               precio_unitario, valor_linea, dias_sin_salida, prioridad,
               orden_recorrido
        FROM inventario_ubicaciones
    """,
    "v_alertas": """
        SELECT a.clave, a.tipo, a.severidad, a.entidad, a.detalle, a.valor,
               a.modulo, a.primera_vez, a.visto_en,
               -- Activa = se volvió a emitir en la última corrida. Una alerta
               -- que dejó de emitirse quedó resuelta, y su `visto_en` marca
               -- cuándo: de ahí sale el tiempo de resolución.
               --
               -- Se compara por `corrida_id`, no por `visto_en`: el timestamp
               -- tiene resolución de segundos, así que dos corridas seguidas
               -- lo bastante rápido dejaban a TODAS las alertas como activas.
               CASE WHEN a.corrida_id = (SELECT MAX(corrida_id) FROM alertas)
                    THEN 1 ELSE 0 END AS activa
        FROM alertas a
    """,
    "v_conteos_archivos": """
        SELECT archivo, filas, modificado_en, primera_vez, visto_en
        FROM conteos_archivos
    """,
    "v_conteos_ira_periodo": """
        SELECT periodo, clase, contadas, exactas, ira FROM conteos_ira_periodo
    """,
    "v_conteos_conformidad": """
        SELECT tipo, posiciones, conformes, conformidad, ultima_fecha
        FROM conteos_conformidad
    """,
    "v_conteos_ira": """
        SELECT clase, contadas, exactas, ira, ultima_fecha FROM conteos_ira
    """,
    "v_conteos": """
        SELECT ubicacion, id_especificacion, fecha, contado_por,
               cantidad_contada, cantidad_sistema, diferencia, diferencia_pct,
               clase, tipo, causa, tolerancia, exacta, hallazgo,
               lote, vencimiento, actividad_origen, observacion, archivo,
               intento, estado_ciclo, dias_abierto
        FROM conteos
    """,
    "v_inventario_anomalias": """
        SELECT motivo, ubicacion, id_especificacion, cantidad
        FROM inventario_anomalias
    """,
    "v_inventario_corridas": """
        SELECT id, ejecutado_en,
               admin_actualizado_en, bochica_actualizado_en, layout_actualizado_en,
               fuente_mas_vieja_h, datos_desactualizados,
               referencias, disponible_venta, vendido_no_alistado,
               inventario_teorico, bochica_altura, bochica_picking, bochica_paso,
               picking_estimado, sobrante_referencias, sobrante_unidades,
               anomalias_filas, anomalias_unidades,
               fuera_layout_lineas, fuera_layout_unidades
        FROM inventario_corridas
    """,
}

_COLUMNAS_COMPARACION = (
    "referencia",
    "familia",
    "es_averia",
    "disponible_venta",
    "vendido_no_alistado",
    "inventario_teorico",
    "bochica_altura",
    "bochica_picking",
    "bochica_paso",
    "bochica_total",
    "diferencia",
    "sobrante_altura",
    "picking_estimado",
)

_COLUMNAS_ANOMALIAS = ("motivo", "ubicacion", "id_especificacion", "cantidad")

_COLUMNAS_CATALOGO = (
    "referencia",
    "codigo_barras",
    "id_producto",
    "id_especificacion",
    "nombre_comercial",
)

_COLUMNAS_OPERACION = (
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
)

_COLUMNAS_VENTANAS = (
    "usuario",
    "dia",
    "primer_cierre",
    "ultimo_cierre",
    "ventana_h",
    "subpedidos",
    "lineas",
    "unidades",
    "utilizable",
)

_COLUMNAS_ABC = (
    "nivel",
    "clave",
    "padre",
    "etiqueta",
    "valor_consumo",
    "pct_valor",
    "pct_acumulado",
    "abc",
    "unidades",
    "cv",
    "meses_con_venta",
    "xyz",
    "celda",
    "politica",
)

_COLUMNAS_CANCELACIONES = (
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
)

_COLUMNAS_POSICIONES = (
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
)

_COLUMNAS_UBICACIONES = (
    "ubicacion",
    "id_especificacion",
    "tipo",
    "rack",
    "posicion",
    "nivel",
    "referencia",
    "familia",
    # DEC-072: alcance de trabajo — ID reconocido por el catálogo del admin.
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
    # DEC-078: orden de visita física.
    "orden_recorrido",
)

_COLUMNAS_SIN_UBICACION = (
    "id_especificacion",
    "referencia",
    "nombre_comercial",
    "vigencia",
    "inventario",
)

_COLUMNAS_IRA_PERIODO = ("periodo", "clase", "contadas", "exactas", "ira")

_COLUMNAS_CONFORMIDAD = ("tipo", "posiciones", "conformes", "conformidad", "ultima_fecha")

_COLUMNAS_IRA = ("clase", "contadas", "exactas", "ira", "ultima_fecha")

_COLUMNAS_CONTEOS = (
    "ubicacion",
    "id_especificacion",
    "fecha",
    "contado_por",
    "cantidad_contada",
    "cantidad_sistema",
    "diferencia",
    "diferencia_pct",
    "clase",
    "tipo",
    "causa",
    "tolerancia",
    "exacta",
    "hallazgo",
    "lote",
    "vencimiento",
    "actividad_origen",
    "observacion",
    "archivo",
    # DEC-070: cierre del ciclo — recuento y ajuste.
    "intento",
    "estado_ciclo",
    "dias_abierto",
)

_COLUMNAS_SALUD = (
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
)


def construir_catalogo_productos(df_admin: pd.DataFrame) -> pd.DataFrame:
    """Arma el puente `(referencia, código de barras)` → IDs de producto (DEC-045/DEC-111).

    `lineas_pedido` no guarda ningún ID de producto: identifica el artículo
    por referencia, código de barras, nombre y presentación. Los IDs viven
    solo en el catálogo del admin, así que esta tabla es la que permite
    mostrarlos en la vista consolidada de Pedidos.

    **La fila solo existe si el par resuelve a un único `id_producto`.**
    El código de barras no es único por ID (un mismo código cuelga de hasta
    21 referencias — la misma arena vendida por unidad, tonelada o
    corporativo), y si un par quedara con dos IDs el LEFT JOIN del dashboard
    **duplicaría la línea de pedido e inflaría `Cantidad comprada`**. Medido:
    el 99,3% de los pares es inequívoco; el resto se descarta a propósito y
    esas líneas muestran el ID vacío. `inventario/hallazgos.py` (detector
    `lineas_sin_id_producto`) depende de este criterio — no cambia con
    DEC-111.

    **`id_especificacion` se resuelve aparte, con su propio criterio de
    unicidad, dentro del mismo grupo (DEC-111).** Es más fino que
    `id_producto` — variantes de color o lote comparten producto pero no
    especificación — así que un par ya incluido por `id_producto` puede
    seguir sin `id_especificacion` (queda NULL, no se descarta la fila).
    Medido 2026-08-12 contra el admin real (6.212 pares, 9.018 filas): de
    los 6.171 pares que entran por `id_producto`, 552 (8,9%) tienen más de
    un `id_especificacion` y quedan con ese campo vacío — el dashboard
    consolida sobre `id_especificacion`, no sobre `id_producto`.

    Se usa el catálogo **sin filtrar por alcance**: acá interesa poder
    nombrar cualquier producto que aparezca en un pedido, incluidas las
    arenas y los otros almacenes que `filtrar_alcance_admin()` excluye del
    cruce de inventario.

    Args:
        df_admin: Resultado de `inventario.normalizador.cargar_admin()`.

    Returns:
        DataFrame con `referencia`, `codigo_barras`, `id_producto`,
        `id_especificacion` (puede ser NULL) y `nombre_comercial`; una fila
        por par cuyo `id_producto` es inequívoco.
    """
    columnas = [
        "referencia",
        "codigo_barras",
        "id_producto_admin",
        "id_especificacion",
        "nombre_comercial",
    ]
    df = df_admin[columnas].copy()
    df["referencia"] = df["referencia"].astype(str).str.strip()
    df["codigo_barras"] = df["codigo_barras"].astype(str).str.strip()
    df["id_producto"] = df["id_producto_admin"].astype(str).str.strip()
    df["id_especificacion"] = df["id_especificacion"].astype(str).str.strip()

    total = df[["referencia", "codigo_barras"]].drop_duplicates().shape[0]
    por_par = df.groupby(["referencia", "codigo_barras"])["id_producto"].nunique()
    inequivocos = por_par[por_par == 1].index

    puente = (
        df.set_index(["referencia", "codigo_barras"])
        .loc[df.set_index(["referencia", "codigo_barras"]).index.isin(inequivocos)]
        .reset_index()
        .drop_duplicates(subset=["referencia", "codigo_barras"])
    )
    descartados = total - len(puente)
    if descartados:
        logger.info(
            "construir_catalogo_productos: %d/%d pares descartados por ambigüedad "
            "(más de un id_producto) — esas líneas quedan sin ID (DEC-045)",
            descartados,
            total,
        )

    # DEC-111: id_especificacion resuelto con su propio criterio de unicidad,
    # calculado sobre el df completo (no sobre `puente`, que ya perdió las
    # filas descartadas por id_producto ambiguo). None cuando el mismo par
    # tiene más de un id_especificacion distinto.
    espec_unico = df.groupby(["referencia", "codigo_barras"])["id_especificacion"].agg(
        lambda s: s.iloc[0] if s.nunique() == 1 else None
    )
    puente = puente.drop(columns=["id_especificacion"]).merge(
        espec_unico.rename("id_especificacion"),
        on=["referencia", "codigo_barras"],
        how="left",
    )
    return puente[list(_COLUMNAS_CATALOGO)]


def init_schema(con: sqlite3.Connection) -> None:
    """Crea tablas y VIEWs si no existen. Idempotente.

    Args:
        con: Conexión abierta a pedidos.db.
    """
    for sql in _TABLAS.values():
        con.execute(sql)
    _recrear_si_cambio_la_forma(con)
    _migrar_columnas(con)
    _crear_views(con)


# Tablas que son snapshots puros: se reescriben enteras en cada corrida, así
# que recrearlas no pierde información. Se listan explícitamente para que un
# DROP nunca alcance a una tabla con historia (`inventario_corridas`).
_SNAPSHOTS_RECREABLES = {"inventario_abc": "nivel"}


def _recrear_si_cambio_la_forma(con: sqlite3.Connection) -> None:
    """Recrea un snapshot cuando su esquema cambió de forma, no solo de columnas.

    `_migrar_columnas()` resuelve columnas agregadas, pero no un cambio de
    llave primaria: `inventario_abc` pasó de estar indexada por
    `referencia` a estarlo por `(nivel, clave)` al volverse jerárquica
    (DEC-053). Se detecta por la ausencia de la columna testigo y se
    recrea, que es seguro porque la tabla se repuebla entera enseguida.
    """
    for tabla, testigo in _SNAPSHOTS_RECREABLES.items():
        columnas = {fila[1] for fila in con.execute(f"PRAGMA table_info({tabla})")}
        if columnas and testigo not in columnas:
            con.execute(f"DROP TABLE {tabla}")
            con.execute(_TABLAS[tabla])
            logger.info("_recrear_si_cambio_la_forma: %s recreada con el esquema nuevo", tabla)


def _migrar_columnas(con: sqlite3.Connection) -> None:
    """Agrega las columnas que falten en tablas que ya existían.

    `CREATE TABLE IF NOT EXISTS` no toca una tabla ya creada, así que sumar
    una columna al esquema no llega a las bases existentes y el INSERT
    falla con "has no column named ...". Mismo patrón forward-compatible
    que usa `scraper/db.py` (AUD-B8): se comparan las columnas declaradas
    contra las reales y se agregan las que faltan.

    Es seguro en estas tablas porque todas son snapshots que se reescriben
    enteros en cada corrida: una columna nueva queda en NULL hasta el
    siguiente `persistir()`, que es inmediato.
    """
    for tabla, ddl in _TABLAS.items():
        existentes = {fila[1] for fila in con.execute(f"PRAGMA table_info({tabla})")}
        if not existentes:
            continue
        for declarada, tipo in _columnas_declaradas(ddl):
            if declarada not in existentes:
                con.execute(f"ALTER TABLE {tabla} ADD COLUMN {declarada} {tipo}")
                logger.info("_migrar_columnas: %s.%s agregada", tabla, declarada)
    _backfill_sobrante(con)


def _backfill_sobrante(con: sqlite3.Connection) -> None:
    """Rellena el sobrante en las corridas anteriores a DEC-051.

    Esas corridas ya traen el dato, solo que con el nombre de la lectura
    vieja: `referencias_negativas` es el conteo de referencias con sobrante
    y `unidades_negativas` es la misma cifra con el signo invertido. No hay
    que recalcular nada — se copia, y así la tendencia arranca con toda la
    historia disponible en vez de desde cero.
    """
    cambios = con.execute(
        """UPDATE inventario_corridas
           SET sobrante_referencias = referencias_negativas,
               sobrante_unidades    = -unidades_negativas
           WHERE sobrante_unidades IS NULL AND unidades_negativas IS NOT NULL"""
    ).rowcount
    # El UPDATE abre una transacción implícita bajo el isolation_level
    # legacy de sqlite3, y sin cerrarla el `BEGIN IMMEDIATE` de
    # `_crear_views()` revienta con "cannot start a transaction within a
    # transaction" — dejando las VIEWs con la definición vieja y la corrida
    # entera sin persistir. Los ALTER de arriba no dan problema porque el
    # DDL no abre transacción implícita; solo el DML lo hace.
    con.commit()
    if cambios:
        logger.info("_backfill_sobrante: %d corridas históricas completadas", cambios)


def _columnas_declaradas(ddl: str) -> list[tuple[str, str]]:
    """Extrae (nombre, tipo) del cuerpo de un CREATE TABLE del módulo."""
    cuerpo = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    columnas = []
    for linea in cuerpo.splitlines():
        partes = linea.strip().rstrip(",").split()
        # Se saltan las restricciones de tabla (PRIMARY KEY (...), etc.).
        if len(partes) >= 2 and partes[0].isidentifier() and partes[0].upper() != "PRIMARY":
            columnas.append((partes[0], partes[1]))
    return columnas


def _crear_views(con: sqlite3.Connection) -> None:
    """Recrea las VIEWs de inventario dentro de una transacción (DEC-019).

    Sin transacción explícita cada DROP VIEW se materializa al instante y
    deja una ventana en la que el dashboard recibe "no such view" — la
    causa raíz de HAL-007. `BEGIN IMMEDIATE` toma el write lock de
    entrada; con WAL los lectores ven el snapshot anterior hasta el COMMIT.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        for nombre, sql in _VIEWS.items():
            con.execute(f"DROP VIEW IF EXISTS {nombre}")
            con.execute(f"CREATE VIEW {nombre} AS {sql}")
    except Exception:
        con.rollback()
        raise
    con.commit()


def _mtime_iso(path: Path) -> str | None:
    """Fecha de última modificación de un archivo, en ISO UTC, o None si no está."""
    if not path.exists():
        return None
    marca = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return marca.isoformat(timespec="seconds")


def medir_frescura(
    admin: Path, bochica: Path, layout: Path, *, ahora: dt.datetime | None = None
) -> dict[str, object]:
    """Mide la antigüedad de las tres fuentes (DEC-043).

    Solo `admin` y `bochica` cuentan para el umbral: son las que el
    scheduler refresca cada hora. El layout es un documento manual y es
    normal que tenga días o semanas — se registra su fecha pero no marca
    nada.

    Args:
        admin: Ruta al Excel del sistema administrativo.
        bochica: Ruta al Excel de Bochica.
        layout: Ruta al layout de bodega.
        ahora: Momento de referencia (inyectable para tests). Default: UTC now.

    Returns:
        Diccionario con las 3 fechas ISO, `fuente_mas_vieja_h` (la peor de
        las dos automáticas) y el flag `datos_desactualizados`.
    """
    referencia = ahora or dt.datetime.now(tz=dt.timezone.utc)
    fechas = {
        "admin_actualizado_en": _mtime_iso(admin),
        "bochica_actualizado_en": _mtime_iso(bochica),
        "layout_actualizado_en": _mtime_iso(layout),
    }

    edades = []
    faltantes = []
    for clave in ("admin_actualizado_en", "bochica_actualizado_en"):
        valor = fechas[clave]
        if valor is None:
            faltantes.append(clave)
        else:
            edades.append((referencia - dt.datetime.fromisoformat(valor)).total_seconds() / 3600)

    mas_vieja = max(edades) if edades else None
    # Una fuente ausente es peor que una vieja: marca desactualizado sin
    # importar qué tan fresca esté la otra.
    desactualizado = bool(faltantes) or mas_vieja is None or mas_vieja > UMBRAL_DESACTUALIZADO_H
    if desactualizado:
        logger.warning(
            "medir_frescura: fuentes no confiables (más vieja: %s h, umbral %s h, "
            "faltantes: %s) — probable descarga fallida; el dashboard lo va a advertir",
            f"{mas_vieja:.1f}" if mas_vieja is not None else "n/a",
            UMBRAL_DESACTUALIZADO_H,
            faltantes or "ninguna",
        )

    return {**fechas, "fuente_mas_vieja_h": mas_vieja, "datos_desactualizados": int(desactualizado)}


def _resumir(comparacion: pd.DataFrame, anomalias: pd.DataFrame) -> dict[str, object]:
    """Calcula los agregados de la corrida que van a `inventario_corridas`."""
    # DEC-051: el sobrante en altura es el hallazgo. Se sigue guardando con
    # el nombre viejo además del nuevo para no dejar en NULL una columna que
    # las corridas históricas sí tienen poblada.
    con_sobrante = comparacion[comparacion["sobrante_altura"] > 0]
    return {
        "referencias": len(comparacion),
        "disponible_venta": float(comparacion["disponible_venta"].sum()),
        "vendido_no_alistado": float(comparacion["vendido_no_alistado"].sum()),
        "inventario_teorico": float(comparacion["inventario_teorico"].sum()),
        "bochica_altura": float(comparacion["bochica_altura"].sum()),
        "bochica_picking": float(comparacion["bochica_picking"].sum()),
        "bochica_paso": float(comparacion["bochica_paso"].sum()),
        "picking_estimado": float(comparacion["picking_estimado"].sum()),
        "sobrante_referencias": len(con_sobrante),
        "sobrante_unidades": float(con_sobrante["sobrante_altura"].sum()),
        "referencias_negativas": len(con_sobrante),
        "unidades_negativas": -float(con_sobrante["sobrante_altura"].sum()),
        "anomalias_filas": len(anomalias),
        "anomalias_unidades": float(anomalias["cantidad"].sum()) if len(anomalias) else 0.0,
    }


def persistir(
    comparacion: pd.DataFrame,
    anomalias: pd.DataFrame,
    frescura: dict[str, object],
    db_path: str | None = None,
    fuera_layout: dict[str, object] | None = None,
    catalogo: pd.DataFrame | None = None,
    hallazgos: list | None = None,
    salud: pd.DataFrame | None = None,
    abc: pd.DataFrame | None = None,
    operacion: pd.DataFrame | None = None,
    ventanas: pd.DataFrame | None = None,
    ubicaciones: pd.DataFrame | None = None,
    sin_ubicacion: pd.DataFrame | None = None,
    conteos: pd.DataFrame | None = None,
    ira: pd.DataFrame | None = None,
    archivos: pd.DataFrame | None = None,
    alertas: pd.DataFrame | None = None,
    posiciones: pd.DataFrame | None = None,
    ira_periodo: pd.DataFrame | None = None,
    conformidad: pd.DataFrame | None = None,
    cancelaciones: pd.DataFrame | None = None,
) -> int:
    """Escribe el snapshot de la corrida, reemplazando el anterior.

    Todo ocurre en una sola transacción `BEGIN IMMEDIATE` (DEC-019): un
    dashboard leyendo concurrentemente ve el snapshot anterior completo
    hasta el COMMIT, nunca una tabla a medio poblar.

    Args:
        comparacion: Resultado de `inventario.comparacion.comparar()`.
        anomalias: Resultado de `inventario.comparacion.anomalias_layout()`.
        frescura: Resultado de `medir_frescura()`.
        db_path: Ruta a pedidos.db. Default: `comun.get_db_path()`.
        catalogo: Resultado de `construir_catalogo_productos()` (DEC-045).
            Si es None, `catalogo_productos` se deja intacta — así un
            problema del cruce de inventario no borra el puente que
            necesita la vista de Pedidos.
        hallazgos: Resultado de `inventario.hallazgos.detectar_todos()`
            (DEC-047). Si es None, `tareas_hallazgos` queda intacta.
        salud: Resultado de `inventario.salud.calcular_salud()` (DEC-049).
            Si es None, `inventario_salud` queda intacta.
        abc: Resultado de `inventario.clasificacion.calcular_clasificacion()`
            (DEC-050). Si es None, `inventario_abc` queda intacta.
        operacion: Resultado de `inventario.operacion.calcular_operacion()`
            (DEC-054). Si es None, `operacion_ciclos` queda intacta.
        ventanas: Resultado de `inventario.operacion.calcular_ventanas()`
            (DEC-055). Si es None, `operacion_ventanas` queda intacta.
        ubicaciones: Resultado de
            `inventario.ubicaciones.calcular_ubicaciones()` (DEC-057). Si
            es None, `inventario_ubicaciones` queda intacta.
        conteos: Resultado de `inventario.conteos.evaluar_conteos()`
            (DEC-058). **Espejo de `data/conteos/`**: se reemplaza entera,
            para que sacar una hoja de la carpeta deshaga su conteo. `None`
            deja la tabla intacta (carpeta ausente ≠ carpeta vacía).
        archivos: Resultado de `inventario.conteos.inventariar_archivos()`.
            Este sí acumula: es la constancia de que una hoja existió.
        alertas: Resultado de `inventario.alertas.generar_alertas()`
            (DEC-059). Se upsertan conservando la fecha de aparición.
        posiciones: Resultado de `inventario.ubicaciones.mapa_posiciones()`
            (DEC-061). Incluye las posiciones vacías.
        ira_periodo: Resultado de `inventario.conteos.ira_por_periodo()`
            (DEC-062). Snapshot: se recalcula entero desde `conteos`.
        conformidad: Resultado de
            `inventario.conteos.conformidad_por_tipo()` (DEC-062).
        cancelaciones: Resultado de
            `inventario.cancelaciones.calcular_cancelaciones()` (DEC-063).
        ira: Resultado de `inventario.conteos.calcular_ira()` (DEC-058).
            Este sí es snapshot: se recalcula entero desde `conteos`.

    Returns:
        El `id` de la corrida registrada en `inventario_corridas`.
    """
    resumen = _resumir(comparacion, anomalias)
    ejecutado_en = dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds")
    # DEC-076: `fuera_layout` va aparte de `resumen` porque no describe lo
    # comparado sino lo que quedó afuera — el 47% de Bochica.
    fila_corrida = {
        "ejecutado_en": ejecutado_en,
        **frescura,
        **resumen,
        **(fuera_layout or {}),
    }

    con = sqlite3.connect(db_path or get_db_path(), timeout=30)
    try:
        init_schema(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            columnas = ", ".join(fila_corrida)
            marcas = ", ".join("?" * len(fila_corrida))
            cur = con.execute(
                f"INSERT INTO inventario_corridas ({columnas}) VALUES ({marcas})",
                tuple(fila_corrida.values()),
            )
            corrida_id = int(cur.lastrowid or 0)

            # Snapshot: solo la última corrida vive en estas dos tablas
            # (DEC-043 — el detalle por referencia de cada corrida serían
            # ~12,5M filas/año). El historial de agregados va en
            # inventario_corridas, que sí acumula.
            con.execute("DELETE FROM inventario_comparacion")
            con.execute("DELETE FROM inventario_anomalias")

            con.executemany(
                f"INSERT INTO inventario_comparacion "
                f"({', '.join(_COLUMNAS_COMPARACION)}, corrida_id) "
                f"VALUES ({', '.join('?' * len(_COLUMNAS_COMPARACION))}, ?)",
                [
                    (*_normalizar(fila), corrida_id)
                    for fila in comparacion[list(_COLUMNAS_COMPARACION)].itertuples(index=False)
                ],
            )
            if len(anomalias):
                con.executemany(
                    f"INSERT INTO inventario_anomalias "
                    f"({', '.join(_COLUMNAS_ANOMALIAS)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_ANOMALIAS))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in anomalias[list(_COLUMNAS_ANOMALIAS)].itertuples(index=False)
                    ],
                )

            # DEC-047: los hallazgos de calidad de datos. El DELETE total
            # es lo que hace que una tarea resuelta desaparezca: si el
            # detector ya no encuentra casos, no se vuelve a insertar.
            if hallazgos is not None:
                con.execute("DELETE FROM tareas_hallazgos")
                con.executemany(
                    """INSERT INTO tareas_hallazgos
                       (clave, titulo, explicacion, categoria, prioridad, origen,
                        unidad, cantidad, detalle, medido_en, corrida_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            h.clave,
                            h.titulo,
                            h.explicacion,
                            h.categoria,
                            h.prioridad,
                            h.origen,
                            h.unidad,
                            h.cantidad,
                            h.filas.to_json(orient="split", index=False),
                            ejecutado_en,
                            corrida_id,
                        )
                        for h in hallazgos
                    ],
                )

            # DEC-049: cobertura y movimiento por referencia.
            if salud is not None:
                con.execute("DELETE FROM inventario_salud")
                con.executemany(
                    f"INSERT INTO inventario_salud "
                    f"({', '.join(_COLUMNAS_SALUD)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_SALUD))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in salud[list(_COLUMNAS_SALUD)].itertuples(index=False)
                    ],
                )

            # DEC-050: clasificación ABC-XYZ.
            if abc is not None:
                con.execute("DELETE FROM inventario_abc")
                con.executemany(
                    f"INSERT INTO inventario_abc "
                    f"({', '.join(_COLUMNAS_ABC)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_ABC))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in abc[list(_COLUMNAS_ABC)].itertuples(index=False)
                    ],
                )

            # DEC-054: tiempos de ciclo y carga por subpedido.
            if operacion is not None:
                con.execute("DELETE FROM operacion_ciclos")
                con.executemany(
                    f"INSERT INTO operacion_ciclos "
                    f"({', '.join(_COLUMNAS_OPERACION)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_OPERACION))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in operacion[list(_COLUMNAS_OPERACION)].itertuples(index=False)
                    ],
                )

            # DEC-055: ventana activa por alistador y día.
            if ventanas is not None:
                con.execute("DELETE FROM operacion_ventanas")
                con.executemany(
                    f"INSERT INTO operacion_ventanas "
                    f"({', '.join(_COLUMNAS_VENTANAS)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_VENTANAS))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in ventanas[list(_COLUMNAS_VENTANAS)].itertuples(index=False)
                    ],
                )

            # Las dos series derivadas de `conteos` son snapshot por la
            # misma razón que el IRA: se recalculan enteras.
            for tabla, columnas, datos in (
                ("conteos_ira_periodo", _COLUMNAS_IRA_PERIODO, ira_periodo),
                ("conteos_conformidad", _COLUMNAS_CONFORMIDAD, conformidad),
            ):
                if datos is not None:
                    con.execute(f"DELETE FROM {tabla}")  # noqa: S608 — nombre literal
                    if len(datos):
                        con.executemany(
                            f"INSERT INTO {tabla} "  # noqa: S608
                            f"({', '.join(columnas)}, corrida_id) "
                            f"VALUES ({', '.join('?' * len(columnas))}, ?)",
                            [
                                (*_normalizar(fila), corrida_id)
                                for fila in datos[list(columnas)].itertuples(index=False)
                            ],
                        )

            # El IRA sí es snapshot: se deriva por completo de `conteos`,
            # así que reemplazarlo no pierde nada.
            if ira is not None:
                con.execute("DELETE FROM conteos_ira")
                con.executemany(
                    f"INSERT INTO conteos_ira "
                    f"({', '.join(_COLUMNAS_IRA)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_IRA))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in ira[list(_COLUMNAS_IRA)].itertuples(index=False)
                    ],
                )

            # DEC-059: las alertas se upsertan conservando `primera_vez`.
            # No se borran las que dejaron de emitirse: su `visto_en` viejo
            # las marca como resueltas y permite medir cuánto tardaron.
            if alertas is not None and len(alertas):
                con.executemany(
                    """INSERT INTO alertas
                           (clave, tipo, severidad, entidad, detalle, valor, modulo,
                            primera_vez, visto_en, corrida_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(clave) DO UPDATE SET
                           tipo = excluded.tipo,
                           severidad = excluded.severidad,
                           entidad = excluded.entidad,
                           detalle = excluded.detalle,
                           valor = excluded.valor,
                           modulo = excluded.modulo,
                           visto_en = excluded.visto_en,
                           corrida_id = excluded.corrida_id""",
                    [
                        (
                            f.clave,
                            f.tipo,
                            f.severidad,
                            f.entidad,
                            f.detalle,
                            None if pd.isna(f.valor) else float(f.valor),
                            f.modulo,
                            ejecutado_en,
                            ejecutado_en,
                            corrida_id,
                        )
                        for f in alertas.itertuples(index=False)
                    ],
                )

            # DEC-058: `conteos` es un ESPEJO de `data/conteos/`, no un
            # acumulado. Se reconstruye entera para que sacar una hoja de la
            # carpeta deshaga su conteo — la operación de corrección que el
            # área necesita y que un acumulado hacía imposible.
            #
            # El `None` sigue significando "no toques la tabla": es el caso
            # de la carpeta ausente, distinto de la carpeta vacía.
            if conteos is not None:
                con.execute("DELETE FROM conteos")
                if len(conteos):
                    con.executemany(
                        f"INSERT INTO conteos "
                        f"({', '.join(_COLUMNAS_CONTEOS)}, corrida_id) "
                        f"VALUES ({', '.join('?' * len(_COLUMNAS_CONTEOS))}, ?)",
                        [
                            (*_normalizar(fila), corrida_id)
                            for fila in conteos[list(_COLUMNAS_CONTEOS)].itertuples(index=False)
                        ],
                    )

            # El inventario de archivos SÍ acumula: es la constancia de que
            # una hoja existió. `primera_vez` se conserva con COALESCE sobre
            # el valor ya guardado, así el upsert no la pisa.
            if archivos is not None and len(archivos):
                con.executemany(
                    """INSERT INTO conteos_archivos
                           (archivo, filas, modificado_en, primera_vez, visto_en, corrida_id)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(archivo) DO UPDATE SET
                           filas = excluded.filas,
                           modificado_en = excluded.modificado_en,
                           visto_en = excluded.visto_en,
                           corrida_id = excluded.corrida_id""",
                    [
                        (
                            fila.archivo,
                            fila.filas,
                            fila.modificado_en,
                            fila.visto_en,
                            fila.visto_en,
                            corrida_id,
                        )
                        for fila in archivos.itertuples(index=False)
                    ],
                )

            # DEC-063: snapshot — se recalcula entero desde subpedidos.
            if cancelaciones is not None:
                con.execute("DELETE FROM cancelaciones_alistadas")
                if len(cancelaciones):
                    con.executemany(
                        f"INSERT INTO cancelaciones_alistadas "
                        f"({', '.join(_COLUMNAS_CANCELACIONES)}, corrida_id) "
                        f"VALUES ({', '.join('?' * len(_COLUMNAS_CANCELACIONES))}, ?)",
                        [
                            (*_normalizar(fila), corrida_id)
                            for fila in cancelaciones[list(_COLUMNAS_CANCELACIONES)].itertuples(
                                index=False
                            )
                        ],
                    )

            # DEC-073: producto que el admin declara y nadie puede ubicar.
            # Es lo que el plan de conteo por construcción no puede
            # descubrir: solo manda gente donde el sistema ya ve algo.
            if sin_ubicacion is not None:
                con.execute("DELETE FROM inventario_sin_ubicacion")
                con.executemany(
                    f"INSERT INTO inventario_sin_ubicacion "
                    f"({', '.join(_COLUMNAS_SIN_UBICACION)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_SIN_UBICACION))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in sin_ubicacion[list(_COLUMNAS_SIN_UBICACION)].itertuples(
                            index=False
                        )
                    ],
                )

            # DEC-061: el mapa de bodega — incluye las posiciones VACÍAS,
            # que son la mitad de la información en un mapa de ocupación.
            if posiciones is not None:
                con.execute("DELETE FROM inventario_posiciones")
                con.executemany(
                    f"INSERT INTO inventario_posiciones "
                    f"({', '.join(_COLUMNAS_POSICIONES)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_POSICIONES))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in posiciones[list(_COLUMNAS_POSICIONES)].itertuples(index=False)
                    ],
                )

            # DEC-057: líneas SKU-posición, la unidad de conteo del plan.
            if ubicaciones is not None:
                con.execute("DELETE FROM inventario_ubicaciones")
                con.executemany(
                    f"INSERT INTO inventario_ubicaciones "
                    f"({', '.join(_COLUMNAS_UBICACIONES)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_UBICACIONES))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in ubicaciones[list(_COLUMNAS_UBICACIONES)].itertuples(index=False)
                    ],
                )

            # DEC-045: el puente producto↔pedido. Va en la misma transacción
            # para que el dashboard nunca lo vea a medio poblar.
            if catalogo is not None:
                con.execute("DELETE FROM catalogo_productos")
                con.executemany(
                    f"INSERT INTO catalogo_productos "
                    f"({', '.join(_COLUMNAS_CATALOGO)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_CATALOGO))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in catalogo[list(_COLUMNAS_CATALOGO)].itertuples(index=False)
                    ],
                )
        except Exception:
            con.rollback()
            raise
        con.commit()
    finally:
        con.close()

    logger.info(
        "persistir: corrida %d — %d referencias, %d anomalías, desactualizado=%s",
        corrida_id,
        resumen["referencias"],
        resumen["anomalias_filas"],
        frescura["datos_desactualizados"],
    )
    return corrida_id


def _normalizar(fila: tuple) -> tuple:
    """Convierte los tipos de numpy/pandas a tipos que sqlite3 acepta.

    `numpy.bool_` y `numpy.int64` no son adaptables por sqlite3 y
    revientan con InterfaceError; `NaN` pasa a NULL.
    """
    valores = []
    for v in fila:
        if isinstance(v, bool):
            valores.append(int(v))
        elif v is None or (isinstance(v, float) and pd.isna(v)):
            valores.append(None)
        elif hasattr(v, "item"):  # escalares de numpy
            valores.append(v.item())
        elif pd.isna(v):
            valores.append(None)
        else:
            valores.append(v)
    return tuple(valores)


def main() -> int:
    """Carga las tres fuentes, calcula la comparación y la persiste.

    Composition root del paso: es lo único que `actualizar_pedidos.bat`
    necesita invocar. AUD-B7: retorna el exit code, `sys.exit` vive en
    `__main__`.

    Returns:
        0 si la corrida se persistió, 1 si faltó una fuente o falló la
        escritura.
    """
    from inventario.alertas import generar_alertas
    from inventario.cancelaciones import calcular_cancelaciones
    from inventario.clasificacion import calcular_clasificacion
    from inventario.comparacion import anomalias_layout, calcular_vendido_no_alistado, comparar
    from inventario.conteos import (
        RUTA_CONTEOS_DEFAULT,
        calcular_ira,
        cargar_conteos,
        conformidad_por_tipo,
        evaluar_conteos,
        inventariar_archivos,
        ira_por_periodo,
    )
    from inventario.hallazgos import detectar_todos
    from inventario.layout import (
        RUTA_LAYOUT_DEFAULT,
        cargar_distribucion,
        cargar_layout,
        clasificar_ubicaciones,
        solo_layout,
    )
    from inventario.normalizador import cargar_admin, cargar_bochica, filtrar_alcance_admin
    from inventario.operacion import calcular_operacion, calcular_ventanas
    from inventario.salud import calcular_salud
    from inventario.ubicaciones import (
        calcular_ubicaciones,
        mapa_posiciones,
        sin_ubicacion_conocida,
    )
    from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
    from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

    try:
        frescura = medir_frescura(ADMIN_XLSX, BOCHICA_XLSX, RUTA_LAYOUT_DEFAULT)

        catalogo_completo = cargar_admin(ADMIN_XLSX)
        # DEC-045: el puente usa el catálogo SIN filtrar por alcance — acá
        # interesa nombrar cualquier producto que aparezca en un pedido,
        # incluidas arenas y otros almacenes.
        catalogo = construir_catalogo_productos(catalogo_completo)

        admin = filtrar_alcance_admin(catalogo_completo)
        layout = cargar_layout()
        _clasificado = clasificar_ubicaciones(cargar_bochica(BOCHICA_XLSX), layout)
        bochica = solo_layout(_clasificado)
        # DEC-076: cuánto queda fuera del layout, para poder decirlo en el
        # dashboard en vez de dejarlo solo en el log. Es el 47% de Bochica:
        # que la regla sea deliberada no la hace menos grande.
        _fuera = _clasificado[~_clasificado.index.isin(bochica.index)]
        fuera_layout = {
            "fuera_layout_lineas": int(len(_fuera)),
            "fuera_layout_unidades": float(
                pd.to_numeric(_fuera["cantidad"], errors="coerce").fillna(0).sum()
            ),
        }

        comparacion = comparar(admin, bochica, calcular_vendido_no_alistado())
        # DEC-077: la política de slotting habilita el cuarto motivo
        # (`familia_fuera_de_rack`). Si la hoja no está, los otros tres
        # siguen funcionando igual.
        anomalias = anomalias_layout(bochica, cargar_distribucion())

        # DEC-047: los detectores leen el estado actual de la DB y del
        # catálogo recién descargado, así que cada corrida refleja lo más
        # reciente del sistema administrativo.
        with sqlite3.connect(get_db_path(), timeout=30) as lectura:
            hallazgos = detectar_todos(catalogo_completo, lectura)
            # DEC-049: usa el catálogo ya filtrado por alcance, para que la
            # salud y el cruce hablen del mismo universo.
            salud = calcular_salud(admin, lectura)
            abc = calcular_clasificacion(admin, lectura)
            operacion = calcular_operacion(lectura)
            ventanas = calcular_ventanas(lectura)
            cancelaciones = calcular_cancelaciones(lectura, admin)

        # DEC-057: depende de abc y salud, por eso va tras el bloque de lectura.
        ubicaciones = calcular_ubicaciones(bochica, abc, admin, salud)
        # DEC-073: la otra mitad — lo que el admin declara y no se ve.
        sin_ubic = sin_ubicacion_conocida(admin, ubicaciones)
        # DEC-058: los conteos se comparan contra las líneas recién calculadas.
        # Carpeta ausente => None => la tabla no se toca. Carpeta vacía =>
        # DataFrame vacío => el espejo queda vacío, que es lo correcto.
        if RUTA_CONTEOS_DEFAULT.exists():
            crudo_conteos = cargar_conteos()
            conteos = evaluar_conteos(crudo_conteos, ubicaciones)
            archivos = inventariar_archivos(crudo_conteos)
        else:
            conteos = archivos = None

        corrida_id = persistir(
            comparacion,
            anomalias,
            frescura,
            fuera_layout=fuera_layout,
            catalogo=catalogo,
            hallazgos=hallazgos,
            salud=salud,
            abc=abc,
            operacion=operacion,
            ventanas=ventanas,
            ubicaciones=ubicaciones,
            # DEC-062: el mapa fecha el último conteo de cada posición,
            # así que necesita los conteos ya evaluados.
            posiciones=mapa_posiciones(ubicaciones, layout, conteos),
            sin_ubicacion=sin_ubic,
            conteos=conteos,
            ira=calcular_ira(conteos) if conteos is not None else None,
            ira_periodo=ira_por_periodo(conteos) if conteos is not None else None,
            conformidad=conformidad_por_tipo(conteos) if conteos is not None else None,
            cancelaciones=cancelaciones,
            alertas=generar_alertas(
                salud,
                comparacion,
                ubicaciones,
                conteos,
                abc,
                frescura,
                hallazgos,
                cancelaciones,
            ),
            archivos=archivos,
        )
    except FileNotFoundError as e:
        logger.error("inventario: falta una fuente — %s", e)
        return 1
    except Exception:
        logger.exception("inventario: la corrida falló sin persistir nada")
        return 1

    logger.info(
        "inventario: corrida %d persistida en pedidos.db (%d hallazgos de calidad)",
        corrida_id,
        len(hallazgos),
    )
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
