"""Marca de bodega en la cabecera SQLite y respaldos por bodega (DEC-163).

`PRAGMA application_id`: 1 = miempresa, 2 = bodega2, 0 = sin marca. Una
base sin marca y vacía es nueva; sin marca y con tablas es la histórica,
válida solo para la bodega por defecto. Toda base va en `tmp_path`: estos
tests nunca abren `data/` real.
"""

import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

import comun
from comun import (
    BODEGA_DEFECTO,
    MARCA_BODEGA,
    BodegaIncorrectaError,
    bodega_de_la_base,
    get_db_path,
    marcar_bodega,
    verificar_bodega,
    verificar_bodega_db,
)
from scraper.scraper_principal import init_db

pytestmark = pytest.mark.integration

_RUTA_RESPALDO = Path(__file__).resolve().parents[2] / "scripts" / "respaldar_db.py"
_spec = importlib.util.spec_from_file_location("respaldar_db_bodegas", _RUTA_RESPALDO)
assert _spec and _spec.loader
respaldo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(respaldo)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))
import tareas_db  # noqa: E402


def _marca(ruta: Path) -> int:
    con = sqlite3.connect(ruta)
    try:
        return con.execute("PRAGMA application_id").fetchone()[0]
    finally:
        con.close()


def _base_historica(ruta: Path) -> Path:
    """Base con datos y sin marca, como la de la bodega por defecto hoy."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY)")
    con.execute("INSERT INTO pedidos VALUES ('TEST-1')")
    con.commit()
    con.close()
    return ruta


def _base_marcada(ruta: Path, bodega: str) -> Path:
    """Base con datos y su marca, puesta antes del esquema como en init_db."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    marcar_bodega(con, bodega)
    con.close()
    return _base_historica(ruta)


@pytest.fixture
def sin_bodega(monkeypatch):
    monkeypatch.delenv("BODEGA", raising=False)


# ─────────────────────────────────────────────
# Marca
# ─────────────────────────────────────────────


@pytest.mark.parametrize("bodega", ["miempresa", "bodega2"])
def test_base_nueva_se_marca_con_su_bodega(tmp_path, bodega):
    ruta = tmp_path / "nueva.db"
    con = sqlite3.connect(ruta)
    marcar_bodega(con, bodega)
    assert bodega_de_la_base(con) == bodega
    marcar_bodega(con, bodega)  # idempotente
    con.close()
    assert _marca(ruta) == MARCA_BODEGA[bodega]


def test_base_historica_sin_marca_es_de_la_bodega_por_defecto(tmp_path):
    ruta = _base_historica(tmp_path / "pedidos.db")
    con = sqlite3.connect(ruta)
    verificar_bodega(con, BODEGA_DEFECTO)  # pasa, sin escribir
    assert bodega_de_la_base(con) is None
    with pytest.raises(BodegaIncorrectaError, match="no tiene marca"):
        verificar_bodega(con, "bodega2")
    with pytest.raises(BodegaIncorrectaError):
        marcar_bodega(con, "bodega2")
    assert bodega_de_la_base(con) is None  # el rechazo no la marcó
    marcar_bodega(con, BODEGA_DEFECTO)
    con.close()
    assert _marca(ruta) == 1
    # La marca no tocó los datos.
    con = sqlite3.connect(ruta)
    assert con.execute("SELECT id_pedido FROM pedidos").fetchall() == [("TEST-1",)]
    con.close()


@pytest.mark.parametrize("marcada,proceso", [("bodega2", "miempresa"), ("miempresa", "bodega2")])
def test_base_de_otra_bodega_detiene_el_proceso(tmp_path, marcada, proceso):
    ruta = _base_marcada(tmp_path / "pedidos.db", marcada)
    with pytest.raises(BodegaIncorrectaError, match=f"es de {marcada}"):
        verificar_bodega_db(ruta, proceso)
    con = sqlite3.connect(ruta)
    with pytest.raises(BodegaIncorrectaError):
        marcar_bodega(con, proceso)
    con.close()
    assert _marca(ruta) == MARCA_BODEGA[marcada]


def test_marca_desconocida_detiene_el_proceso(tmp_path):
    ruta = tmp_path / "ajena.db"
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA application_id = 99")
    with pytest.raises(BodegaIncorrectaError, match="desconocida"):
        verificar_bodega(con, BODEGA_DEFECTO)
    con.close()


def test_la_bodega_del_proceso_sale_del_entorno(tmp_path, monkeypatch):
    ruta = _base_marcada(tmp_path / "pedidos.db", "bodega2")
    monkeypatch.setenv("BODEGA", "bodega2")
    verificar_bodega_db(ruta)
    monkeypatch.delenv("BODEGA")
    with pytest.raises(BodegaIncorrectaError):
        verificar_bodega_db(ruta)


# ─────────────────────────────────────────────
# get_db_path()
# ─────────────────────────────────────────────


def test_get_db_path_de_otra_bodega_crea_la_base_ya_marcada(tmp_path, monkeypatch):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    ruta = Path(get_db_path("bodega2"))
    assert ruta == tmp_path / "data" / "bodega2" / "pedidos.db"
    assert _marca(ruta) == 2
    assert get_db_path("bodega2") == str(ruta)  # existente: solo verifica


def test_get_db_path_por_defecto_no_crea_la_base(tmp_path, monkeypatch, sin_bodega):
    """Como antes de DEC-163: crea data/, no el archivo."""
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    ruta = Path(get_db_path())
    assert ruta == tmp_path / "data" / "pedidos.db"
    assert ruta.parent.is_dir()
    assert not ruta.exists()


