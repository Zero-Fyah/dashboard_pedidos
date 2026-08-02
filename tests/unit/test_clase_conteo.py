"""Criterio único de clase para la cola de conteo (DEC-067).

Antes había dos definiciones de "A/B" en el proyecto —una por línea en la
cola de conteo, otra por posición en las alertas— que daban 1.516 contra
1.817 posiciones para el mismo concepto. El criterio vive ahora en `comun/`.
"""

import pandas as pd
import pytest

from comun import (
    CLASES_CONTEO_PRIORITARIO,
    SUFIJO_CLASE_HEREDADA,
    es_conteo_prioritario,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("clase", ["A", "B", "A (por referencia)", "B (por referencia)"])
def test_las_cuatro_clases_prioritarias(clase):
    assert es_conteo_prioritario(clase)


@pytest.mark.parametrize("clase", ["C", "C (por referencia)", "Sin rotación", "Sin consumo"])
def test_las_no_prioritarias(clase):
    assert not es_conteo_prioritario(clase)


def test_la_clase_heredada_cuenta_como_propia():
    """Un "A por referencia" sigue siendo prioritario: tiene menos certeza,
    pero no menos urgencia. Excluirlo dejaba 301 posiciones fuera de la cola."""
    assert es_conteo_prioritario("A" + SUFIJO_CLASE_HEREDADA)


@pytest.mark.parametrize("vacio", [None, float("nan"), pd.NA])
def test_tolera_valores_ausentes(vacio):
    """Una posición vacía no tiene clase; no debe reventar ni colarse."""
    assert not es_conteo_prioritario(vacio)


def test_no_usa_startswith():
    """Se descartó `startswith(("A","B"))` a propósito: una clase futura
    llamada "Ampliado" o "Bloqueado" entraría sola a la cola de conteo."""
    assert not es_conteo_prioritario("Ampliado")
    assert not es_conteo_prioritario("Bloqueado")


def test_el_conjunto_tiene_exactamente_cuatro():
    assert len(CLASES_CONTEO_PRIORITARIO) == 4


def test_el_sufijo_es_el_mismo_que_usa_ubicaciones():
    """DEC-067: `ubicaciones.py` dejó de declarar el suyo. Si vuelven a
    divergir, la cola y las alertas se desalinean otra vez."""
    from inventario.ubicaciones import SUFIJO_HEREDADA

    assert SUFIJO_HEREDADA == SUFIJO_CLASE_HEREDADA
