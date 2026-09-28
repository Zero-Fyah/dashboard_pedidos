"""Respaldo diario de las bases SQLite (auditoría 2026-09-25, DEC-146)."""

import importlib.util
import sqlite3
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_RUTA = Path(__file__).resolve().parents[2] / "scripts" / "respaldar_db.py"
_spec = importlib.util.spec_from_file_location("respaldar_db", _RUTA)
assert _spec and _spec.loader
respaldo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(respaldo)

HOY = date(2026, 9, 25)


def _base(ruta: Path, filas: int = 3) -> Path:
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"v{i}",) for i in range(filas)])
    con.commit()
    con.close()
    return ruta


def test_respaldo_copia_consistente_con_wal_y_nombre_del_dia(tmp_path):
    origen = _base(tmp_path / "pedidos.db", filas=50)
    # Escritor con cambios todavía en el WAL (sin checkpoint): la copia debe
    # incluirlos igual, que es lo que garantiza la API de respaldo.
    escritor = sqlite3.connect(origen)
    escritor.execute("INSERT INTO t (v) VALUES ('en_wal')")
    escritor.commit()

    nuevo = respaldo.respaldar(origen, tmp_path / "backups", HOY)
    escritor.close()

    assert nuevo is not None
    assert nuevo.name == "pedidos_2026-09-25.db"
    con = sqlite3.connect(nuevo)
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 51
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # archivo único
    con.close()
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_respaldo_ya_hecho_hoy_no_se_repite(tmp_path):
    origen = _base(tmp_path / "pedidos.db")
    destino = tmp_path / "backups"
    primero = respaldo.respaldar(origen, destino, HOY)
    assert primero is not None
    mtime = primero.stat().st_mtime_ns

    assert respaldo.respaldar(origen, destino, HOY) is None
    assert primero.stat().st_mtime_ns == mtime


def test_copia_que_no_pasa_quick_check_se_descarta(tmp_path, monkeypatch):
    origen = _base(tmp_path / "pedidos.db")
    destino = tmp_path / "backups"

    monkeypatch.setattr(respaldo, "_verificar", lambda ruta: "page 3: btree corrupta")

    with pytest.raises(RuntimeError, match="quick_check"):
        respaldo.respaldar(origen, destino, HOY)
    assert list(destino.iterdir()) == []  # ni el temporal ni un respaldo "válido"


def test_purga_conserva_los_ultimos_y_no_toca_respaldos_manuales(tmp_path):
    for dia in range(18, 26):  # 8 respaldos automáticos
        (tmp_path / f"pedidos_2026-09-{dia:02d}.db").write_bytes(b"x")
    manuales = [
        tmp_path / "pedidos_pre-reindex_2026-09-01_215604.db",
        tmp_path / "pedidos_pre_DEC140_2026-09-23_2209.db",
        tmp_path / "tareas_2026-09-10.db",  # otra base: su propia retención
    ]
    for m in manuales:
        m.write_bytes(b"x")

    borrados = respaldo.purgar(tmp_path, "pedidos", conservar=7)

    assert [p.name for p in borrados] == ["pedidos_2026-09-18.db"]
    assert all(m.exists() for m in manuales)
    assert len(list(tmp_path.glob("pedidos_2026-09-*.db"))) == 7


def test_retencion_desde_el_entorno(monkeypatch):
    monkeypatch.setenv("RESPALDO_DIAS", "3")
    assert respaldo._dias_retencion() == 3
    monkeypatch.setenv("RESPALDO_DIAS", "abc")
    assert respaldo._dias_retencion() == 7
    monkeypatch.setenv("RESPALDO_DIAS", "0")
    assert respaldo._dias_retencion() == 1  # nunca borra todo


def test_main_respalda_ambas_bases_y_reporta_fallo(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    _base(tmp_path / "data" / "pedidos.db")
    _base(tmp_path / "data" / "tareas.db")
    monkeypatch.setattr(respaldo, "RAIZ", tmp_path)

    assert respaldo.main() == 0
    hechos = sorted(p.name for p in (tmp_path / "data" / "backups").iterdir())
    assert hechos == [
        f"pedidos_{date.today().isoformat()}.db",
        f"tareas_{date.today().isoformat()}.db",
    ]

    def _falla(*a, **kw):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(respaldo, "respaldar", _falla)
    assert respaldo.main() == 1
