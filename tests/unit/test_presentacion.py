"""Tests de `_unir_presentacion` — DEC-026.

La columna de presentación de una línea une todos los `<span>` de
`.goods-specs`. Migrados en la auditoría de tests del 2026-09-24: antes
probaban `leer_presentacion()` (versión por ElementHandle), que quedó sin
uso con el batch de DEC-030 Fase 3 y se retiró. La preocupación sigue viva
en `_unir_presentacion`, la única implementación que corre hoy.
"""

import pytest

from scraper.extractores import _unir_presentacion

pytestmark = pytest.mark.unit


def test_un_solo_atributo():
    assert _unir_presentacion(["Presentacion: PRA13, CLASICA, 4.5KG"]) == (
        "Presentacion: PRA13, CLASICA, 4.5KG"
    )


def test_dos_atributos_se_unen_con_barra():
    assert _unir_presentacion(["Tamaño: L- 35-50cm", "Color: Rosado"]) == (
        "Tamaño: L- 35-50cm | Color: Rosado"
    )


def test_span_vacio_se_omite():
    assert _unir_presentacion(["Tamaño: M", ""]) == "Tamaño: M"


def test_sin_specs():
    assert _unir_presentacion([]) == ""
