"""Tests de navegar_a_detalle_via_router() y de la decisión usar_push en
procesar_pedido() (auditoría de rendimiento 2026-08-26).

navegar_a_detalle_via_router() se prueba con fakes de Page (mismo patrón
que test_extractores_batch.py) — el router.push() en sí no es verificable
sin browser real; se validó contra la sesión real en el piloto (2.280
pedidos, 6 workers, 2.280/2.280 correctos). Acá se fija el contrato:
retorna bool, nunca lanza, y usa la ruta/selector correctos.
"""

import pytest

import scraper.extractores as ext
import scraper.workers as sw
from scraper.extractores import navegar_a_detalle_via_router
from scraper.workers import procesar_pedido

pytestmark = pytest.mark.unit


# ── navegar_a_detalle_via_router() ──────────────────────────────────────────


class _PageRouterOk:
    def __init__(self):
        self.ruta_evaluada = None
        self.id_esperado = None

    async def evaluate(self, js, arg=None):
        self.ruta_evaluada = arg

    async def wait_for_function(self, js, arg=None, timeout=None):
        self.id_esperado = arg


class _PageRouterSinApp:
    """Simula __vue_app__ ausente — la SPA no montó o cambió de versión."""

    async def evaluate(self, js, arg=None):
        raise Exception("Cannot read properties of null (reading 'config')")


class _PageRouterTimeoutId:
    """El push no lanza, pero el ID nunca llega a coincidir a tiempo."""

    async def evaluate(self, js, arg=None):
        pass

    async def wait_for_function(self, js, arg=None, timeout=None):
        raise TimeoutError("timeout esperando el ID")


async def test_navegar_via_router_exitoso_usa_la_ruta_y_el_id_correctos():
    page = _PageRouterOk()
    ok = await navegar_a_detalle_via_router(page, "2092454050")
    assert ok is True
    assert page.ruta_evaluada == "/country/CO/orders/parent-orders/detail/2092454050"
    assert page.id_esperado == "2092454050"


async def test_navegar_via_router_sin_app_montada_retorna_false_sin_lanzar(monkeypatch):
    eventos: list[tuple] = []
    monkeypatch.setattr(ext, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    ok = await navegar_a_detalle_via_router(_PageRouterSinApp(), "2092454050")

    assert ok is False
    assert eventos[0][0] == "router_push_fallback"
    assert eventos[0][1]["level"] == "WARNING"
    assert eventos[0][1]["id_pedido"] == "2092454050"


async def test_navegar_via_router_timeout_esperando_id_retorna_false(monkeypatch):
    monkeypatch.setattr(ext, "log_event", lambda evento, **kw: None)

    ok = await navegar_a_detalle_via_router(_PageRouterTimeoutId(), "2092454050")

    assert ok is False


# ── procesar_pedido(usar_push=...) ──────────────────────────────────────────


class _PageSoloEstado:
    """Fake mínimo para el camino solo_estado — sin subpedidos que expandir,
    suficiente para probar solo la decisión de navegación."""

    def __init__(self):
        self.url = "https://admin.example.com/detail/actual"
        self.goto_llamadas = 0

    async def goto(self, *args, **kwargs):
        self.goto_llamadas += 1

    async def wait_for_selector(self, *args, **kwargs):
        pass

    async def query_selector_all(self, *args, **kwargs):
        return []

    async def evaluate(self, js, arg=None):
        return []

    async def screenshot(self, path):
        pass

    async def content(self):
        return "<html></html>"


async def _sembrar_pedido_solo_estado(db_path, id_pedido):
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, scraping_completo) VALUES (?, 1)", (id_pedido,)
        )
        await db.execute(
            "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES (?, '1', 'en proceso')",
            (id_pedido,),
        )
        await db.commit()


async def test_usar_push_true_evita_page_goto_si_el_router_funciona(monkeypatch, db_path):
    await _sembrar_pedido_solo_estado(db_path, "TEST-PUSH-OK")
    monkeypatch.setattr(sw, "navegar_a_detalle_via_router", lambda page, pid: _resuelve(True))

    page = _PageSoloEstado()
    exito = await procesar_pedido(
        0, page, "TEST-PUSH-OK", __import__("asyncio").Queue(), db_path, usar_push=True
    )

    assert exito is True
    assert page.goto_llamadas == 0  # nunca recurrió a goto — el router bastó


async def test_usar_push_true_recurre_a_goto_si_el_router_falla(monkeypatch, db_path):
    await _sembrar_pedido_solo_estado(db_path, "TEST-PUSH-FALLBACK")
    monkeypatch.setattr(sw, "navegar_a_detalle_via_router", lambda page, pid: _resuelve(False))

    page = _PageSoloEstado()
    exito = await procesar_pedido(
        0, page, "TEST-PUSH-FALLBACK", __import__("asyncio").Queue(), db_path, usar_push=True
    )

    assert exito is True
    assert page.goto_llamadas == 1  # el router falló, recurrió a goto — no se perdió el pedido


async def test_usar_push_false_nunca_llama_al_router(monkeypatch, db_path):
    await _sembrar_pedido_solo_estado(db_path, "TEST-SIN-PUSH")
    llamadas_router = []
    monkeypatch.setattr(
        sw,
        "navegar_a_detalle_via_router",
        lambda page, pid: llamadas_router.append(pid) or _resuelve(True),
    )

    page = _PageSoloEstado()
    exito = await procesar_pedido(
        0, page, "TEST-SIN-PUSH", __import__("asyncio").Queue(), db_path, usar_push=False
    )

    assert exito is True
    assert llamadas_router == []  # comportamiento por default: idéntico a antes de esta auditoría
    assert page.goto_llamadas == 1


async def _resuelve(valor):
    return valor
