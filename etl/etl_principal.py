"""
ETL principal — dashboard_pedidos
Normaliza montos TEXT a REAL y crea VIEWs analíticas
sobre data/pedidos.db.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiosqlite

# AUD-M5 (auditoría 2026-07-01): importar del módulo común — el ETL ya no
# carga el scraper (ni Playwright, ni sus efectos secundarios de import).
from comun import (
    ESTADOS_ACTIVOS_INVENTARIO,
    ESTADOS_CERRADOS,
    ESTADOS_CONOCIDOS,
    get_db_path,
    to_num,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("etl")

# AUD-M8 (auditoría 2026-07-01): literales SQL generados desde las
# constantes del módulo común — único origen de verdad para
# "cerrado"/"activo" en las VIEWs. sorted() para SQL determinístico.
_CERRADOS_SQL = ",".join(f"'{e}'" for e in sorted(ESTADOS_CERRADOS))
_ACTIVOS_INVENTARIO_SQL = ",".join(f"'{e}'" for e in ESTADOS_ACTIVOS_INVENTARIO)


def _log_event(
    event: str,
    level: str = "INFO",
    msg: str = "",
    **kwargs,
) -> None:
    """Emite una línea JSONL a stderr con schema consistente con el scraper.

    Args:
        event: Categoría semántica del evento (snake_case).
        level: Nivel de log: INFO, WARNING o ERROR.
        msg: Descripción textual del evento.
        **kwargs: Campos adicionales a incluir en el JSON.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "msg": msg,
        "modulo": "etl",
        **kwargs,
    }
    line = json.dumps(record, ensure_ascii=False)
    if level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


