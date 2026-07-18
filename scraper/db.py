"""
db.py — Esquema SQLite, migraciones forward-compatible y utilidades de DB.

init_db() crea las 10 tablas si no existen; las columnas nuevas se agregan
con ALTER TABLE tolerante para bases existentes (DEC-013).
"""

import sqlite3

import aiosqlite

from scraper.config import log_event

# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────


async def init_db(db_path: str) -> None:
    """Crea las tablas si no existen, aplica PRAGMAs y ejecuta migraciones.

    Las tablas base se crean con CREATE TABLE IF NOT EXISTS. Las columnas
    nuevas se agregan via ALTER TABLE con try/except individual por columna
    para compatibilidad con bases de datos existentes.

    Args:
        db_path: Ruta al archivo SQLite (se crea si no existe).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA foreign_keys = ON")
        await db.commit()

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id_pedido           TEXT PRIMARY KEY,
                fecha               TEXT,
                servicio_cliente    TEXT,
                vendedor            TEXT,
                forma_pago          TEXT,
                comprobante         TEXT,
                nombre_empresa      TEXT,
                nit                 TEXT,
                metodo_entrega      TEXT,
                destinatario        TEXT,
                telefono            TEXT,
                direccion_envio     TEXT,
                observaciones       TEXT,
                scraping_completo   INTEGER DEFAULT 0,
                actualizado_en      TEXT
            );

            CREATE TABLE IF NOT EXISTS subpedidos (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido               TEXT,
                numero_subpedido        TEXT,
                tipo_subpedido          TEXT,
                estado                  TEXT,
                inicio_alistamiento     TEXT,
                alistamiento_completado TEXT,
                alistador               TEXT,
                inicio_inspeccion       TEXT,
                inspeccion_completada   TEXT,
                inspector               TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS lineas_pedido (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido           TEXT,
                numero_subpedido    TEXT,
                tipo_subpedido      TEXT,
                nombre_producto     TEXT,
                referencia          TEXT,
                codigo_barras       TEXT,
                presentacion        TEXT,
                almacen             TEXT,
                cantidad_comprada   REAL,
                cantidad_entregada  REAL,
                precio_unitario     TEXT,
                descuento           TEXT,
                precio_descuento    TEXT,
                monto_pagar         TEXT,
                monto_final         TEXT,
                iva                 TEXT,
                peso_total          TEXT,
                observaciones       TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS errores (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido TEXT,
                momento   TEXT,
                detalle   TEXT
            );

            CREATE TABLE IF NOT EXISTS timeline_pedido (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido        TEXT,
                numero_subpedido TEXT,
                paso             INTEGER,
                titulo           TEXT,
                fecha_hora       TEXT,
                completado       INTEGER DEFAULT 0,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS estadisticas_monto (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido    TEXT NOT NULL,
                orden        INTEGER,
                concepto     TEXT,
                concepto_tag TEXT,
                monto_pagar  TEXT,
                monto_final  TEXT,
                diferencia   TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS gestion_diferencias (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido          TEXT NOT NULL,
                total_pagar_pedido TEXT,
                monto_final_pagar  TEXT,
                monto_pagado       TEXT,
                monto_diferencia   TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS detalle_diferencias (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido           TEXT NOT NULL,
                nombre_producto     TEXT,
                especificacion      TEXT,
                tipo                TEXT,
                precio_unitario     TEXT,
                descuento           TEXT,
                precio_descuento    TEXT,
                cantidad_pedido     TEXT,
                cantidad_entregada  TEXT,
                diferencia_cantidad TEXT,
                monto_pagar_pedido  TEXT,
                monto_final_pagar   TEXT,
                iva                 TEXT,
                monto_diferencia    TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS registro_operaciones (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido    TEXT NOT NULL,
                momento      TEXT,
                usuario      TEXT,
                tipo_usuario TEXT,
                accion       TEXT,
                referencia   TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS meta (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                ultima_corrida_ok TEXT
            );
        """)

        for ddl in (
            "ALTER TABLE pedidos ADD COLUMN hora                  TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN alistador_pedido      TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN inspector_pedido      TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN movil_cliente         TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN despachador           TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN hora_entrega          TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN obs_entrega           TEXT    DEFAULT NULL",
            # DEC-023: la SPA solo renderiza estos dos campos con
            # metodo_entrega='Ruta'; antes se guardaban por error en
            # hora_entrega y obs_entrega (BUG-019).
            "ALTER TABLE pedidos ADD COLUMN conductor             TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN vehiculo_entrega      TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN entrega_ruta_tag      TEXT    DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN entrega_descuento_tag TEXT    DEFAULT NULL",
            # DEC-024: el tipo de descuento (etiquetas de la celda) se separa
            # del monto; antes se descartaba en lineas_pedido y ocupaba la
            # columna descuento en detalle_diferencias.
            "ALTER TABLE lineas_pedido ADD COLUMN descuento_tipo TEXT DEFAULT NULL",
            "ALTER TABLE detalle_diferencias ADD COLUMN descuento_tipo TEXT DEFAULT NULL",
            "ALTER TABLE pedidos ADD COLUMN hay_diferencia        INTEGER DEFAULT 0",
            "ALTER TABLE subpedidos ADD COLUMN cantidades_definitivas INTEGER DEFAULT 0",
            "ALTER TABLE subpedidos ADD COLUMN estado_cambiado_en TEXT DEFAULT NULL",
            "ALTER TABLE lineas_pedido ADD COLUMN numero_caja     TEXT    DEFAULT NULL",
            "ALTER TABLE lineas_pedido ADD COLUMN tipo            TEXT    DEFAULT NULL",
        ):
            try:
                await db.execute(ddl)
                await db.commit()
            except sqlite3.OperationalError as exc:
                # AUD-B8: el único error esperado aquí es que la columna ya
                # exista (migración ya aplicada). Cualquier otro
                # OperationalError (DB corrupta, disco lleno, tabla ausente)
                # debe propagarse, no enmascararse.
                if "duplicate column name" not in str(exc).lower():
                    raise

        for ddl_idx in (
            "CREATE INDEX IF NOT EXISTS idx_subpedidos_pedido   ON subpedidos(id_pedido)",
            "CREATE INDEX IF NOT EXISTS idx_lineas_pedido       ON lineas_pedido(id_pedido, numero_subpedido)",
            "CREATE INDEX IF NOT EXISTS idx_errores_pedido      ON errores(id_pedido)",
            "CREATE INDEX IF NOT EXISTS idx_estadisticas_pedido ON estadisticas_monto(id_pedido)",
            "CREATE INDEX IF NOT EXISTS idx_detalle_dif_pedido  ON detalle_diferencias(id_pedido)",
            "CREATE INDEX IF NOT EXISTS idx_registro_ops_pedido ON registro_operaciones(id_pedido)",
            "CREATE INDEX IF NOT EXISTS idx_pedidos_scraping    ON pedidos(scraping_completo)",
        ):
            await db.execute(ddl_idx)
        await db.commit()

        # AUD-B9: UNIQUE en gestion_diferencias.id_pedido — la tabla es
        # 1:1 con pedidos por diseño (DELETE + INSERT único por pedido en
        # persistencia), pero sin la restricción un bug futuro duplicaría
        # productos_con_diferencia en v_diferencias_resumen en silencio.
        # Antes de crear el índice se depura cualquier duplicado legado
        # conservando la fila más reciente (MAX(id)); el índice no-único
        # anterior (idx_gestion_dif_pedido) queda redundante y se elimina.
        cursor = await db.execute(
            """
            DELETE FROM gestion_diferencias
            WHERE id NOT IN (
                SELECT MAX(id) FROM gestion_diferencias GROUP BY id_pedido
            )
            """
        )
        if cursor.rowcount:
            log_event(
                "gestion_dif_deduplicada",
                level="WARNING",
                msg=(
                    f"AUD-B9: {cursor.rowcount} filas duplicadas eliminadas "
                    "de gestion_diferencias (se conservó la más reciente)"
                ),
            )
        await db.execute("DROP INDEX IF EXISTS idx_gestion_dif_pedido")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gestion_dif_unico "
            "ON gestion_diferencias(id_pedido)"
        )
        await db.commit()

    log_event("db_init", msg=f"Base de datos lista: {db_path}")


