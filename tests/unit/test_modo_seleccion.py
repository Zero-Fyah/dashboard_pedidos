import pytest
from scraper.scraper_principal import determinar_modo


@pytest.mark.unit
def test_modo_pedido_nuevo_sin_subs():
    """Pedido nuevo siempre es completo."""
    assert determinar_modo(True, []) == "completo"


@pytest.mark.unit
def test_modo_pedido_nuevo_ignora_estado_subs():
    """Pedido nuevo es completo sin importar el estado de sus subs."""
    assert determinar_modo(True, [("completado", 0)]) == "completo"


@pytest.mark.unit
def test_modo_con_cantidades_estado_completado():
    """BUG-005 opción B: completado con cd=0 activa con_cantidades."""
    assert determinar_modo(False, [("completado", 0)]) == "con_cantidades"


@pytest.mark.unit
def test_modo_con_cantidades_estado_cancelado():
    assert determinar_modo(False, [("cancelado", 0)]) == "con_cantidades"


@pytest.mark.unit
def test_modo_con_cantidades_estado_comentado():
    assert determinar_modo(False, [("comentado", 0)]) == "con_cantidades"


@pytest.mark.unit
def test_modo_solo_estado_intermedio_sin_cantidades():
    """BUG-005 opción B: estados intermedios NO activan con_cantidades."""
    assert determinar_modo(False, [("enviado", 0)]) == "solo_estado"
    assert determinar_modo(False, [("pendiente de entrega", 0)]) == "solo_estado"
    assert determinar_modo(False, [("período contable", 0)]) == "solo_estado"


@pytest.mark.unit
def test_modo_solo_estado_cantidades_ya_definitivas():
    """Sub cerrado con cantidades ya registradas → solo_estado."""
    assert determinar_modo(False, [("completado", 1)]) == "solo_estado"


@pytest.mark.unit
def test_modo_con_cantidades_mixto():
    """Un sub cerrado sin cantidades + otro ya definitivo."""
    subs = [("completado", 0), ("cancelado", 1)]
    assert determinar_modo(False, subs) == "con_cantidades"


@pytest.mark.unit
def test_modo_solo_estado_todos_definitivos():
    """Todos los subs con cantidades definitivas → solo_estado."""
    subs = [("completado", 1), ("cancelado", 1)]
    assert determinar_modo(False, subs) == "solo_estado"