async def normalizar_montos(db: aiosqlite.Connection) -> None:
    """Agrega columnas _num REAL y las puebla con to_num().

    Usa ALTER TABLE con try/except por columna para
    idempotencia. Puebla en batches de 500 filas
    procesando solo las filas donde _num IS NULL.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    columnas_por_tabla = {
        "lineas_pedido": [
            "precio_unitario_num",
            "descuento_num",
            "precio_descuento_num",
            "monto_pagar_num",
            "monto_final_num",
            "iva_num",
            "peso_total_num",
        ],
        "estadisticas_monto": [
            "monto_pagar_num",
            "monto_final_num",
            "diferencia_num",
        ],
        "gestion_diferencias": [
            "total_pagar_pedido_num",
            "monto_final_pagar_num",
            "monto_pagado_num",
            "monto_diferencia_num",
        ],
        "detalle_diferencias": [
            "precio_unitario_num",
            "descuento_num",
            "precio_descuento_num",
            "cantidad_pedido_num",
            "cantidad_entregada_num",
            "diferencia_cantidad_num",
            "monto_pagar_pedido_num",
            "monto_final_pagar_num",
            "iva_num",
            "monto_diferencia_num",
        ],
    }

    for tabla, columnas in columnas_por_tabla.items():
        # Paso 1: agregar columnas con ALTER TABLE
        for col_num in columnas:
            try:
                await db.execute(f"ALTER TABLE {tabla} ADD COLUMN {col_num} REAL")
                await db.commit()
            except Exception:
                # La columna ya existe — continuar
                pass

        # Paso 2: poblar en batches de 500 filas.
        # Filtra por col_num IS NULL para procesar únicamente
        # las filas que aún no tienen valor convertido.
        # Esto hace el ETL idempotente y captura correctamente
        # las filas nuevas que deja el scraper en cada ejecución.
        for col_num in columnas:
            col_src = col_num[:-4]  # quitar "_num"
            last_id = 0
            batch_count = 0
            total_filas = 0
            total_fallidos = 0
            while True:
                rows = await (
                    await db.execute(
                        f"SELECT id, {col_src} FROM {tabla} "
                        f"WHERE id > ? AND {col_num} IS NULL "
                        f"ORDER BY id LIMIT 500",
                        (last_id,),
                    )
                ).fetchall()
                if not rows:
                    _log_event(
                        "etl_columna_ok",
                        msg=(
                            f"{tabla}.{col_num} | "
                            f"{total_filas} filas convertidas | "
                            f"{total_fallidos} sin valor fuente"
                        ),
                    )
                    break
                for row_id, val in rows:
                    valor_num = to_num(val) if val is not None else None
                    if val is not None and valor_num is None:
                        total_fallidos += 1
                        _log_event(
                            "etl_conversion_fallida",
                            level="WARNING",
                            msg=(f"{tabla}.{col_src} id={row_id} valor_original={val!r}"),
                        )
                    await db.execute(
                        f"UPDATE {tabla} SET {col_num} = ? WHERE id = ?",
                        (valor_num, row_id),
                    )
                last_id = rows[-1][0]
                await db.commit()
                batch_count += 1
                total_filas += len(rows)
                if batch_count % 10 == 0:
                    _log_event(
                        "etl_batch",
                        msg=(
                            f"{tabla}.{col_num} | "
                            f"batch {batch_count} | "
                            f"{total_filas} filas acumuladas"
                        ),
                    )


async def crear_views(db: aiosqlite.Connection) -> None:
    """Crea o reemplaza las VIEWs analíticas y de montos limpios.

    Usa DROP VIEW IF EXISTS antes de cada CREATE VIEW
    para garantizar idempotencia.

    VIEWs analíticas (7): v_pedidos_activos, v_pedidos_cerrados,
    v_inventario_comprometido, v_diferencias_resumen,
    v_rendimiento_operadores, v_variaciones_timeline,
    v_variaciones_operaciones.

    VIEWs para el dashboard (4): v_lineas_pedido_num,
    v_estadisticas_monto_num, v_gestion_diferencias_num,
    v_detalle_diferencias_num. Exponen los valores monetarios
    ya convertidos a REAL con nombres limpios (sin sufijo _num)
    para que el dashboard los consuma directamente.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    views = {
        # AUD-M8: los literales de estados se generan desde comun/
        "v_pedidos_activos": f"""
            SELECT
                p.id_pedido,
                p.fecha,
                p.vendedor,
                p.forma_pago,
                p.destinatario,
                p.metodo_entrega,
                p.hay_diferencia,
                p.actualizado_en,
                COUNT(s.id) AS total_subpedidos,
                SUM(CASE WHEN LOWER(s.estado) NOT IN
                    ({_CERRADOS_SQL})
                    THEN 1 ELSE 0 END) AS subpedidos_abiertos
            FROM pedidos p
            JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 1
            GROUP BY p.id_pedido
            HAVING subpedidos_abiertos > 0
        """,
        "v_pedidos_cerrados": f"""
            SELECT
                p.id_pedido,
                p.fecha,
                p.vendedor,
                p.forma_pago,
                p.hay_diferencia,
                p.actualizado_en,
                COUNT(s.id) AS total_subpedidos
            FROM pedidos p
            JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 1
            GROUP BY p.id_pedido
            HAVING SUM(CASE WHEN LOWER(s.estado) NOT IN
                ({_CERRADOS_SQL})
                THEN 1 ELSE 0 END) = 0
        """,
        "v_inventario_comprometido": f"""
            SELECT
                l.nombre_producto,
                l.referencia,
                l.codigo_barras,
                l.presentacion,
                l.almacen,
                s.estado,
                SUM(l.cantidad_comprada)
                    AS cantidad_comprometida_total,
                SUM(l.cantidad_entregada)
                    AS cantidad_entregada_total,
                -- FIX C-4 (auditoría 2026-07-01): COALESCE evita que
                -- cantidad_entregada NULL anule la fila entera en el SUM
                -- (x - NULL = NULL y SUM ignora NULLs → subcuenta Pendiente)
                SUM(l.cantidad_comprada - COALESCE(l.cantidad_entregada, 0))
                    AS cantidad_pendiente,
                COUNT(DISTINCT l.id_pedido) AS pedidos_activos
            FROM lineas_pedido l
            JOIN subpedidos s
                ON l.id_pedido = s.id_pedido
                AND l.numero_subpedido = s.numero_subpedido
            WHERE LOWER(s.estado) IN ({_ACTIVOS_INVENTARIO_SQL})
            AND l.nombre_producto IS NOT NULL
            AND l.nombre_producto != ''
            GROUP BY
                l.nombre_producto,
                l.referencia,
                l.codigo_barras,
                l.almacen,
                s.estado
        """,
        "v_diferencias_resumen": """
            SELECT
                p.id_pedido,
                p.fecha,
                p.vendedor,
                p.forma_pago,
                g.total_pagar_pedido,
                g.monto_final_pagar,
                g.monto_diferencia,
                g.total_pagar_pedido_num,
                g.monto_final_pagar_num,
                g.monto_diferencia_num,
                COUNT(d.id) AS productos_con_diferencia
            FROM pedidos p
            JOIN gestion_diferencias g
                ON p.id_pedido = g.id_pedido
            LEFT JOIN detalle_diferencias d
                ON p.id_pedido = d.id_pedido
            WHERE p.hay_diferencia = 1
            GROUP BY p.id_pedido
        """,
        "v_rendimiento_operadores": """
            SELECT
                o.usuario,
                o.tipo_usuario,
                o.accion,
                COUNT(*) AS total_operaciones,
                DATE(o.momento) AS fecha,
                MIN(o.momento) AS primera_operacion,
                MAX(o.momento) AS ultima_operacion
            FROM registro_operaciones o
            WHERE o.tipo_usuario = 'staff'
            AND o.accion IN (
                'Alistamiento sin diferencia',
                'Alistamiento con faltantes',
                'Inspección sin diferencia',
                'Inspección con diferencia'
            )
            GROUP BY o.usuario, o.accion, DATE(o.momento)
            ORDER BY fecha DESC, o.usuario
        """,
        "v_variaciones_timeline": """
            SELECT
                titulo,
                COUNT(*) AS total_ocurrencias,
                COUNT(DISTINCT id_pedido) AS pedidos_afectados,
                MIN(fecha_hora) AS primera_vez,
                MAX(fecha_hora) AS ultima_vez
            FROM timeline_pedido
            WHERE titulo IS NOT NULL
            AND titulo != ''
            GROUP BY titulo
            ORDER BY total_ocurrencias DESC
        """,
        "v_variaciones_operaciones": """
            SELECT
                accion,
                tipo_usuario,
                COUNT(*) AS total_ocurrencias,
                COUNT(DISTINCT id_pedido) AS pedidos_afectados,
                MIN(momento) AS primera_vez,
                MAX(momento) AS ultima_vez
            FROM registro_operaciones
            WHERE accion IS NOT NULL
            AND accion != ''
            GROUP BY accion, tipo_usuario
            ORDER BY total_ocurrencias DESC
        """,
        # ── VIEWs para el dashboard ────────────────────────────────────
        # Exponen los valores monetarios ya convertidos a REAL con
        # nombres limpios (sin sufijo _num). El dashboard lee estas
        # views en lugar de las tablas base.
        # Ejemplo: precio_unitario = 1940.0 en lugar de "COP 1.940"
        "v_lineas_pedido_num": """
            SELECT
                l.id,
                l.id_pedido,
                l.numero_subpedido,
                l.tipo_subpedido,
                l.nombre_producto,
                l.referencia,
                l.codigo_barras,
                l.presentacion,
                l.almacen,
                l.cantidad_comprada,
                l.cantidad_entregada,
                l.precio_unitario_num   AS precio_unitario,
                l.descuento_num         AS descuento,
                l.precio_descuento_num  AS precio_descuento,
                l.monto_pagar_num       AS monto_pagar,
                l.monto_final_num       AS monto_final,
                l.iva_num               AS iva,
                l.peso_total_num        AS peso_total,
                l.observaciones,
                l.numero_caja,
                l.tipo
            FROM lineas_pedido l
        """,
        "v_estadisticas_monto_num": """
            SELECT
                e.id,
                e.id_pedido,
                e.orden,
                e.concepto,
                e.concepto_tag,
                e.monto_pagar_num   AS monto_pagar,
                e.monto_final_num   AS monto_final,
                e.diferencia_num    AS diferencia
            FROM estadisticas_monto e
        """,
        "v_gestion_diferencias_num": """
            SELECT
                g.id,
                g.id_pedido,
                g.total_pagar_pedido_num  AS total_pagar_pedido,
                g.monto_final_pagar_num   AS monto_final_pagar,
                g.monto_pagado_num        AS monto_pagado,
                g.monto_diferencia_num    AS monto_diferencia
            FROM gestion_diferencias g
        """,
        "v_detalle_diferencias_num": """
            SELECT
                d.id,
                d.id_pedido,
                d.nombre_producto,
                d.especificacion,
                d.tipo,
                d.precio_unitario_num        AS precio_unitario,
                d.descuento_num              AS descuento,
                d.precio_descuento_num       AS precio_descuento,
                d.cantidad_pedido_num        AS cantidad_pedido,
                d.cantidad_entregada_num     AS cantidad_entregada,
                d.diferencia_cantidad_num    AS diferencia_cantidad,
                d.monto_pagar_pedido_num     AS monto_pagar_pedido,
                d.monto_final_pagar_num      AS monto_final_pagar,
                d.iva_num                    AS iva,
                d.monto_diferencia_num       AS monto_diferencia
            FROM detalle_diferencias d
        """,
    }

    for nombre, sql in views.items():
        await db.execute(f"DROP VIEW IF EXISTS {nombre}")
        await db.execute(f"CREATE VIEW {nombre} AS {sql}")
    await db.commit()


