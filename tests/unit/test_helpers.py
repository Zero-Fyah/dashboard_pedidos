import pytest
from scraper.scraper_principal import (
    CONFIG,
    to_num,
    get_db_path,
    build_arg_parser,
)


@pytest.mark.unit
def test_argparse_default_desde():
    """BUG-002: el default de --desde debe ser 2026-05-01."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.desde == "2026-05-01"


@pytest.mark.unit
def test_argparse_no_expone_nombre_empresa():
    """BUG-008: la descripción no expone el nombre real."""
    parser = build_arg_parser()
    desc = parser.description.lower()
    assert "calabaza" not in desc
    assert "pets" not in desc


@pytest.mark.unit
def test_config_headless_es_bool():
    """BUG-001: HEADLESS presente en CONFIG como bool."""
    assert "HEADLESS" in CONFIG
    assert isinstance(CONFIG["HEADLESS"], bool)


@pytest.mark.unit
def test_config_slow_mo_es_int():
    """BUG-001: SLOW_MO presente en CONFIG como int."""
    assert "SLOW_MO" in CONFIG
    assert isinstance(CONFIG["SLOW_MO"], int)


@pytest.mark.unit
def test_db_path_contiene_data_pedidos():
    """BUG-007: la ruta apunta a data/pedidos.db.
    Nota: get_db_path() crea data/ como efecto secundario.
    Esto es intencional — data/ debe existir en el proyecto.
    """
    path = get_db_path()
    assert "data" in path
    assert path.endswith("pedidos.db")


@pytest.mark.unit
def test_db_path_es_absoluto():
    """BUG-007: la ruta es absoluta, no relativa al cwd."""
    from pathlib import Path
    path = get_db_path()
    assert Path(path).is_absolute()


@pytest.mark.parametrize("entrada,esperado", [
    ("1.234,56",   1234.56),
    ("200",        200.0),
    ("0,50",       0.5),
    (",50",        0.5),
    ("-1.000,00",  -1000.0),
    (" 1.234,56 ", 1234.56),
    ("",           None),
    ("N/A",        None),
    ("—",          None),
    ("None",       None),
])
@pytest.mark.unit
def test_to_num(entrada, esperado):
    assert to_num(entrada) == esperado
