"""DEC-152 (D3): `SeguidorSecciones` espera a que el origen entregue las
secciones asíncronas del detalle antes de extraerlas."""

import asyncio
import time

import pytest

import scraper.workers as sw
from scraper.config import CONFIG
from scraper.extractores import SECCIONES_ASYNC, SeguidorSecciones

pytestmark = pytest.mark.unit


class _Req:
    def __init__(self, ruta: str):
        self.url = f"https://admin.example.com{ruta}?x=1"


class _PageEventos:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, evento, handler):
        self.handlers.setdefault(evento, []).append(handler)

    def emitir(self, evento, ruta):
        for h in self.handlers.get(evento, []):
            h(_Req(ruta))


@pytest.fixture(autouse=True)
def _tiempos_cortos(monkeypatch):
    monkeypatch.setitem(CONFIG, "SECCIONES_TIMEOUT_MS", 300)
    monkeypatch.setitem(CONFIG, "SECCIONES_ASENTAR_MS", 0)


def _seguidor_conectado():
    page = _PageEventos()
    seg = SeguidorSecciones()
    seg.conectar(page)
    return seg, page


async def test_secciones_ya_llegadas_no_esperan():
    seg, page = _seguidor_conectado()
    for ruta in SECCIONES_ASYNC.values():
        page.emitir("requestfinished", ruta)
    t0 = time.monotonic()
    assert await seg.esperar(["timeline", "registro_ops", "pagos"]) == set()
    assert time.monotonic() - t0 < 0.1


async def test_espera_la_seccion_que_llega_despues():
    seg, page = _seguidor_conectado()
    page.emitir("requestfinished", SECCIONES_ASYNC["timeline"])
    asyncio.get_running_loop().call_later(
        0.05, page.emitir, "requestfinished", SECCIONES_ASYNC["registro_ops"]
    )
    t0 = time.monotonic()
    assert await seg.esperar(["timeline", "registro_ops"]) == set()
    assert 0.04 < time.monotonic() - t0 < 0.25


async def test_sin_respuesta_devuelve_las_faltantes_al_vencer_el_tope():
    seg, page = _seguidor_conectado()
    page.emitir("requestfinished", SECCIONES_ASYNC["timeline"])
    t0 = time.monotonic()
    assert await seg.esperar(["timeline", "registro_ops", "pagos"]) == {"registro_ops", "pagos"}
    assert time.monotonic() - t0 >= 0.29


async def test_peticion_fallida_cuenta_como_terminada():
    """La sección queda vacía y la persistencia no pisa: no hay que esperar 5 s."""
    seg, page = _seguidor_conectado()
    page.emitir("requestfailed", SECCIONES_ASYNC["pagos"])
    assert await seg.esperar(["pagos"]) == set()


async def test_otras_peticiones_no_cuentan():
    seg, page = _seguidor_conectado()
    page.emitir("requestfinished", "/api/order/orderDetail/123")
    page.emitir("requestfinished", "/api/timerShaft/otraCosa")
    assert await seg.esperar(["timeline"]) == {"timeline"}


async def test_reiniciar_olvida_la_navegacion_anterior():
    """Sin esto, las secciones del pedido anterior darían por llegadas las del nuevo."""
    seg, page = _seguidor_conectado()
    page.emitir("requestfinished", SECCIONES_ASYNC["timeline"])
    seg.reiniciar()
    assert await seg.esperar(["timeline"]) == {"timeline"}


async def test_asienta_desde_la_ultima_llegada(monkeypatch):
    """Vue pinta la sección después de recibirla: se deja un margen."""
    monkeypatch.setitem(CONFIG, "SECCIONES_ASENTAR_MS", 80)
    seg, page = _seguidor_conectado()
    page.emitir("requestfinished", SECCIONES_ASYNC["timeline"])
    t0 = time.monotonic()
    await seg.esperar(["timeline"])
    assert time.monotonic() - t0 >= 0.07


# ── _esperar_secciones: qué espera cada modo y cuándo avisa ─────────────────


class _SeguidorFake:
    def __init__(self, faltan=frozenset()):
        self.pedidas = None
        self._faltan = set(faltan)

    async def esperar(self, secciones):
        self.pedidas = tuple(secciones)
        return self._faltan & set(secciones)


@pytest.mark.parametrize(
    ("modo", "esperadas"),
    [
        ("completo", ("timeline", "registro_ops", "pagos")),
        ("con_cantidades", ("timeline", "registro_ops")),
        ("solo_estado", ("timeline", "registro_ops")),
    ],
)
async def test_cada_modo_espera_solo_lo_que_lee(modo, esperadas):
    seg = _SeguidorFake()
    await sw._esperar_secciones(seg, modo, 0, "TEST-1")
    assert seg.pedidas == esperadas


async def test_seccion_faltante_se_avisa_y_se_sigue(monkeypatch):
    eventos = []
    monkeypatch.setattr(sw, "log_event", lambda ev, **kw: eventos.append((ev, kw)))
    await sw._esperar_secciones(_SeguidorFake({"registro_ops"}), "solo_estado", 3, "TEST-2")
    assert [e for e, _ in eventos] == ["secciones_async_incompletas"]
    assert eventos[0][1]["level"] == "WARNING"
    assert "registro_ops" in eventos[0][1]["msg"]


async def test_sin_seguidor_no_espera_ni_avisa(monkeypatch):
    eventos = []
    monkeypatch.setattr(sw, "log_event", lambda ev, **kw: eventos.append(ev))
    await sw._esperar_secciones(None, "completo", 0, "TEST-3")
    assert eventos == []
