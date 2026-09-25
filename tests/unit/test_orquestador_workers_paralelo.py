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
