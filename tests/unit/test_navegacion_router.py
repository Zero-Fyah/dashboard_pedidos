"""Tests de navegar_a_detalle_via_router() (auditoría de rendimiento
2026-08-26). La decisión `usar_push` de procesar_pedido() vive en
`tests/integration/test_procesar_pedido_modo.py` (usa SQLite real).

navegar_a_detalle_via_router() se prueba con fakes de Page (mismo patrón
que test_extractores_batch.py) — el router.push() en sí no es verificable
sin browser real; se validó contra la sesión real en el piloto (2.280
pedidos, 6 workers, 2.280/2.280 correctos). Acá se fija el contrato:
retorna bool, nunca lanza, y usa la ruta/selector correctos.
"""

import pytest

import scraper.extractores as ext
from scraper.extractores import navegar_a_detalle_via_router

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
