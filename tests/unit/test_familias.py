"""Tests de las familias de producto — DEC-041.

La familia son los 2 primeros caracteres de la referencia (confirmado por
el Arquitecto 2026-07-25). Las averías son referencias propias dentro de
la familia del producto de origen, así que no requieren caso especial.
"""

import pytest

from comun import FAMILIAS_PRODUCTO, familia_de

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "referencia,esperado",
    [
        ("PA01", "PA"),
        ("PB102", "PB"),
        ("PC80", "PC"),
        ("PH12", "PH"),
        ("PJ91", "PJ"),
        ("PO07", "PO"),
        ("PP13", "PP"),
        ("PR09", "PR"),
        ("PS20-29", "PS"),
        # PW todavía no tiene existencias pero ya está en el catálogo
        ("PW01", "PW"),
    ],
)
def test_familia_de_reconoce_las_familias_activas(referencia, esperado):
    assert familia_de(referencia) == esperado


def test_familia_de_averia_hereda_la_familia_de_origen():
    """'PJ91 AVERIA' es referencia propia, pero de la familia PJ (DEC-041)."""
    assert familia_de("PJ91 AVERIA") == familia_de("PJ91") == "PJ"


def test_familia_de_tolera_espacios_al_inicio():
    """Hay 2 referencias del catálogo que empiezan con espacio (DEC-041)."""
    assert familia_de(" PC40 AVERIA") == "PC"


@pytest.mark.parametrize("referencia", ["", None, "AR01", "YUM-1", "12345", "ER99"])
def test_familia_de_devuelve_none_fuera_del_catalogo(referencia):
    """Legados y familias recibidas por peso no se fuerzan a una familia."""
    assert familia_de(referencia) is None


def test_familias_producto_es_tupla_ordenada_y_sin_duplicados():
    """Tupla para SQL determinístico, mismo criterio que ESTADOS_ACTIVOS_INVENTARIO."""
    assert isinstance(FAMILIAS_PRODUCTO, tuple)
    assert list(FAMILIAS_PRODUCTO) == sorted(FAMILIAS_PRODUCTO)
    assert len(set(FAMILIAS_PRODUCTO)) == len(FAMILIAS_PRODUCTO)
    assert all(len(f) == 2 and f.isupper() for f in FAMILIAS_PRODUCTO)
