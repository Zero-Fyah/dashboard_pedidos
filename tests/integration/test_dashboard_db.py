"""AUD-M6 (defensa secundaria): dashboard/db.py debe abrir sus conexiones
con timeout=5 en vez del default de sqlite3 (0s, falla de inmediato ante
"database is locked"). La causa raíz de la ventana "no such view" ya
quedó resuelta en el ETL (DEC-019) — esto solo cubre contención residual
con el ETL/scraper escribiendo al mismo tiempo.
"""

import sqlite3

import pytest

import dashboard.db as ddb

# Referencia capturada antes de cualquier monkeypatch — el espía la usa
# para abrir la conexión real sin recursar sobre sí mismo (ddb.sqlite3 es
# el mismo objeto módulo que este `sqlite3`, así que parchear
# ddb.sqlite3.connect también reemplaza sqlite3.connect globalmente).
_CONNECT_REAL = sqlite3.connect


def _espiar_connect(monkeypatch):
    """Parchea ddb.sqlite3.connect y devuelve el dict de kwargs capturados."""
    llamadas: dict = {}

    def _connect_espia(path, **kwargs):
        llamadas.update(kwargs)
        return _CONNECT_REAL(path, **kwargs)

    monkeypatch.setattr(ddb.sqlite3, "connect", _connect_espia)
    return llamadas


@pytest.mark.integration
def test_conn_pasa_timeout_5(monkeypatch, tmp_path):
    db_file = tmp_path / "existe.db"
    db_file.touch()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    llamadas = _espiar_connect(monkeypatch)

    con = ddb._conn()
    con.close()

    assert llamadas.get("timeout") == 5


@pytest.mark.integration
def test_conn_sigue_lanzando_filenotfound_si_no_existe_la_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "no_existe.db")
    with pytest.raises(FileNotFoundError):
        ddb._conn()


@pytest.mark.integration
def test_num_cols_exist_pasa_timeout_5(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "cols.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE lineas_pedido (monto_pagar_num REAL)")
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    llamadas = _espiar_connect(monkeypatch)

    assert ddb._num_cols_exist() is True
    assert llamadas.get("timeout") == 5
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_view_consolidado_exists_pasa_timeout_5(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "view.db"
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    db_file.touch()
    llamadas = _espiar_connect(monkeypatch)

    assert ddb._view_consolidado_exists() is False
    assert llamadas.get("timeout") == 5
    ddb.st.cache_data.clear()


# ── AUD-M12: TTL único + indicador de frescura ──────────────────────────────


@pytest.mark.integration
def test_ttl_unico_y_razonable():
    """AUD-M12: un solo TTL para todo el módulo (antes 7200s en queries y
    300s en checks de esquema, inconsistente) y lo bastante corto para no
    volver a acumular horas de datos viejos con el scheduler cada 1h."""
    assert ddb._CACHE_TTL_S <= 1200


@pytest.mark.integration
def test_ultima_actualizacion_devuelve_max(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "fresco.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE pedidos (id_pedido TEXT, actualizado_en TEXT)")
    con_setup.executemany(
        "INSERT INTO pedidos VALUES (?, ?)",
        [("P1", "2026-07-22T10:00:00+00:00"), ("P2", "2026-07-22T12:00:00+00:00")],
    )
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    assert ddb.get_ultima_actualizacion() == "2026-07-22T12:00:00+00:00"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_ultima_actualizacion_none_si_tabla_vacia(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "vacio.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE pedidos (id_pedido TEXT, actualizado_en TEXT)")
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    assert ddb.get_ultima_actualizacion() is None
    ddb.st.cache_data.clear()
