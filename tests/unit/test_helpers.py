import pytest

from scraper.scraper_principal import (
    CONFIG,
    build_arg_parser,
    get_db_path,
    to_num,
)


@pytest.mark.unit
def test_argparse_default_desde():
    """BUG-002: el default de --desde debe ser 2026-01-01."""
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.desde == "2026-01-01"


# Literales prohibidos construidos por fragmentos: el nombre real no debe
# existir en texto plano en ningún archivo del repo (DEC-017), incluido este.
_NOMBRE_PROHIBIDO = "".join(("cala", "baza"))
_SUFIJO_PROHIBIDO = "".join(("pe", "ts"))


@pytest.mark.unit
def test_argparse_no_expone_nombre_empresa():
    """BUG-008: la descripción no expone el nombre real."""
    parser = build_arg_parser()
    desc = parser.description.lower()
    assert _NOMBRE_PROHIBIDO not in desc
    assert _SUFIJO_PROHIBIDO not in desc


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
def test_db_path_contiene_data_pedidos(tmp_path, monkeypatch):
    """BUG-007: la ruta apunta a data/pedidos.db.
    Nota: get_db_path() crea data/ como efecto secundario.
    Esto es intencional — data/ debe existir en el proyecto.

    DEC-163: get_db_path() verifica la marca de bodega de una base que ya
    existe, así que la raíz va a un temporal — la suite nunca abre la base
    de producción, ni en solo lectura (mismo criterio que DEC-145).
    """
    import comun

    monkeypatch.delenv("BODEGA", raising=False)
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    path = get_db_path()
    assert path == str(tmp_path / "data" / "pedidos.db")
    assert (tmp_path / "data").is_dir()


@pytest.mark.unit
def test_db_path_es_absoluto():
    """BUG-007: la ruta es absoluta, no relativa al cwd.

    DEC-163: get_db_path() la toma de ruta_bodega(), que se mide sobre la
    raíz real sin abrir ninguna base.
    """
    from pathlib import Path

    from comun import ruta_bodega

    assert Path(ruta_bodega("pedidos.db", "miempresa")).is_absolute()
    assert Path(ruta_bodega("pedidos.db", "bodega2")).is_absolute()


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("1.234,56", 1234.56),
        ("200", 200.0),
        ("0,50", 0.5),
        (",50", 0.5),
        ("-1.000,00", -1000.0),
        (" 1.234,56 ", 1234.56),
        ("", None),
        ("N/A", None),
        ("—", None),
        ("None", None),
    ],
)
@pytest.mark.unit
def test_to_num(entrada, esperado):
    assert to_num(entrada) == esperado
