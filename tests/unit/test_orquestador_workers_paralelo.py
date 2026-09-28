"""Tests de _preparar_contexto_worker() y su paralelización (auditoría de
rendimiento 2026-08-26).

Antes, main() creaba y autenticaba los NUM_WORKERS contextos uno por uno
(medido: ~31s/ciclo en 6 logins secuenciales). _preparar_contexto_worker()
aísla esa secuencia por worker para que main() pueda lanzarlas todas con
asyncio.gather() — cada contexto es independiente, sin estado compartido.
"""

import asyncio
import time

import pytest

import scraper.orquestador as orq

pytestmark = pytest.mark.unit


class _FakePage:
    async def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self.rutas_bloqueadas = False

    async def route(self, patron, handler):
        self.rutas_bloqueadas = True

    async def new_page(self):
        return _FakePage()


class _FakeBrowser:
    def __init__(self):
        self.contextos_creados: list[dict] = []

    async def new_context(self, **kwargs):
        self.contextos_creados.append(kwargs)
        return _FakeContext()


async def test_preparar_contexto_worker_autentica_y_cierra_pagina(monkeypatch):
    llamadas: list[tuple] = []

    async def _login_fake(page, usuario, clave):
        llamadas.append((usuario, clave))

    monkeypatch.setattr(orq, "login", _login_fake)
    monkeypatch.setattr(orq, "USUARIO", "usuario_test")
    monkeypatch.setattr(orq, "CLAVE", "clave_test")

    ctx = await orq._preparar_contexto_worker(_FakeBrowser(), 0)

    assert isinstance(ctx, _FakeContext)
    assert ctx.rutas_bloqueadas is True
    assert llamadas == [("usuario_test", "clave_test")]


async def test_preparar_contexto_worker_asigna_viewport_y_user_agent_por_indice(monkeypatch):
    """wid determina qué viewport/user_agent le toca — mismo criterio que
    antes de la paralelización, solo que ahora vive en un helper aislado."""

    async def _login_fake(page, usuario, clave):
        pass

    monkeypatch.setattr(orq, "login", _login_fake)
    browser = _FakeBrowser()

    await orq._preparar_contexto_worker(browser, 0)
    await orq._preparar_contexto_worker(browser, 1)

    assert browser.contextos_creados[0]["viewport"] == orq._VIEWPORTS[0]
    assert browser.contextos_creados[1]["viewport"] == orq._VIEWPORTS[1]
    assert browser.contextos_creados[0]["user_agent"] != browser.contextos_creados[1]["user_agent"]


async def test_logins_de_workers_corren_en_paralelo_no_secuencial(monkeypatch):
    """El tiempo total de preparar N workers debe acercarse a UN login, no
    a N logins sumados — así se mide la ganancia real de paralelizar."""
    demora_s = 0.05
    n_workers = 6

    async def _login_lento(page, usuario, clave):
        await asyncio.sleep(demora_s)

    monkeypatch.setattr(orq, "login", _login_lento)
    browser = _FakeBrowser()

    t0 = time.monotonic()
    contexts = await asyncio.gather(
        *[orq._preparar_contexto_worker(browser, wid) for wid in range(n_workers)]
    )
    transcurrido = time.monotonic() - t0

    assert len(contexts) == n_workers
    # Secuencial habría tardado ~n_workers * demora_s = 0.3s; en paralelo
    # debe acercarse a un solo demora_s. Margen 3x para tolerar CI lento
    # sin dejar de detectar una regresión a secuencial (que daría ~6x).
    assert transcurrido < demora_s * 3


# ── DEC-142: un worker que no entra ya no tumba el ciclo ──────────────────────


class _FakeContextCerrable(_FakeContext):
    def __init__(self):
        super().__init__()
        self.cerrado = False

    async def close(self):
        self.cerrado = True


class _FakeBrowserCerrable(_FakeBrowser):
    def __init__(self):
        super().__init__()
        self.contextos: list[_FakeContextCerrable] = []

    async def new_context(self, **kwargs):
        ctx = _FakeContextCerrable()
        self.contextos.append(ctx)
        return ctx


def _login_que_falla_para(wids_que_fallan: set[int]):
    llamada = {"n": 0}

    async def _login(page, usuario, clave):
        wid = llamada["n"]
        llamada["n"] += 1
        if wid in wids_que_fallan:
            raise RuntimeError("Login fallido tras 3 intentos")

    return _login


