"""
ETL principal — dashboard_pedidos
Normaliza montos TEXT a REAL y crea VIEWs analíticas
sobre data/pedidos.db.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import logging
import aiosqlite
from scraper.scraper_principal import to_num, get_db_path

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", '
           '"event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("etl")


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
                await db.execute(
                    f"ALTER TABLE {tabla} ADD COLUMN {col_num} REAL"
                )
                await db.commit()
            except Exception:
                # La columna ya existe — continuar
                pass

        # Paso 2: poblar en batches de 500 filas
        # El nombre de la columna fuente se obtiene
        # quitando el sufijo _num
        for col_num in columnas:
            col_src = col_num[:-4]  # quitar "_num"
            last_id = 0
            batch_count = 0
            total_filas = 0
            while True:
                rows = await (await db.execute(
                    f"SELECT id, {col_src} FROM {tabla} "
                    f"WHERE id > ? ORDER BY id LIMIT 500",
                    (last_id,)
                )).fetchall()
                if not rows:
                    logger.info(
                        f"etl_columna_ok | {tabla}.{col_num} | "
                        f"{total_filas} filas totales"
                    )
                    break
                for row_id, val in rows:
                    await db.execute(
                        f"UPDATE {tabla} SET {col_num} = ? "
                        f"WHERE id = ?",
                        (to_num(val) if val is not None else None,
                         row_id),
                    )
                last_id = rows[-1][0]
                await db.commit()
                batch_count += 1
                total_filas += len(rows)
                if batch_count % 10 == 0:
                    logger.info(
                        f"etl_batch | {tabla}.{col_num} | "
                        f"batch {batch_count} | {total_filas} filas acumuladas"
                    )


async def crear_views(db: aiosqlite.Connection) -> None:
    """Crea o reemplaza las 7 VIEWs analíticas.

    Usa DROP VIEW IF EXISTS antes de cada CREATE VIEW
    para garantizar idempotencia.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    views = {
        "v_pedidos_activos": """
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
                    ('completado','cancelado','comentado')
                    THEN 1 ELSE 0 END) AS subpedidos_abiertos
            FROM pedidos p
            JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 1
            GROUP BY p.id_pedido
            HAVING subpedidos_abiertos > 0
        """,
        "v_pedidos_cerrados": """
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
                ('completado','cancelado','comentado')
                THEN 1 ELSE 0 END) = 0
        """,
        "v_inventario_comprometido": """
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
                SUM(l.cantidad_comprada - l.cantidad_entregada)
                    AS cantidad_pendiente,
                COUNT(DISTINCT l.id_pedido) AS pedidos_activos
            FROM lineas_pedido l
            JOIN subpedidos s
                ON l.id_pedido = s.id_pedido
                AND l.numero_subpedido = s.numero_subpedido
            WHERE s.estado IN (
                'Pendiente de confirmación',
                'Pendiente de pago (pago inmediato)',
                'Pendiente de pago (crédito)',
                'Pendiente de pago (contra entrega)',
                'Pendiente de recolección',
                'Aprobación de Pagos',
                'Pendiente de envío (pago inmediato)',
                'Pendiente de envío (crédito)',
                'Pendiente de envío (contra entrega)',
                'Pendiente de entrega',
                'En inspección'
            )
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
    }

    for nombre, sql in views.items():
        await db.execute(f"DROP VIEW IF EXISTS {nombre}")
        await db.execute(f"CREATE VIEW {nombre} AS {sql}")
    await db.commit()


async def main() -> None:
    """Punto de entrada del ETL.

    Abre la conexión a data/pedidos.db, ejecuta la
    normalización de montos y la creación de VIEWs,
    y cierra la conexión.
    """
    db_path = get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await normalizar_montos(db)
        await crear_views(db)
        await db.commit()
    logger.info("etl_completado")


if __name__ == "__main__":
    asyncio.run(main())
