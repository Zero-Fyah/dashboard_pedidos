"""Tests del circuit breaker de scraper_worker() (Fase 7 — cobertura dirigida).

El worker se testea con procesar_pedido() monkeypatcheado en
scraper.workers (fallos controlados, sin browser ni DB) y un
BrowserContext fake. asyncio.sleep se intercepta para registrar los
cooldowns sin dormir de verdad.
"""

import asyncio

import pytest

import scraper.workers as sw
from scraper.config import _RATE_LIMIT, CONFIG, registrar_rate_limit


class _FakePage:
    def on(self, evento, handler):
        pass

    async def close(self):
        pass


class _FakeContext:
    async def new_page(self):
        return _FakePage()


@pytest.fixture(autouse=True)
def _rate_limit_limpio():
    _RATE_LIMIT["hasta"] = 0.0
    yield
    _RATE_LIMIT["hasta"] = 0.0


@pytest.fixture
def circuito_corto(monkeypatch):
    """Umbrales pequeños para que el circuito abra rápido en tests."""
    monkeypatch.setitem(CONFIG, "CIRCUIT_FAILURE_THRESHOLD", 2)
    monkeypatch.setitem(CONFIG, "CIRCUIT_COOLDOWN_S", 99)
    monkeypatch.setitem(CONFIG, "CIRCUIT_MAX_REOPENINGS", 1)


@pytest.fixture
def sleeps(monkeypatch):
    """Registra las pausas del worker sin dormir."""
    registro: list[float] = []

    async def _fake_sleep(segundos):
        registro.append(segundos)
        # Si la pausa es por rate limit, limpiar la señal para no
        # quedar en el while de re-consulta (el reloj real no avanza).
        _RATE_LIMIT["hasta"] = 0.0

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return registro


async def _correr_worker(ids: list[str]) -> None:
    cola: asyncio.Queue = asyncio.Queue()
    for pid in ids:
        await cola.put(pid)
    await cola.put(None)  # sentinel — el worker puede terminar antes por circuito
    await sw.scraper_worker(0, _FakeContext(), cola, asyncio.Queue(), "db-fake")


@pytest.mark.unit
async def test_circuito_abre_y_termina_tras_max_reaperturas(monkeypatch, circuito_corto, sleeps):
    """threshold=2, max_reopenings=1: tras 4 fallos consecutivos el worker
    abre el circuito dos veces y termina sin consumir el resto de la cola."""
    llamadas = {"n": 0}

    async def _siempre_falla(worker_id, page, pid, rq, db, max_reintentos=None):
        llamadas["n"] += 1
        return False

    monkeypatch.setattr(sw, "procesar_pedido", _siempre_falla)

    await _correr_worker(["P1", "P2", "P3", "P4", "P5", "P6"])

    # 2 fallos -> cooldown (reapertura 1, permitida) -> 2 fallos -> cooldown
    # -> reapertura 2 > max 1 -> worker terminado. P5/P6 quedan sin consumir.
    assert llamadas["n"] == 4
    assert sleeps == [99, 99]


@pytest.mark.unit
async def test_exito_resetea_fallos_consecutivos(monkeypatch, circuito_corto, sleeps):
    """Un éxito antes del umbral reinicia el contador: F,S,F,S,... nunca abre."""
    llamadas = {"n": 0}

    async def _alterna(worker_id, page, pid, rq, db, max_reintentos=None):
        llamadas["n"] += 1
        return llamadas["n"] % 2 == 0  # falla impares, acierta pares

    monkeypatch.setattr(sw, "procesar_pedido", _alterna)

    await _correr_worker(["P1", "P2", "P3", "P4", "P5", "P6"])

    assert llamadas["n"] == 6  # consumió toda la cola
    assert sleeps == []  # el circuito jamás abrió


@pytest.mark.unit
async def test_circuito_cerrado_reanuda_tras_cooldown(monkeypatch, circuito_corto, sleeps):
    """Tras el primer cooldown (reapertura permitida) el worker sigue
    consumiendo la cola con el contador en cero."""
    llamadas = {"n": 0}

    async def _falla_dos_luego_ok(worker_id, page, pid, rq, db, max_reintentos=None):
        llamadas["n"] += 1
        return llamadas["n"] > 2

    monkeypatch.setattr(sw, "procesar_pedido", _falla_dos_luego_ok)

    await _correr_worker(["P1", "P2", "P3", "P4"])

    assert llamadas["n"] == 4  # se reanudó y terminó la cola
    assert sleeps == [99]  # un solo cooldown


@pytest.mark.unit
async def test_worker_duerme_ante_rate_limit_activo(monkeypatch, sleeps):
    """AUD-M2 + cobertura del loop: con señal de 429 activa, el worker
    duerme ANTES de procesar el siguiente pedido."""
    procesados: list[str] = []

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None):
        procesados.append(pid)
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)
    registrar_rate_limit("30")  # señal activa: ~30s pendientes

    await _correr_worker(["P1"])

    assert len(sleeps) == 1
    assert 29 <= sleeps[0] <= 30
    assert procesados == ["P1"]  # procesó después de la pausa
