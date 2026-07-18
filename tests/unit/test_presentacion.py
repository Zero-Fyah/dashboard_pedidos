"""Tests de leer_presentacion — DEC-026.

.goods-specs puede traer varios <span> (Tamaño, Color, Presentación...).
Antes se leía con query_selector (singular) y se perdía todo menos el
primero. Los fakes reproducen las variantes reales del DOM.
"""

import pytest

from scraper.extractores import leer_presentacion


class _FakeSpan:
    def __init__(self, texto: str):
        self._texto = texto

    async def inner_text(self) -> str:
        return self._texto


class _FakeInfoCol:
    def __init__(self, specs: list[str]):
        self._specs = specs

    async def query_selector_all(self, sel: str):
        assert sel == ".goods-specs span"
        return [_FakeSpan(t) for t in self._specs]


@pytest.mark.unit
async def test_un_solo_atributo():
    res = await leer_presentacion(_FakeInfoCol(["Presentacion: PRA13, CLASICA, 4.5KG"]))
    assert res == "Presentacion: PRA13, CLASICA, 4.5KG"


@pytest.mark.unit
async def test_dos_atributos_se_unen():
    """DEC-026: antes esto perdía 'Color: Rosado'."""
    res = await leer_presentacion(_FakeInfoCol(["Tamaño: L- 35-50cm  ", "Color: Rosado"]))
    assert res == "Tamaño: L- 35-50cm | Color: Rosado"


@pytest.mark.unit
async def test_span_vacio_se_omite():
    """Caso real: '<span>Tamaño: ...</span><span>: </span>' con texto vacío."""
    res = await leer_presentacion(_FakeInfoCol(["Tamaño: M", ""]))
    assert res == "Tamaño: M"


@pytest.mark.unit
async def test_sin_specs():
    assert await leer_presentacion(_FakeInfoCol([])) == ""


@pytest.mark.unit
async def test_sin_info_col():
    assert await leer_presentacion(None) == ""
