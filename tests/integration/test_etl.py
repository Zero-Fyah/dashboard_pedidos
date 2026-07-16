import aiosqlite
import pytest

from etl.etl_principal import crear_views, normalizar_montos


@pytest.mark.integration
async def test_columnas_num_creadas(db_path):
    """Las 23 columnas _num existen tras normalizar."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        cols_lp = {
            c[1] for c in await (await db.execute("PRAGMA table_info(lineas_pedido)")).fetchall()
        }
        cols_em = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(estadisticas_monto)")).fetchall()
        }
        cols_gd = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(gestion_diferencias)")).fetchall()
        }
        cols_dd = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(detalle_diferencias)")).fetchall()
        }

    for col in [
        "precio_unitario_num",
        "descuento_num",
        "precio_descuento_num",
        "monto_pagar_num",
        "monto_final_num",
        "iva_num",
        "peso_total_num",
    ]:
        assert col in cols_lp, f"{col} falta en lineas_pedido"

    for col in ["monto_pagar_num", "monto_final_num", "diferencia_num"]:
        assert col in cols_em, f"{col} falta en estadisticas_monto"

    for col in [
        "total_pagar_pedido_num",
        "monto_final_pagar_num",
        "monto_pagado_num",
        "monto_diferencia_num",
    ]:
        assert col in cols_gd, f"{col} falta en gestion_diferencias"

    for col in [
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
    ]:
        assert col in cols_dd, f"{col} falta en detalle_diferencias"


@pytest.mark.integration
async def test_normalizacion_es_idempotente(db_path):
    """Ejecutar normalizar_montos dos veces no genera errores."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        await normalizar_montos(db)


@pytest.mark.integration
async def test_views_creadas(db_path):
    """Las 11 VIEWs existen tras normalizar y crear_views.

    normalizar_montos debe ejecutarse primero porque
    v_diferencias_resumen referencia columnas _num de
    gestion_diferencias.
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='view'")
        views = {r[0] for r in await cursor.fetchall()}

    views_esperadas = {
        # 7 VIEWs analíticas
        "v_pedidos_activos",
        "v_pedidos_cerrados",
        "v_inventario_comprometido",
        "v_diferencias_resumen",
        "v_rendimiento_operadores",
        "v_variaciones_timeline",
        "v_variaciones_operaciones",
        # 4 VIEWs para el dashboard (montos _num con nombres limpios)
        "v_lineas_pedido_num",
        "v_estadisticas_monto_num",
        "v_gestion_diferencias_num",
        "v_detalle_diferencias_num",
    }
    assert views_esperadas.issubset(views), f"VIEWs faltantes: {views_esperadas - views}"


@pytest.mark.integration
async def test_views_son_idempotentes(db_path):
    """Ejecutar crear_views dos veces produce exactamente 11 VIEWs.

    11 = 7 analíticas + 4 del dashboard. Actualizado 2026-07-02: el test
    esperaba 7 desde antes de que se agregaran las views del dashboard.
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)
        await crear_views(db)
        count = (
            await (
                await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'")
            ).fetchone()
        )[0]
    assert count == 11


# ── FIX C-4 (auditoría 2026-07-01) — COALESCE en cantidad_pendiente ────────────


@pytest.mark.integration
async def test_inventario_pendiente_incluye_entregada_null(db_path):
    """FIX C-4: una línea con cantidad_entregada NULL (aún sin entrega
    registrada) aporta su cantidad_comprada completa a cantidad_pendiente.

    Antes del fix, x - NULL = NULL y SUM ignoraba la fila entera:
    el KPI "Pendiente" subcontaba exactamente las líneas pendientes.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-C4', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO subpedidos "
            "(id_pedido, numero_subpedido, tipo_subpedido, estado) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Normal', 'Pendiente de entrega')"
        )
        # Línea 1: sin entrega registrada (NULL) — debe aportar 10 pendientes
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, referencia, "
            "codigo_barras, presentacion, almacen, "
            "cantidad_comprada, cantidad_entregada) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Producto C4', 'REF-C4', "
            "'7700000000C4', 'Unidad', 'Almacén Test', 10.0, NULL)"
        )
        # Línea 2: entrega parcial — debe aportar 6 pendientes
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, referencia, "
            "codigo_barras, presentacion, almacen, "
            "cantidad_comprada, cantidad_entregada) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Producto C4', 'REF-C4', "
            "'7700000000C4', 'Unidad', 'Almacén Test', 10.0, 4.0)"
        )
        await db.commit()

        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)

        row = await (
            await db.execute(
                "SELECT cantidad_comprometida_total, cantidad_entregada_total, "
                "cantidad_pendiente "
                "FROM v_inventario_comprometido WHERE referencia = 'REF-C4'"
            )
        ).fetchone()

    assert row is not None, "la línea no entró a v_inventario_comprometido"
    assert row[0] == 20.0  # 10 + 10 compradas
    assert row[1] == 4.0  # solo la entrega registrada
    assert row[2] == 16.0  # 10 (NULL→0) + 6 — antes del fix daba 6
