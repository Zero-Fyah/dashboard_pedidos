"""Carril de re-extracción del histórico por tandas — DEC-140.

Selecciona pedidos con líneas sin `peso_entregado` o con `observaciones`
vaciada por la reparación. Se apaga solo: cuando el pedido se re-extrae, sus
líneas quedan con los dos campos y deja de salir.
"""

import aiosqlite
import pytest

from scraper.orquestador import construir_resumen, obtener_ids_reextraccion


async def _crear(db_path: str, lineas: list[tuple[str, str | None, str | None]]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY)")
        await db.execute(
            "CREATE TABLE lineas_pedido (id_pedido TEXT, peso_entregado TEXT, observaciones TEXT)"
        )
        for pid in {pid for pid, _, _ in lineas} | {"SIN_LINEAS"}:
            await db.execute("INSERT INTO pedidos VALUES (?)", (pid,))
        await db.executemany("INSERT INTO lineas_pedido VALUES (?, ?, ?)", lineas)
        await db.commit()


@pytest.mark.integration
async def test_selecciona_solo_lo_que_falta(tmp_path):
    db = str(tmp_path / "r.db")
    await _crear(
        db,
        [
            ("VIEJO", None, "-"),  # capturado antes de la columna
            ("REPARADO", "500g", None),  # la reparación vació observaciones
            ("MIXTO", "500g", "-"),
            ("MIXTO", None, "-"),  # basta una línea incompleta
            ("COMPLETO", "500g", "-"),
            ("VACIO_OK", "", "-"),  # origen sin la columna: '' no es NULL
        ],
    )
    assert sorted(await obtener_ids_reextraccion(db, 100)) == ["MIXTO", "REPARADO", "VIEJO"]


@pytest.mark.integration
async def test_respeta_el_tamano_de_la_tanda(tmp_path):
    db = str(tmp_path / "r.db")
    await _crear(db, [(f"P{i}", None, "-") for i in range(20)])
    assert len(await obtener_ids_reextraccion(db, 7)) == 7


@pytest.mark.integration
async def test_tanda_cero_apaga_el_carril(tmp_path):
    db = str(tmp_path / "r.db")
    await _crear(db, [("VIEJO", None, "-")])
    assert await obtener_ids_reextraccion(db, 0) == []


@pytest.mark.integration
def test_resumen_reporta_el_carril_y_tolera_carriles_viejos():
    stats = {"ok": set(), "error": set()}
    nuevo = construir_resumen(
        0, stats, 1.0, "incremental", {"activos": 1, "errores": 0, "nuevos": 2, "reextraccion": 5}
    )
    viejo = construir_resumen(
        0, stats, 1.0, "incremental", {"activos": 1, "errores": 0, "nuevos": 2}
    )
    assert nuevo["pedidos_reextraccion"] == 5
    assert viejo["pedidos_reextraccion"] is None