def test_get_db_path_verifica_sin_escribir_en_la_base_historica(tmp_path, monkeypatch, sin_bodega):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    ruta = _base_historica(tmp_path / "data" / "pedidos.db")
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    antes = ruta.read_bytes()
    assert get_db_path() == str(ruta)
    assert ruta.read_bytes() == antes  # solo lectura


def test_get_db_path_rechaza_una_base_de_otra_bodega(tmp_path, monkeypatch, sin_bodega):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    _base_marcada(tmp_path / "data" / "pedidos.db", "bodega2")
    with pytest.raises(BodegaIncorrectaError):
        get_db_path()


# ─────────────────────────────────────────────
# Escritores que crean el esquema
# ─────────────────────────────────────────────


async def test_init_db_marca_antes_de_crear_el_esquema(tmp_path, sin_bodega):
    ruta = tmp_path / "pedidos.db"
    await init_db(str(ruta))
    assert _marca(ruta) == 1

    ajena = tmp_path / "ajena.db"
    con = sqlite3.connect(ajena)
    marcar_bodega(con, "bodega2")
    con.close()
    with pytest.raises(BodegaIncorrectaError):
        await init_db(str(ajena))
    con = sqlite3.connect(ajena)
    tablas = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    con.close()
    assert tablas == 0  # rechazada sin tocarla


async def test_init_db_marca_la_base_historica_sin_tocar_sus_datos(tmp_path, sin_bodega):
    # Base con el esquema real y datos, sin marca: como la de producción hoy.
    ruta = tmp_path / "pedidos.db"
    await init_db(str(ruta))
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA application_id = 0")
    con.execute("INSERT INTO pedidos (id_pedido) VALUES ('TEST-1')")
    con.commit()
    con.close()
    assert _marca(ruta) == 0

    await init_db(str(ruta))
    assert _marca(ruta) == 1
    con = sqlite3.connect(ruta)
    assert con.execute("SELECT id_pedido FROM pedidos").fetchall() == [("TEST-1",)]
    con.close()


def test_tareas_db_marca_y_rechaza(tmp_path, monkeypatch, sin_bodega):
    ruta = tmp_path / "tareas.db"
    monkeypatch.setattr(tareas_db, "DB_PATH", ruta)
    tareas_db.init_db()
    assert _marca(ruta) == 1

    ajena = tmp_path / "ajena_tareas.db"
    con = sqlite3.connect(ajena)
    marcar_bodega(con, "bodega2")
    con.close()
    monkeypatch.setattr(tareas_db, "DB_PATH", ajena)
    with pytest.raises(BodegaIncorrectaError):
        tareas_db.init_db()


def test_dashboard_no_lee_la_base_de_otra_bodega(tmp_path, monkeypatch, sin_bodega):
    import dashboard.db as ddb

    monkeypatch.setattr(ddb, "DB_PATH", _base_marcada(tmp_path / "pedidos.db", "bodega2"))
    with pytest.raises(BodegaIncorrectaError):
        ddb._conn()
    monkeypatch.setattr(ddb, "DB_PATH", _base_historica(tmp_path / "historica.db"))
    ddb._conn().close()


# ─────────────────────────────────────────────
# Respaldos por bodega
# ─────────────────────────────────────────────


def test_el_respaldo_conserva_la_marca(tmp_path):
    origen = _base_marcada(tmp_path / "pedidos.db", "bodega2")
    nuevo = respaldo.respaldar(origen, tmp_path / "backups", date(2026, 10, 21))
    assert nuevo is not None
    assert _marca(nuevo) == 2


def test_respaldo_y_purga_de_una_bodega_no_tocan_la_otra(tmp_path, monkeypatch):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    monkeypatch.setenv("RESPALDO_DIAS", "2")
    _base_marcada(tmp_path / "data" / "pedidos.db", "miempresa")
    _base_marcada(tmp_path / "data" / "bodega2" / "pedidos.db", "bodega2")
    viejos = ("pedidos_2026-01-01.db", "pedidos_2026-01-02.db", "pedidos_2026-01-03.db")
    for carpeta in (tmp_path / "data" / "backups", tmp_path / "data" / "bodega2" / "backups"):
        carpeta.mkdir(parents=True)
        for nombre in viejos:
            (carpeta / nombre).write_bytes(b"x")
    antes = sorted(p.name for p in (tmp_path / "data" / "backups").iterdir())

    monkeypatch.setenv("BODEGA", "bodega2")
    assert respaldo.main() == 0

    hoy = date.today().isoformat()
    propios = sorted(p.name for p in (tmp_path / "data" / "bodega2" / "backups").iterdir())
    assert propios == ["pedidos_2026-01-03.db", f"pedidos_{hoy}.db"]  # purgada a 2
    assert sorted(p.name for p in (tmp_path / "data" / "backups").iterdir()) == antes


def test_respaldo_se_niega_a_copiar_una_base_ajena(tmp_path, monkeypatch):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    # Base con la marca de la otra bodega en la carpeta de bodega2.
    _base_marcada(tmp_path / "data" / "bodega2" / "pedidos.db", "miempresa")
    monkeypatch.setenv("BODEGA", "bodega2")
    assert respaldo.main() == 1
    assert not (tmp_path / "data" / "bodega2" / "backups").exists()
