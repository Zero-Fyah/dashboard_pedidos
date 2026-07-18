"""Tests de la semántica de placeholders — DEC-025.

El guion significa CERO solo en las columnas de COLUMNAS_GUION_ES_CERO
(regla de negocio 2026-07-18). En el resto significa "no aplica" → NULL:
`precio_descuento='-'` es "sin descuento aplicado", no "precio cero".
"""

import pytest

from comun import (
    COLUMNAS_GUION_ES_CERO,
    es_placeholder,
    normalizar_numerico,
    to_num,
)


@pytest.mark.unit
@pytest.mark.parametrize("guion", ["-", "--", " - "])
def test_guion_es_cero_donde_el_negocio_lo_define(guion):
    assert normalizar_numerico(guion, guion_es_cero=True) == 0.0


@pytest.mark.unit
@pytest.mark.parametrize("guion", ["-", "--", " - "])
def test_guion_es_null_en_el_resto_de_columnas(guion):
    """DEC-025: convertirlo a 0 pondría en cero el precio de 246.854 líneas."""
    assert normalizar_numerico(guion, guion_es_cero=False) is None


@pytest.mark.unit
@pytest.mark.parametrize("vacio", ["", "  ", "g"])
def test_ausencia_de_dato_siempre_es_null(vacio):
    """'' y 'g' son ausencia incluso donde el guion vale cero: el peso
    cero real se escribe '0g'."""
    assert normalizar_numerico(vacio, guion_es_cero=True) is None
    assert normalizar_numerico(vacio, guion_es_cero=False) is None


@pytest.mark.unit
def test_peso_cero_real_se_conserva():
    """'0g' es un peso de cero medido, no una ausencia."""
    assert normalizar_numerico("0g") == 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "entrada,esperado",
    [("COP 1.940", 1940.0), ("-COP 137.706", -137706.0), ("1.234,56", 1234.56)],
)
def test_valores_reales_se_convierten_igual_que_to_num(entrada, esperado):
    """La capa de negocio no altera el parseo de formato."""
    assert normalizar_numerico(entrada) == esperado
    assert to_num(entrada) == esperado


@pytest.mark.unit
def test_none_es_none():
    assert normalizar_numerico(None) is None
    assert normalizar_numerico(None, guion_es_cero=True) is None


@pytest.mark.unit
@pytest.mark.parametrize("val", ["-", "--", "", "g", None, "  "])
def test_es_placeholder_reconoce_ausencias(val):
    assert es_placeholder(val) is True


@pytest.mark.unit
@pytest.mark.parametrize("val", ["COP 100", "0g", "1.234,56", "Promoción3%"])
def test_es_placeholder_no_marca_valores_reales(val):
    """Un formato inesperado ('Promoción3%') NO es placeholder: debe
    seguir emitiendo WARNING en el ETL."""
    assert es_placeholder(val) is False


@pytest.mark.unit
def test_to_num_no_cambio_de_comportamiento():
    """SRP (DEC-022): to_num sigue siendo parser puro — el scraper lo usa
    para cantidades y no debe ver la semántica de negocio."""
    assert to_num("-") is None
    assert to_num("") is None


@pytest.mark.unit
def test_solo_descuento_trata_el_guion_como_cero():
    """La lista es deliberadamente mínima: ampliarla exige revisar la
    semántica de la columna contra los datos (DEC-025)."""
    assert COLUMNAS_GUION_ES_CERO == frozenset({"descuento"})
