"""Tests de comun.clasificar_modalidad_arena() (DEC-118).

Cubre los 5 grupos verificados contra `admin_inventario.xlsx`, el esquema
de referencias anterior a la migración nacional (solo aparece en
`lineas_pedido` de pedidos viejos), las exclusiones conocidas y el caso de
una referencia nueva no reconocida.
"""

import pytest

from comun import (
    MODALIDAD_CORPORATIVO,
    MODALIDAD_RESPALDO,
    MODALIDAD_TONELADA,
    MODALIDAD_UNIDADES,
    MODALIDAD_YUMBO_HUB,
    clasificar_modalidad_arena,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("referencia", "esperado"),
    [
        ("PRA13", MODALIDAD_UNIDADES),
        ("PRA78", MODALIDAD_UNIDADES),
        ("PRA ARENA TONELADA", MODALIDAD_TONELADA),
        ("ARENA TONELADA CORPORATIVO BOGOTA", MODALIDAD_CORPORATIVO),
        ("ARENA TONELADA CORPORATIVO  PEREIRA", MODALIDAD_CORPORATIVO),  # doble espacio real
        ("ARENA AVERIA BOGOTA", MODALIDAD_RESPALDO),
        ("ARENA AVERIA MEDELLIN", MODALIDAD_RESPALDO),
        ("YUMBO TONELADA", MODALIDAD_YUMBO_HUB),
        ("YUMBO EN TRANSITO", MODALIDAD_YUMBO_HUB),
    ],
)
def test_referencias_conocidas(referencia, esperado):
    assert clasificar_modalidad_arena(referencia) == esperado


@pytest.mark.parametrize("referencia", ["PRA-T01", "PRA-001", "PB008", "PS0199"])
def test_excluidas_devuelven_none(referencia):
    assert clasificar_modalidad_arena(referencia) is None


@pytest.mark.parametrize("referencia", [None, "", "  "])
def test_vacio_devuelve_none(referencia):
    assert clasificar_modalidad_arena(referencia) is None


def test_referencia_no_reconocida_devuelve_none():
    """Una modalidad/ciudad nueva no debe adivinarse — el detector de
    hallazgos (`inventario.hallazgos.arena_referencias_no_reconocidas`) es
    quien avisa que hace falta revisarla."""
    assert clasificar_modalidad_arena("ARENA SABOR NUEVO CARTAGENA") is None


@pytest.mark.parametrize(
    ("referencia", "esperado"),
    [
        ("BOGOTA TONELADA", MODALIDAD_TONELADA),
        ("CALI TONELADA", MODALIDAD_TONELADA),
        ("BOGOTA UNIDADES", MODALIDAD_UNIDADES),
        ("CALI UNIDADES", MODALIDAD_UNIDADES),
    ],
)
def test_esquema_de_referencias_anterior_a_la_migracion(referencia, esperado):
    """Ya no existe en `admin_inventario.xlsx`, pero sí en `lineas_pedido` de
    pedidos viejos — necesario para medir el balance histórico completo
    (DEC-118: sin esta regla, 22,4% de las ventas de Arena quedaban sin
    clasificar)."""
    assert clasificar_modalidad_arena(referencia) == esperado
