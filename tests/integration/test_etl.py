import pytest
import aiosqlite
from etl.etl_principal import normalizar_montos, crear_views


@pytest.mark.integration
async def test_columnas_num_creadas(db_path):
    """Las 23 columnas _num existen tras normalizar."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        cols_lp = {c[1] for c in await (await db.execute(
            "PRAGMA table_info(lineas_pedido)"
        )).fetchall()}
        cols_em = {c[1] for c in await (await db.execute(
            "PRAGMA table_info(estadisticas_monto)"
        )).fetchall()}
        cols_gd = {c[1] for c in await (await db.execute(
            "PRAGMA table_info(gestion_diferencias)"
        )).fetchall()}
        cols_dd = {c[1] for c in await (await db.execute(
            "PRAGMA table_info(detalle_diferencias)"
        )).fetchall()}

    for col in ["precio_unitario_num", "descuento_num",
                "precio_descuento_num", "monto_pagar_num",
                "monto_final_num", "iva_num", "peso_total_num"]:
        assert col in cols_lp, f"{col} falta en lineas_pedido"

    for col in ["monto_pagar_num", "monto_final_num",
                "diferencia_num"]:
        assert col in cols_em, f"{col} falta en estadisticas_monto"

    for col in ["total_pagar_pedido_num", "monto_final_pagar_num",
                "monto_pagado_num", "monto_diferencia_num"]:
        assert col in cols_gd, f"{col} falta en gestion_diferencias"

    for col in ["precio_unitario_num", "descuento_num",
                "precio_descuento_num", "cantidad_pedido_num",
                "cantidad_entregada_num", "diferencia_cantidad_num",
                "monto_pagar_pedido_num", "monto_final_pagar_num",
                "iva_num", "monto_diferencia_num"]:
        assert col in cols_dd, f"{col} falta en detalle_diferencias"


@pytest.mark.integration
async def test_normalizacion_es_idempotente(db_path):
    """Ejecutar normalizar_montos dos veces no genera errores."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        await normalizar_montos(db)


@pytest.mark.integration
async def test_views_creadas(db_path):
    """Las 7 VIEWs existen tras normalizar y crear_views.

    normalizar_montos debe ejecutarse primero porque
    v_diferencias_resumen referencia columnas _num de
    gestion_diferencias.
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)   # ← obligatorio primero
        await crear_views(db)
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
        views = {r[0] for r in await cursor.fetchall()}

    views_esperadas = {
        "v_pedidos_activos",
        "v_pedidos_cerrados",
        "v_inventario_comprometido",
        "v_diferencias_resumen",
        "v_rendimiento_operadores",
        "v_variaciones_timeline",
        "v_variaciones_operaciones",
    }
    assert views_esperadas.issubset(views), (
        f"VIEWs faltantes: {views_esperadas - views}"
    )


@pytest.mark.integration
async def test_views_son_idempotentes(db_path):
    """Ejecutar crear_views dos veces produce exactamente 7 VIEWs."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)   # ← obligatorio primero
        await crear_views(db)
        await crear_views(db)
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='view'"
        )).fetchone())[0]
    assert count == 7
