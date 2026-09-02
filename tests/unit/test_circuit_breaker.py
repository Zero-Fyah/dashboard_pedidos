"""Tests del circuit breaker de scraper_worker() (Fase 7 — cobertura dirigida).

El worker se testea con procesar_pedido() monkeypatcheado en
scraper.workers (fallos controlados, sin browser ni DB) y un
BrowserContext fake. asyncio.sleep se intercepta para registrar los
cooldowns sin dormir de verdad.
"""

import asyncio
import time

import pytest

import scraper.workers as sw
from scraper.config import _RATE_LIMIT, CONFIG, registrar_rate_limit


class _RelojControlado:
    """Reemplaza time.monotonic() con un valor que se puede avanzar a mano.

    Necesario para test_worker_espera_cola_* (auditoría 2026-08-26): a
    diferencia de un lambda fijo, acá el propio consumidor (la cola fake)
    puede simular que "pasó tiempo" mientras el worker esperaba un ítem.
    """

    def __init__(self, inicio: float = 1000.0):
        self.valor = inicio

    def __call__(self) -> float:
        return self.valor


class _ColaConEspera:
    """Cola fake cuyo get() avanza el reloj controlado antes de responder —
    simula una espera real sin dormir de verdad en el test."""

    def __init__(self, items: list, reloj: _RelojControlado, avance_s: float):
        self._items = list(items)
        self._reloj = reloj
        self._avance_s = avance_s

    async def get(self):
        self._reloj.valor += self._avance_s
        return self._items.pop(0)


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

    async def _siempre_falla(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
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

    async def _alterna(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
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

    async def _falla_dos_luego_ok(
        worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False
    ):
        llamadas["n"] += 1
        return llamadas["n"] > 2

    monkeypatch.setattr(sw, "procesar_pedido", _falla_dos_luego_ok)

    await _correr_worker(["P1", "P2", "P3", "P4"])

    assert llamadas["n"] == 4  # se reanudó y terminó la cola
    assert sleeps == [99]  # un solo cooldown


@pytest.mark.unit
async def test_worker_duerme_ante_rate_limit_activo(monkeypatch, sleeps):
    """AUD-M2 + cobertura del loop: con señal de 429 activa, el worker
    duerme ANTES de procesar el siguiente pedido.

    Reloj congelado: registrar_rate_limit() y rate_limit_pendiente() (ambas
    en scraper.config, mismo objeto módulo `time` que este import) llaman a
    time.monotonic() sin inyectar `ahora` — igual que en producción. Sin
    congelar el reloj, el tiempo real transcurrido entre ambas llamadas
    hacía el resultado no determinístico (flaky bajo carga/CI).
    """
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    procesados: list[str] = []

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        procesados.append(pid)
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)
    registrar_rate_limit("30")  # señal activa: 30s pendientes exactos

    await _correr_worker(["P1"])

    assert sleeps == [30.0]
    assert procesados == ["P1"]  # procesó después de la pausa


# ── AUD-M9: red de seguridad ante excepción no controlada ──────────────────


@pytest.mark.unit
async def test_worker_sobrevive_excepcion_no_controlada_de_procesar_pedido(monkeypatch):
    """AUD-M9: antes de este fix, una excepción escapada de procesar_pedido()
    (fuera de su propio try/except) mataba la task del worker para siempre —
    sin log, sin resultado en la cola, y el resto de la cola sin consumir."""
    llamadas: list[str] = []

    async def _revienta_en_P2(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        llamadas.append(pid)
        if pid == "P2":
            raise RuntimeError("bug no previsto simulado")
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _revienta_en_P2)

    eventos: list[tuple] = []
    monkeypatch.setattr(sw, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    resultados: asyncio.Queue = asyncio.Queue()
    cola: asyncio.Queue = asyncio.Queue()
    for pid in ["P1", "P2", "P3"]:
        await cola.put(pid)
    await cola.put(None)

    await sw.scraper_worker(0, _FakeContext(), cola, resultados, "db-fake")

    # El worker sobrevivió y consumió TODA la cola, no solo hasta P2.
    assert llamadas == ["P1", "P2", "P3"]
    # P2 dejó un resultado de error publicado — no se pierde en silencio.
    publicados = []
    while not resultados.empty():
        publicados.append(resultados.get_nowait())
    ids_error = [r["id_pedido"] for r in publicados if r.get("_error")]
    assert ids_error == ["P2"]
    # Y quedó logueado como ERROR, no solo al final del run.
    errores_loggeados = [e for e in eventos if e[0] == "worker_excepcion_no_controlada"]
    assert len(errores_loggeados) == 1
    assert errores_loggeados[0][1]["level"] == "ERROR"
    assert errores_loggeados[0][1]["id_pedido"] == "P2"


# ── Auditoría de rendimiento 2026-08-26: instrumentación de ocupación ──────


@pytest.mark.unit
async def test_worker_espera_cola_larga_se_loguea(monkeypatch):
    """Si el worker tarda más de 500ms en conseguir un ítem de la cola, se
    loguea worker_espera_cola con la duración real — instrumentación nueva
    para poder atribuir la ocupación baja medida (76,7%) a una causa
    concreta en vez de quedar sin explicar."""
    reloj = _RelojControlado()
    monkeypatch.setattr(time, "monotonic", reloj)

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)

    eventos: list[tuple] = []
    monkeypatch.setattr(sw, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    cola = _ColaConEspera(["P1", None], reloj, avance_s=0.8)
    resultados: asyncio.Queue = asyncio.Queue()
    await sw.scraper_worker(0, _FakeContext(), cola, resultados, "db-fake")

    esperas = [e for e in eventos if e[0] == "worker_espera_cola"]
    assert len(esperas) == 2  # una antes de P1, otra antes del sentinel None
    # Tolerancia de 1ms: 1000.0 + 0.8 no es exactamente representable en
    # float, el mismo redondeo que sufre la conversión real en producción.
    assert abs(esperas[0][1]["duracion_ms"] - 800) <= 1
    assert "sin ítems disponibles" in esperas[0][1]["msg"].lower()
    assert "señal de cierre" in esperas[1][1]["msg"].lower()


@pytest.mark.unit
async def test_worker_espera_cola_corta_no_se_loguea(monkeypatch):
    """Caso normal (cola ya llena por _fill()): get() resuelve casi
    instantáneo y no debe generar ruido en el log."""
    reloj = _RelojControlado()
    monkeypatch.setattr(time, "monotonic", reloj)

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)

    eventos: list[tuple] = []
    monkeypatch.setattr(sw, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    cola = _ColaConEspera(["P1", None], reloj, avance_s=0.1)
    resultados: asyncio.Queue = asyncio.Queue()
    await sw.scraper_worker(0, _FakeContext(), cola, resultados, "db-fake")

    assert not [e for e in eventos if e[0] == "worker_espera_cola"]


@pytest.mark.unit
async def test_rate_limit_espera_loguea_duracion(monkeypatch, sleeps):
    """rate_limit_espera ahora lleva duracion_ms — antes solo tenía la
    pausa en el texto del msg, no analizable en agregado."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    eventos: list[tuple] = []
    monkeypatch.setattr(sw, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)
    registrar_rate_limit("15")

    await _correr_worker(["P1"])

    esperas = [e for e in eventos if e[0] == "rate_limit_espera"]
    assert len(esperas) == 1
    assert esperas[0][1]["duracion_ms"] == 15000


@pytest.mark.unit
async def test_circuit_open_loguea_duracion_del_cooldown(monkeypatch, circuito_corto, sleeps):
    """circuit_open ahora lleva duracion_ms = CIRCUIT_COOLDOWN_S en ms —
    mismo criterio que rate_limit_espera, para que las tres causas de
    inactividad (cola, rate limit, circuit breaker) sean comparables."""
    eventos: list[tuple] = []
    monkeypatch.setattr(sw, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    async def _siempre_falla(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        return False

    monkeypatch.setattr(sw, "procesar_pedido", _siempre_falla)

    await _correr_worker(["P1", "P2"])

    aperturas = [e for e in eventos if e[0] == "circuit_open"]
    assert len(aperturas) == 1
    assert aperturas[0][1]["duracion_ms"] == CONFIG["CIRCUIT_COOLDOWN_S"] * 1000


# ── Auditoría de rendimiento 2026-08-26: página reutilizada + refresco ─────


@pytest.mark.unit
async def test_refresco_periodico_alterna_goto_y_push(monkeypatch):
    """El primer pedido y cada REFRESH_CADA_N_PEDIDOS deben pedir
    usar_push=False (page.goto completo, arranque/refresco); el resto,
    usar_push=True (navegación interna vía router)."""
    llamadas_usar_push: list[bool] = []

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        llamadas_usar_push.append(usar_push)
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)
    monkeypatch.setattr(sw, "REFRESH_CADA_N_PEDIDOS", 3)

    await _correr_worker([f"P{i}" for i in range(1, 8)])

    assert llamadas_usar_push == [False, True, True, False, True, True, False]


@pytest.mark.unit
async def test_fallo_fuerza_goto_en_el_siguiente_pedido(monkeypatch):
    """Un fallo deja la página en estado incierto — el pedido siguiente NO
    debe encadenar un push sobre esa incertidumbre, aunque no haya llegado
    al punto de refresco periódico."""
    llamadas_usar_push: list[bool] = []

    async def _falla_p2(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        llamadas_usar_push.append(usar_push)
        return pid != "P2"

    monkeypatch.setattr(sw, "procesar_pedido", _falla_p2)
    monkeypatch.setattr(sw, "REFRESH_CADA_N_PEDIDOS", 50)

    await _correr_worker(["P1", "P2", "P3"])

    assert llamadas_usar_push == [False, True, False]


@pytest.mark.unit
async def test_pagina_se_crea_y_cierra_una_sola_vez_por_worker(monkeypatch):
    """Antes de esta auditoría, cada pedido creaba y cerraba su propia
    página. Ahora debe ser una sola página para todo el lote del worker."""
    paginas_creadas: list[_FakePage] = []

    class _ContextoContador:
        async def new_page(self):
            p = _FakePage()
            paginas_creadas.append(p)
            return p

    async def _ok(worker_id, page, pid, rq, db, max_reintentos=None, usar_push=False):
        return True

    monkeypatch.setattr(sw, "procesar_pedido", _ok)

    cola: asyncio.Queue = asyncio.Queue()
    for pid in ["P1", "P2", "P3"]:
        await cola.put(pid)
    await cola.put(None)
    await sw.scraper_worker(0, _ContextoContador(), cola, asyncio.Queue(), "db-fake")

    assert len(paginas_creadas) == 1
