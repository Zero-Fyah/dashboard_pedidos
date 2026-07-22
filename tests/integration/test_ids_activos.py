"""BUG-024: obtener_ids_activos() debe seguir viendo un pedido que ya cerró
todos sus subpedidos pero aún no tiene cantidades_definitivas — antes de
este fix, ese pedido desaparecía del carril "activos" para siempre.
"""

import aiosqlite
import pytest

from scraper.orquestador import obtener_ids_activos


async def _insertar_pedido(db_path, id_pedido, subpedidos):
    """subpedidos: lista de (estado, cantidades_definitivas)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, scraping_completo) VALUES (?, 1)",
            (id_pedido,),
        )
        for i, (estado, cd) in enumerate(subpedidos):
            await db.execute(
                """INSERT INTO subpedidos
                   (id_pedido, numero_subpedido, estado, cantidades_definitivas)
                   VALUES (?, ?, ?, ?)""",
                (id_pedido, f"{id_pedido}-{i}", estado, cd),
            )
        await db.commit()


@pytest.mark.integration
async def test_pedido_recien_cerrado_sin_cantidades_definitivas_sigue_activo(db_path):
    """El caso del bug: todos los subpedidos cerraron en el mismo
    incremento, ninguno queda abierto, pero cantidades_definitivas=0."""
    await _insertar_pedido(db_path, "CERRADO-SIN-CANT", [("completado", 0)])
    ids = await obtener_ids_activos(db_path)
    assert "CERRADO-SIN-CANT" in ids


@pytest.mark.integration
async def test_pedido_totalmente_resuelto_no_aparece(db_path):
    """Cerrado y con cantidades_definitivas=1: ya no necesita atención."""
    await _insertar_pedido(db_path, "RESUELTO", [("completado", 1)])
    ids = await obtener_ids_activos(db_path)
    assert "RESUELTO" not in ids


@pytest.mark.integration
async def test_pedido_con_subpedido_abierto_sigue_activo(db_path):
    """Caso normal preexistente: sigue funcionando igual que antes."""
    await _insertar_pedido(db_path, "ABIERTO", [("en alistamiento", 0)])
    ids = await obtener_ids_activos(db_path)
    assert "ABIERTO" in ids


@pytest.mark.integration
async def test_pedido_mixto_uno_cerrado_sin_cantidades_otro_abierto(db_path):
    """Un subpedido cerrado sin cantidades + otro todavía abierto — ya
    funcionaba antes del fix (tenía un subpedido abierto), sigue activo."""
    await _insertar_pedido(db_path, "MIXTO", [("completado", 0), ("en inspección", 0)])
    ids = await obtener_ids_activos(db_path)
    assert "MIXTO" in ids
