import logging
from logging.handlers import TimedRotatingFileHandler

import pytest
import pytest_asyncio

from scraper.config import CONFIG
from scraper.scraper_principal import init_db


# Auditoría 2026-09-25: la suite escribía en las salidas de PRODUCCIÓN —
# `logs/scraper.log` (cientos de ERROR falsos por corrida: `db_error`,
# `worker_excepcion_no_controlada`… con IDs `TEST-`/`SUB-001`, que
# contaminaban la telemetría) y `data/debug/`, donde cada archivo de un test
# desplazaba de la rotación (tope de 50) un HTML real de diagnóstico.
# Se redirigen a un directorio temporal para toda la sesión. El reemplazo
# es otro TimedRotatingFileHandler para que el guard N-4 (un solo handler
# de ese tipo, `test_logger_tiene_un_solo_handler`) siga midiendo lo mismo.
@pytest.fixture(autouse=True, scope="session")
def _aislar_salidas_del_scraper(tmp_path_factory):
    salidas = tmp_path_factory.mktemp("salidas_scraper")
    logger = logging.getLogger("scraper.config")
    originales = [h for h in logger.handlers if type(h) is TimedRotatingFileHandler]
    for h in originales:
        logger.removeHandler(h)
    aislado = TimedRotatingFileHandler(salidas / "scraper.log", when="midnight", encoding="utf-8")
    aislado.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(aislado)
    mp = pytest.MonkeyPatch()
    mp.setitem(CONFIG, "LOG_FILE", str(salidas / "scraper.log"))
    mp.setitem(CONFIG, "ERRORS_DIR", str(salidas / "errors"))
    mp.setitem(CONFIG, "DEBUG_DIR", str(salidas / "debug"))
    yield salidas
    mp.undo()
    logger.removeHandler(aislado)
    aislado.close()
    for h in originales:
        logger.addHandler(h)


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
