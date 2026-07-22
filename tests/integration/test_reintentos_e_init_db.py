"""Tests de Fase 6 con SQLite temporal.

AUD-B4: procesar_pedido() respeta max_reintentos como parámetro y no muta
CONFIG (una page fake que siempre falla cuenta los intentos reales).
AUD-B8b: init_db() es re-ejecutable — los ALTER de columnas duplicadas se
omiten con el catch específico, sin enmascarar otros errores.
AUD-B9: init_db() depura duplicados legados de gestion_diferencias y deja
el índice UNIQUE activo.
"""

import asyncio

import aiosqlite
import pytest

from scraper.config import CONFIG
from scraper.db import init_db
from scraper.workers import procesar_pedido


class _PageRota:
    """Page fake cuya navegación siempre falla (SPA caída simulada)."""

    url = ""

    async def goto(self, *args, **kwargs):
        raise RuntimeError("SPA caída simulada")

    async def screenshot(self, path):
        raise RuntimeError("sin screenshot en el fake")

    async def content(self):
        raise RuntimeError("sin html en el fake")


@pytest.fixture(autouse=True)
def _dirs_debug_en_tmp(monkeypatch, tmp_path):
    """guardar_debug() corre en cada fallo — apuntarlo a tmp para que los
    tests jamás toquen data/errors y data/debug reales."""
    monkeypatch.setitem(CONFIG, "ERRORS_DIR", str(tmp_path / "errors"))
    monkeypatch.setitem(CONFIG, "DEBUG_DIR", str(tmp_path / "debug"))


@pytest.fixture(autouse=True)
def _sin_backoff(monkeypatch):
    """Evita dormir el backoff real entre reintentos."""

    async def _instantaneo(_segundos):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instantaneo)


# ── AUD-B4: max_reintentos parametrizado ────────────────────────────────────


@pytest.mark.integration
async def test_max_reintentos_explicito_limita_intentos(db_path):
    queue: asyncio.Queue = asyncio.Queue()
    exito = await procesar_pedido(0, _PageRota(), "TEST-B4", queue, db_path, max_reintentos=2)
    assert exito is False
    resultado = queue.get_nowait()
    assert resultado["_error"] is True
    assert "tras 2 intentos" in resultado["detalle"]


@pytest.mark.integration
async def test_max_reintentos_default_usa_config_sin_mutarla(db_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "MAX_REINTENTOS", 3)
    queue: asyncio.Queue = asyncio.Queue()
    exito = await procesar_pedido(0, _PageRota(), "TEST-B4-DEF", queue, db_path)
    assert exito is False
    assert "tras 3 intentos" in queue.get_nowait()["detalle"]


@pytest.mark.integration
async def test_config_no_se_muta_con_parametro(db_path):
    """AUD-B4: pasar un tope explícito no toca el valor global."""
    original = CONFIG["MAX_REINTENTOS"]
    queue: asyncio.Queue = asyncio.Queue()
    await procesar_pedido(0, _PageRota(), "TEST-B4-NOMUT", queue, db_path, max_reintentos=1)
    assert CONFIG["MAX_REINTENTOS"] == original


# ── AUD-M9: excepción en la determinación de modo no mata el worker ────────


@pytest.mark.integration
async def test_error_de_db_en_determinar_modo_no_revienta_devuelve_error(tmp_path):
    """AUD-M9: antes de este fix, un OperationalError en la consulta previa
    al loop de reintentos (DB sin tablas — sesión no inicializada, DB
    bloqueada, etc.) se escapaba de procesar_pedido() sin control. Ahora
    respeta el mismo contrato de salida que el resto de la función: False +
    resultado "_error" en la cola, nunca una excepción sin capturar."""
    db_sin_init = str(tmp_path / "sin_tablas.db")  # aiosqlite la crea vacía,
    # sin correr init_db() — las SELECT de determinar_modo fallan con
    # "no such table: pedidos" (OperationalError real, no simulado).
    queue: asyncio.Queue = asyncio.Queue()

    exito = await procesar_pedido(0, _PageRota(), "TEST-M9", queue, db_sin_init)

    assert exito is False
    resultado = queue.get_nowait()
    assert resultado["_error"] is True
    assert "Determinación de modo falló" in resultado["detalle"]


# ── AUD-B8b: init_db re-ejecutable con catch específico ─────────────────────


@pytest.mark.integration
async def test_init_db_es_reejecutable(db_path):
    """Segunda corrida sobre la misma DB: los ALTER de columnas ya
    aplicadas se omiten sin explotar y sin enmascarar otros errores."""
    await init_db(db_path)  # el fixture ya corrió la primera


# ── AUD-B9: dedupe + índice UNIQUE en gestion_diferencias ───────────────────


@pytest.mark.integration
async def test_gestion_diferencias_dedupe_y_unique(db_path):
    async with aiosqlite.connect(db_path) as db:
        # Estado legado: sin índice UNIQUE y con filas duplicadas.
        await db.execute("DROP INDEX IF EXISTS idx_gestion_dif_unico")
        await db.executemany(
            "INSERT INTO gestion_diferencias "
            "(id_pedido, total_pagar_pedido, monto_final_pagar, "
            " monto_pagado, monto_diferencia) VALUES (?, ?, ?, ?, ?)",
            [
                ("TEST-B9", "100", "100", "100", "0"),  # vieja
                ("TEST-B9", "200", "200", "200", "0"),  # más reciente
            ],
        )
        await db.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                "SELECT total_pagar_pedido FROM gestion_diferencias WHERE id_pedido = 'TEST-B9'"
            )
        ).fetchall()
        indices = await (await db.execute("PRAGMA index_list('gestion_diferencias')")).fetchall()

    # Queda una sola fila y es la más reciente (MAX(id)).
    assert len(rows) == 1
    assert rows[0][0] == "200"
    # El índice UNIQUE existe (columna 'unique' == 1 en index_list).
    unicos = [idx for idx in indices if idx[1] == "idx_gestion_dif_unico"]
    assert len(unicos) == 1
    assert unicos[0][2] == 1


@pytest.mark.integration
async def test_gestion_diferencias_rechaza_duplicado_nuevo(db_path):
    """Con el índice activo, un INSERT duplicado explota en vez de
    multiplicar v_diferencias_resumen en silencio."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO gestion_diferencias (id_pedido) VALUES ('TEST-B9-DUP')")
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("INSERT INTO gestion_diferencias (id_pedido) VALUES ('TEST-B9-DUP')")