async def main() -> int:
    """Punto de entrada del ETL.

    Abre la conexión a data/pedidos.db, ejecuta la
    normalización de montos y la creación de VIEWs,
    y cierra la conexión.

    Returns:
        0: ETL completado sin errores.
        1: Error no recuperable durante la ejecución.
        (AUD-B7: el sys.exit vive en __main__, no en la corrutina.)
    """
    db_path = get_db_path()
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            await normalizar_montos(db)
            await crear_views(db)
            row = await (
                await db.execute("SELECT COUNT(*) FROM v_rendimiento_operadores")
            ).fetchone()
            if row and row[0] == 0:
                _log_event(
                    "etl_view_vacia",
                    level="WARNING",
                    msg="v_rendimiento_operadores retorna 0 filas — verificar acciones hardcodeadas en WHERE",
                )

            # AUD-M8 (auditoría 2026-07-01): check defensivo — un estado en
            # DB fuera de las listas del módulo común indica que el sistema
            # origen agregó o renombró estados; las VIEWs podrían estar
            # excluyéndolo en silencio.
            rows_est = await (
                await db.execute(
                    "SELECT DISTINCT LOWER(estado) FROM subpedidos "
                    "WHERE estado IS NOT NULL AND estado != ''"
                )
            ).fetchall()
            desconocidos = sorted(r[0] for r in rows_est if r[0] not in ESTADOS_CONOCIDOS)
            if desconocidos:
                _log_event(
                    "etl_estado_desconocido",
                    level="WARNING",
                    msg=(
                        "Estados de subpedidos fuera de las listas conocidas "
                        f"del módulo común: {desconocidos}"
                    ),
                )
        _log_event("etl_completado", db_path=db_path)
        return 0
    except Exception as exc:
        _log_event(
            "etl_fallido",
            level="ERROR",
            error=str(exc),
            error_type=type(exc).__name__,
            db_path=db_path,
        )
        return 1


if __name__ == "__main__":
    # AUD-B7: main() retorna el exit code; sys.exit() solo vive aquí.
    sys.exit(asyncio.run(main()))
