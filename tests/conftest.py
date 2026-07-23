import pytest
import pytest_asyncio

from scraper.scraper_principal import init_db


@pytest_asyncio.fixture
async def db_path(tmp_path):
    path = str(tmp_path / "test_pedidos.db")
    await init_db(path)
    return path


# DEC-038: gate opt-in para tests E2E (browser real) — diseñado en
# docs/testing.md desde 2026-05-22 pero nunca implementado. Sin --e2e,
# cualquier test marcado "e2e" se salta; cierra la brecha de CI (que
# corre `pytest -q` sin filtro -m) para el primer test E2E que se agregue.
def pytest_addoption(parser):
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="Ejecutar tests E2E con browser real",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--e2e"):
        skip_e2e = pytest.mark.skip(reason="Usar --e2e para ejecutar tests de browser")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_e2e)