async def limpiar_errores_resueltos(db_path: str) -> int:
    """Elimina registros de errores para pedidos que ya están scraping_completo=1.

    Returns:
        Número de filas eliminadas.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")  # N-3 (DEC-014)
        cursor = await db.execute("""
            DELETE FROM errores
            WHERE id_pedido IN (
                SELECT id_pedido FROM pedidos WHERE scraping_completo = 1
            )
        """)
        await db.commit()
        return cursor.rowcount


async def actualizar_watermark(db_path: str, fecha_cobertura: str) -> str:
    """Avanza meta.ultima_corrida_ok tras una corrida OK (AUD-M4, DEC-012).

    Escribe max(valor actual, fecha_cobertura): la marca nunca retrocede,
    ni siquiera si un run completo con --hasta antiguo termina OK.

    Args:
        db_path: Ruta al archivo SQLite.
        fecha_cobertura: Fecha YYYY-MM-DD hasta la cual este run cubrió la
                         captura de pedidos nuevos.

    Returns:
        Valor final almacenado en meta.ultima_corrida_ok.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")  # N-3 (DEC-014)
        await db.execute(
            """
            INSERT INTO meta (id, ultima_corrida_ok) VALUES (1, :fecha)
            ON CONFLICT(id) DO UPDATE SET
                ultima_corrida_ok = max(
                    COALESCE(meta.ultima_corrida_ok, ''),
                    excluded.ultima_corrida_ok
                )
            """,
            {"fecha": fecha_cobertura},
        )
        await db.commit()
        row = await (await db.execute("SELECT ultima_corrida_ok FROM meta WHERE id = 1")).fetchone()
        return row[0]
