"""Tests de frame_con_selector() — polling de page.frames (scraper/bochica.py).

Bochica (Google Apps Script, sandbox) renderiza su contenido dentro de un
iframe anidado cuyo nombre se repite entre sub-frames no relacionados —
frame_locator encadenado no lo resuelve de forma confiable (ver
docs/decisions.md DEC-039). frame_con_selector() busca por contenido en
todos los page.frames en su lugar; se testea con fakes de Page/Frame, sin
Playwright ni browser real (mismo criterio que _leer_ids_pagina).
"""

import pytest

from scraper.bochica import frame_con_selector

pytestmark = pytest.mark.unit


class _FakeLocator:
    def __init__(self, count: int, *, error: bool = False):
        self._count = count
        self._error = error

    async def count(self) -> int:
        if self._error:
            raise RuntimeError("frame desprendido a mitad de chequeo")
        return self._count


class _FakeFrame:
    def __init__(self, url: str, matches: dict[str, int], *, error_on: str | None = None):
        self.url = url
        self._matches = matches
        self._error_on = error_on

    def locator(self, selector: str) -> _FakeLocator:
        if selector == self._error_on:
            return _FakeLocator(0, error=True)
        return _FakeLocator(self._matches.get(selector, 0))


class _FakePage:
    def __init__(self, frames: list[_FakeFrame]):
        self.frames = frames
        self.waits = 0

    async def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1


async def test_encuentra_selector_en_primer_frame():
    frame_objetivo = _FakeFrame("about:blank", {"#loginEmail": 1})
    page = _FakePage([frame_objetivo])

    resultado = await frame_con_selector(page, "#loginEmail", timeout_ms=200)

    assert resultado is frame_objetivo
    assert page.waits == 0


async def test_encuentra_selector_solo_en_segundo_frame():
    """El outer iframe (userCodeAppPanel) no tiene el selector; el nested
    userHtmlFrame sí — replica la estructura real observada contra Bochica."""
    outer = _FakeFrame("https://.../userCodeAppPanel", {})
    inner = _FakeFrame("https://.../blank", {"#appContent": 1})
    page = _FakePage([outer, inner])

    resultado = await frame_con_selector(page, "#appContent", timeout_ms=200)

    assert resultado is inner


async def test_ignora_frame_que_lanza_excepcion():
    """Un frame desprendido durante el chequeo no debe abortar la búsqueda."""
    roto = _FakeFrame("https://roto", {}, error_on="#btnGenerarInv")
    sano = _FakeFrame("https://sano", {"#btnGenerarInv": 1})
    page = _FakePage([roto, sano])

    resultado = await frame_con_selector(page, "#btnGenerarInv", timeout_ms=200)

    assert resultado is sano


async def test_timeout_si_ningun_frame_tiene_el_selector():
    page = _FakePage([_FakeFrame("https://sin-match", {})])

    with pytest.raises(Exception, match="Ningún frame contiene"):
        await frame_con_selector(page, "#no-existe", timeout_ms=50)

    assert page.waits >= 1