async def test_worker_que_no_entra_se_descarta_y_el_resto_sigue(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    monkeypatch.setattr(orq, "login", _login_que_falla_para({2}))
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 6)
    browser = _FakeBrowserCerrable()

    workers = await orq._autenticar_workers(browser)

    assert [wid for wid, _ in workers] == [0, 1, 3, 4, 5]
    assert browser.contextos[2].cerrado is True  # el contexto del caído no queda abierto
    assert not any(ctx.cerrado for wid, ctx in workers)
    assert [kw["worker_id"] for ev, kw in eventos if ev == "worker_login_fallido"] == [2]
    resumen = [kw for ev, kw in eventos if ev == "workers_login_completado"][0]
    assert resumen["level"] == "WARNING"
    assert "5/6" in resumen["msg"]


async def test_sin_ningun_worker_autenticado_el_run_se_aborta(monkeypatch):
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: None)
    monkeypatch.setattr(orq, "login", _login_que_falla_para(set(range(6))))
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 6)
    browser = _FakeBrowserCerrable()

    with pytest.raises(RuntimeError, match="Ningún worker"):
        await orq._autenticar_workers(browser)
    assert all(ctx.cerrado for ctx in browser.contextos)


async def test_todos_autenticados_reporta_info(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    monkeypatch.setattr(orq, "login", _login_que_falla_para(set()))
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 6)

    workers = await orq._autenticar_workers(_FakeBrowserCerrable())

    assert len(workers) == 6
    resumen = [kw for ev, kw in eventos if ev == "workers_login_completado"][0]
    assert resumen["level"] == "INFO"


# ── DEC-144: el worker descartado reintenta el login durante el run ──────────


class _FillTerminado:
    def done(self):
        return True


class _FillEnCurso:
    def done(self):
        return False


def _cola_con(n: int) -> asyncio.Queue:
    cola: asyncio.Queue = asyncio.Queue()
    for i in range(n):
        cola.put_nowait(str(i))
    return cola


def test_hay_trabajo_mientras_el_llenado_sigue(monkeypatch):
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 6)

    assert orq._hay_trabajo_para_reintento(_FillEnCurso(), _cola_con(0)) is True


def test_hay_trabajo_solo_si_la_cola_supera_los_centinelas(monkeypatch):
    """Con el llenado terminado la cola lleva hasta NUM_WORKERS centinelas:
    solo más ítems que eso garantizan un pedido real."""
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 6)

    assert orq._hay_trabajo_para_reintento(_FillTerminado(), _cola_con(7)) is True
    assert orq._hay_trabajo_para_reintento(_FillTerminado(), _cola_con(6)) is False


def _config_reintento(monkeypatch, espera_s=0, rondas=3):
    monkeypatch.setitem(orq.CONFIG, "LOGIN_REINTENTO_ESPERA_S", espera_s)
    monkeypatch.setitem(orq.CONFIG, "LOGIN_REINTENTO_RONDAS", rondas)
    monkeypatch.setattr(orq, "_TRAMO_ESPERA_REINTENTO_S", 0.01)


async def test_reintento_entra_en_la_segunda_ronda(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    monkeypatch.setattr(orq, "login", _login_que_falla_para({0}))  # 1.ª llamada falla
    _config_reintento(monkeypatch)
    browser = _FakeBrowserCerrable()

    ctx = await orq._reautenticar_worker(browser, 4, lambda: True)

    assert ctx is browser.contextos[1]
    assert ctx.cerrado is False
    assert browser.contextos[0].cerrado is True
    assert [ev for ev, _ in eventos] == [
        "worker_login_reintento_fallido",
        "worker_login_reintento_ok",
    ]
    assert all(kw["worker_id"] == 4 for _, kw in eventos)


async def test_reintento_agotado_devuelve_none_y_no_deja_contextos(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    monkeypatch.setattr(orq, "login", _login_que_falla_para({0, 1, 2}))
    _config_reintento(monkeypatch, rondas=3)
    browser = _FakeBrowserCerrable()

    assert await orq._reautenticar_worker(browser, 1, lambda: True) is None
    assert len(browser.contextos) == 3
    assert all(c.cerrado for c in browser.contextos)
    assert eventos[-1][0] == "worker_login_reintento_agotado"
    assert eventos[-1][1]["level"] == "ERROR"


async def test_sin_trabajo_no_intenta_el_login(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    _config_reintento(monkeypatch)
    browser = _FakeBrowserCerrable()

    assert await orq._reautenticar_worker(browser, 2, lambda: False) is None
    assert browser.contextos == []
    assert [ev for ev, _ in eventos] == ["worker_login_reintento_omitido"]


async def test_la_espera_se_corta_si_la_cola_se_vacia(monkeypatch):
    """Con 60 s de espera configurados, el worker se retira en cuanto deja
    de haber trabajo — no retiene el cierre del run."""
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: None)
    _config_reintento(monkeypatch, espera_s=60)
    consultas = {"n": 0}

    def _hay_trabajo():
        consultas["n"] += 1
        return consultas["n"] < 3

    browser = _FakeBrowserCerrable()
    ctx = await asyncio.wait_for(orq._reautenticar_worker(browser, 0, _hay_trabajo), timeout=1)

    assert ctx is None
    assert browser.contextos == []


def _consumidor_fake(procesados: list[tuple[int, str]], cola: asyncio.Queue):
    """Imita el contrato de scraper_worker con la cola: consume hasta el
    primer centinela."""

    async def _consumir(wid, ctx):
        while True:
            pid = await cola.get()
            if pid is None:
                return
            procesados.append((wid, pid))
            await asyncio.sleep(0.001)

    return _consumir


async def test_worker_tardio_se_suma_a_la_cola_y_nadie_queda_bloqueado(monkeypatch):
    """Dos workers de arranque + uno tardío sobre la cola que arma
    `_llenar_cola`: cada pedido se procesa una sola vez, el tardío participa,
    todos terminan (con un centinela por worker vivo, el tardío quedaría
    bloqueado) y el tardío cierra su contexto."""
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: None)
    monkeypatch.setattr(orq, "login", _login_que_falla_para(set()))
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 3)
    _config_reintento(monkeypatch, espera_s=0.02)
    cola: asyncio.Queue = asyncio.Queue()
    await orq._llenar_cola(cola, [f"P{i}" for i in range(200)])
    procesados: list[tuple[int, str]] = []
    consumir = _consumidor_fake(procesados, cola)
    browser = _FakeBrowserCerrable()

    await asyncio.wait_for(
        asyncio.gather(
            consumir(0, None),
            consumir(1, None),
            orq._worker_tardio(browser, 2, lambda: cola.qsize() > 3, consumir),
        ),
        timeout=5,
    )

    assert sorted(pid for _, pid in procesados) == sorted(f"P{i}" for i in range(200))
    assert any(wid == 2 for wid, _ in procesados)
    assert browser.contextos[0].cerrado is True


