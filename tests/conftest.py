import pytest_asyncio
from scraper.scraper_principal import init_db


@pytest_asyncio.fixture
async def db_path(tmp_path):
    path = str(tmp_path / "test_pedidos.db")
    await init_db(path)
    return path
