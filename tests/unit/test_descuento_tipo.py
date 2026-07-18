"""Tests de leer_celda_descuento — DEC-024.

La celda de descuento combina una ranura de monto y etiquetas de tipo.
Los fakes reproducen las variantes reales observadas en el DOM y en la DB:
solo tag, promoción en la ranura, y varias etiquetas apiladas.
"""

import pytest

from scraper.extractores import leer_celda_descuento


class _FakeSpan:
    def __init__(self, texto: str, clase: str = ""):
        self._texto = texto
        self._clase = clase

    async def get_attribute(self, nombre: str):
        return self._clase if nombre == "class" else None

    async def inner_text(self) -> str:
        return self._texto


class _FakeCelda:
    """Celda de descuento.

    spans: los <span> en orden de documento (incluye los del el-tag).
    tags:  los textos de .el-tag__content.
    """

    def __init__(self, spans: list[_FakeSpan], tags: list[str], texto: str = ""):
        self._spans = spans
        self._tags = tags
        self._texto = texto

    async def query_selector_all(self, sel: str):
        if sel == ".el-tag__content":
            return [_FakeSpan(t) for t in self._tags]
        if sel == "span":
            return self._spans
        return []

    async def inner_text(self) -> str:
        return self._texto


@pytest.mark.unit
async def test_descuento_guion_con_una_etiqueta():
    """Caso mayoritario: ranura '-' + un tag de tipo de cambio."""
    celda = _FakeCelda(
        spans=[_FakeSpan("-"), _FakeSpan("Tipo de cambio9%", "el-tag__content")],
        tags=["Tipo de cambio9%"],
        texto="-\nTipo de cambio9%",
    )
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "-"
    assert tipos == "Tipo de cambio9%"


@pytest.mark.unit
async def test_descuento_varias_etiquetas_se_unen():
    """Promoción + tipo de cambio apilados en la misma celda."""
    celda = _FakeCelda(
        spans=[_FakeSpan("-")],
        tags=["Promoción30%", "Tipo de cambio5%"],
        texto="-\nPromoción30%\nTipo de cambio5%",
    )
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "-"
    assert tipos == "Promoción30% | Tipo de cambio5%"


@pytest.mark.unit
async def test_promocion_en_la_ranura_se_reclasifica_como_tipo():
    """DEC-024: si la ranura del monto trae una etiqueta (no convertible
    por to_num), va a los tipos y el monto queda en '-'.

    Antes de DEC-024 esas filas guardaban 'Promoción3%' en `descuento`,
    una columna que el ETL intenta normalizar a REAL.
    """
    celda = _FakeCelda(
        spans=[],  # la celda arranca con el tag: no hay span de monto
        tags=["Promoción3%"],
        texto="Promoción3%",
    )
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "-"
    assert tipos == "Promoción3%"


@pytest.mark.unit
async def test_monto_real_se_conserva_como_monto():
    """Si algún día la SPA expone un monto, no se reclasifica."""
    celda = _FakeCelda(
        spans=[_FakeSpan("COP 648")],
        tags=["Tipo de cambio9%"],
        texto="COP 648\nTipo de cambio9%",
    )
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "COP 648"
    assert tipos == "Tipo de cambio9%"


@pytest.mark.unit
async def test_celda_sin_etiquetas():
    """Sin tags, tipos queda vacío y el monto se conserva."""
    celda = _FakeCelda(spans=[_FakeSpan("-")], tags=[], texto="-")
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "-"
    assert tipos == ""


@pytest.mark.unit
async def test_etiquetas_duplicadas_no_se_repiten():
    """El mismo texto en la ranura y en un tag no se duplica."""
    celda = _FakeCelda(
        spans=[_FakeSpan("Promoción3%", "")],
        tags=["Promoción3%", "Tipo de cambio3%"],
        texto="Promoción3%\nTipo de cambio3%",
    )
    monto, tipos = await leer_celda_descuento(celda)
    assert monto == "-"
    assert tipos == "Promoción3% | Tipo de cambio3%"
