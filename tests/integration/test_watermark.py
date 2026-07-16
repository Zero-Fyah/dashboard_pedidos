"""Tests de persistencia del watermark en la tabla meta (AUD-M4, DEC-012).

actualizar_watermark() escribe meta.ultima_corrida_ok con
max(valor actual, fecha de cobertura): la marca nunca retrocede. La tabla
es de fila única (CHECK id = 1). El cálculo de la ventana a partir de la
marca se testea en tests/unit/test_watermark.py.
"""

import aiosqlite
import pytest

from scraper.scraper_principal import actualizar_watermark


@pytest.mark.integration
async def test_init_db_crea_tabla_meta(db_path):
    """La décima tabla del schema existe tras init_db (DEC-012)."""
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'")
        ).fetchone()
    assert row is not None


@pytest.mark.integration
async def test_primer_watermark_inserta_la_fila(db_path):
    assert await actualizar_watermark(db_path, "2026-07-10") == "2026-07-10"


@pytest.mark.integration
async def test_watermark_avanza_con_fecha_mayor(db_path):
    await actualizar_watermark(db_path, "2026-07-10")
    assert await actualizar_watermark(db_path, "2026-07-15") == "2026-07-15"


@pytest.mark.integration
async def test_watermark_nunca_retrocede(db_path):
    """Un run completo OK con --hasta antiguo no puede reabrir huecos ya
    cubiertos: la escritura es max(valor actual, cobertura)."""
    await actualizar_watermark(db_path, "2026-07-15")
    assert await actualizar_watermark(db_path, "2026-07-01") == "2026-07-15"


@pytest.mark.integration
async def test_meta_es_de_fila_unica(db_path):
    """El CHECK (id = 1) rechaza una segunda fila; los upserts repetidos
    dejan exactamente una."""
    await actualizar_watermark(db_path, "2026-07-10")
    await actualizar_watermark(db_path, "2026-07-15")
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("INSERT INTO meta (id, ultima_corrida_ok) VALUES (2, '2026-01-01')")
        n = (await (await db.execute("SELECT COUNT(*) FROM meta")).fetchone())[0]
    assert n == 1
