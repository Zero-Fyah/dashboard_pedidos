"""`procesar_pedido()` — decisión de navegación y de modo contra SQLite real.

Separado de `tests/unit/test_navegacion_router.py` en la auditoría de tests
del 2026-09-24: estos tests siembran pedidos en una base en `tmp_path`
(`db_path`), y por la regla del proyecto eso es `integration`, no `unit`
(misma clase de error que corrigió DEC-038). Cubren el uso del router con
`usar_push` y el modo completo forzado en memoria (DEC-142).
"""

import pytest

import scraper.workers as sw
from scraper.workers import procesar_pedido

pytestmark = pytest.mark.integration


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


# ── DEC-142: modo completo forzado en memoria ─────────────────────────────────


class _PageRegistraSelectores(_PageSoloEstado):
    def __init__(self):
        super().__init__()
        self.selectores: list[str] = []

    async def wait_for_selector(self, selector, *args, **kwargs):
        self.selectores.append(selector)


async def test_forzar_completo_ignora_lo_que_dice_la_base(monkeypatch, db_path):
    """El pedido está completo y abierto en la base (iría a solo_estado);
    forzado, va a `completo` sin consultar determinar_modo ni marcar la base."""
    await _sembrar_pedido_solo_estado(db_path, "TEST-FORZADO")
    consultas = []
    monkeypatch.setattr(sw, "determinar_modo", lambda *a: consultas.append(a) or "solo_estado")
    monkeypatch.setattr(sw, "navegar_a_detalle_via_router", lambda page, pid: _resuelve(False))

    page = _PageRegistraSelectores()
    await procesar_pedido(
        0,
        page,
        "TEST-FORZADO",
        __import__("asyncio").Queue(),
        db_path,
        max_reintentos=1,
        forzar_completo=True,
    )

    assert consultas == []
    assert "div.info-item" in page.selectores  # espera propia del modo completo
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        sc = (
            await (
                await db.execute(
                    "SELECT scraping_completo FROM pedidos WHERE id_pedido = 'TEST-FORZADO'"
                )
            ).fetchone()
        )[0]
    assert sc == 1  # nada queda marcado en la base


async def test_sin_forzar_decide_la_base(monkeypatch, db_path):
    await _sembrar_pedido_solo_estado(db_path, "TEST-NO-FORZADO")
    monkeypatch.setattr(sw, "navegar_a_detalle_via_router", lambda page, pid: _resuelve(False))

    page = _PageRegistraSelectores()
    exito = await procesar_pedido(
        0, page, "TEST-NO-FORZADO", __import__("asyncio").Queue(), db_path
    )

    assert exito is True
    assert "div.info-item" not in page.selectores  # fue por solo_estado