async def test_worker_tardio_que_no_entra_no_bloquea_a_los_demas(monkeypatch):
    monkeypatch.setattr(orq, "log_event", lambda ev, **kw: None)
    monkeypatch.setattr(orq, "login", _login_que_falla_para({0, 1, 2}))
    monkeypatch.setitem(orq.CONFIG, "NUM_WORKERS", 3)
    _config_reintento(monkeypatch, espera_s=0, rondas=3)
    cola: asyncio.Queue = asyncio.Queue()
    await orq._llenar_cola(cola, [f"P{i}" for i in range(50)])
    procesados: list[tuple[int, str]] = []
    consumir = _consumidor_fake(procesados, cola)

    await asyncio.wait_for(
        asyncio.gather(
            consumir(0, None),
            consumir(1, None),
            orq._worker_tardio(_FakeBrowserCerrable(), 2, lambda: True, consumir),
        ),
        timeout=5,
    )

    assert len(procesados) == 50
    assert {wid for wid, _ in procesados} <= {0, 1}


# ── DEC-150: flags del navegador ──────────────────────────────────────────


def test_argumentos_navegador_sin_opciones_no_agrega_flags(monkeypatch):
    monkeypatch.setitem(orq.CONFIG, "BLOQUEAR_IMAGENES", False)
    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", False)
    assert orq.argumentos_navegador() == []


def test_argumentos_navegador_con_bloqueo_apaga_imagenes_en_blink(monkeypatch):
    monkeypatch.setitem(orq.CONFIG, "BLOQUEAR_IMAGENES", True)
    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", False)
    assert orq.argumentos_navegador() == ["--blink-settings=imagesEnabled=false"]


def test_argumentos_navegador_v8_optimiza_tamano(monkeypatch):
    monkeypatch.setitem(orq.CONFIG, "BLOQUEAR_IMAGENES", False)
    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", True)
    assert orq.argumentos_navegador() == ["--js-flags=--optimize-for-size"]


def test_argumentos_navegador_ambas_opciones(monkeypatch):
    monkeypatch.setitem(orq.CONFIG, "BLOQUEAR_IMAGENES", True)
    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", True)
    assert orq.argumentos_navegador() == [
        "--blink-settings=imagesEnabled=false",
        "--js-flags=--optimize-for-size",
    ]


def test_flags_v8_apagado_no_agrega_nada(monkeypatch):
    from scraper.config import flags_v8

    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", False)
    assert flags_v8() == []


def test_flags_v8_encendido_optimiza_tamano(monkeypatch):
    """DEC-154: la misma regla la usa la descarga de BOCHICA."""
    from scraper.config import flags_v8

    monkeypatch.setitem(orq.CONFIG, "V8_OPTIMIZAR_TAMANO", True)
    assert flags_v8() == ["--js-flags=--optimize-for-size"]
