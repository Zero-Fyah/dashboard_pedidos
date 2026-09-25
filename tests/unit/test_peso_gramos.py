"""Tests de `a_gramos()` — DEC-140.

El origen escribe el peso de dos formas: gramos enteros ("500g") y kilos
con punto DECIMAL ("4.5KG"). `to_num` lee ese punto como separador de miles
y devolvía 45 por 4,5 kg; además `peso_total_num` mezclaba gramos con kilos.
"""

import pytest

from comun import COLUMNAS_PESO, a_gramos, normalizar_numerico


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("500g", 500.0),
        ("14460g", 14460.0),
        ("0g", 0.0),
        ("4.5KG", 4500.0),
        ("0.1KG", 100.0),
        ("135KG", 135000.0),
        ("0KG", 0.0),
        (" 3 kg ", 3000.0),
    ],
)
def test_a_gramos_formatos_del_origen(entrada, esperado):
    assert a_gramos(entrada) == esperado


@pytest.mark.unit
@pytest.mark.parametrize("entrada", ["g", "", "-", "1.234g", "abc", None, "12"])
def test_a_gramos_formato_no_reconocido_es_none(entrada):
    """'1.234g' es ambiguo (¿miles o decimal?) y no aparece en los datos:
    mejor fallar y que el ETL lo reporte que adivinar."""
    assert a_gramos(entrada) is None


@pytest.mark.unit
def test_normalizar_numerico_peso_usa_gramos_no_to_num():
    assert normalizar_numerico("1.5KG", es_peso=True) == 1500.0
    assert normalizar_numerico("1.5KG") == 15.0  # el defecto que corrige DEC-140


@pytest.mark.unit
@pytest.mark.parametrize("placeholder", ["-", "g", ""])
def test_normalizar_numerico_peso_respeta_placeholders(placeholder):
    assert normalizar_numerico(placeholder, es_peso=True) is None


@pytest.mark.unit
def test_columnas_peso_declaradas():
    assert COLUMNAS_PESO == {"peso_total", "peso_entregado"}
