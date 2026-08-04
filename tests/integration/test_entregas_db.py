"""`get_entregas()` sobre una base sintética — el contrato de la consulta (DEC-095).

Los tests de `comun/entregas.py` cubren las reglas; estos cubren el SQL: que el
`LEFT JOIN` conserve al pedido prometido y no entregado —el caso accionable—,
que `MIN(momento)` elija el primer intento cuando hay varios, y que los
contadores no se dupliquen al unir tres subconsultas.
"""

import sqlite3

import pytest

import dashboard.db as ddb


def _base(tmp_path):
    """Base mínima con el esquema que toca la consulta."""
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, metodo_entrega TEXT,
            forma_pago TEXT, nombre_empresa TEXT, vendedor TEXT, hora_entrega TEXT,
            despachador TEXT, scraping_completo INTEGER
        );
        CREATE TABLE registro_operaciones (
            id INTEGER PRIMARY KEY, id_pedido TEXT, momento TEXT,
            usuario TEXT, tipo_usuario TEXT, accion TEXT, referencia TEXT
        );
        CREATE TABLE estadisticas_monto (
            id INTEGER PRIMARY KEY, id_pedido TEXT, concepto_base TEXT,
            monto_final_num REAL
        );
        """
    )
    con.commit()
    return ruta, con


def _pedido(con, pid, hora_entrega="2026-08-01 08:00 ~ 09:00", fecha="2026-08-01"):
    con.execute(
        "INSERT INTO pedidos VALUES (?,?,'Ruta','Pago inmediato','ACME','ANA',?,'JUAN',1)",
        (pid, fecha, hora_entrega),
    )


def _evento(con, pid, accion, momento):
    con.execute(
        "INSERT INTO registro_operaciones (id_pedido, momento, usuario, tipo_usuario,"
        " accion, referencia) VALUES (?,?,'sys','system',?,'')",
        (pid, momento, accion),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    ruta, con = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    ddb.get_entregas.clear()
    yield con
    con.close()


@pytest.mark.integration
def test_conserva_el_pedido_prometido_y_no_entregado(db):
    """El caso accionable: un INNER JOIN lo borraría justo a él."""
    _pedido(db, "TEST-1")
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert df.iloc[0]["id_pedido"] == "TEST-1"
    assert df.iloc[0]["entregado_en"] is None


@pytest.mark.integration
def test_toma_el_primer_evento_de_entrega(db):
    """1.685 pedidos reales tienen varios: se mide cuándo llegó, no cuántos intentos."""
    _pedido(db, "TEST-2")
    _evento(db, "TEST-2", "Entrega", "2026-08-05 10:00:00")
    _evento(db, "TEST-2", "Entrega", "2026-08-03 09:00:00")
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert df.iloc[0]["entregado_en"] == "2026-08-03 09:00:00"


@pytest.mark.integration
def test_cuenta_las_reprogramaciones_sin_duplicar_filas(db):
    """Tres subconsultas unidas no pueden multiplicar el pedido."""
    _pedido(db, "TEST-3")
    for m in ("2026-07-30 08:00:00", "2026-07-31 08:00:00", "2026-08-01 08:00:00"):
        _evento(db, "TEST-3", "Actualizar hora de entrega", m)
    _evento(db, "TEST-3", "Entrega", "2026-08-02 09:00:00")
    db.execute(
        "INSERT INTO estadisticas_monto (id_pedido, concepto_base, monto_final_num)"
        " VALUES ('TEST-3','Total a pagar / Total final a pagar', 5000.0)"
    )
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert len(df) == 1, "el join duplicó el pedido"
    assert int(df.iloc[0]["reprogramaciones"]) == 3
    assert df.iloc[0]["valor"] == 5000.0


@pytest.mark.integration
def test_pedido_sin_monto_no_desaparece(db):
    """El valor es contexto, no un requisito: sin él la fila igual tiene que estar."""
    _pedido(db, "TEST-4")
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert df.iloc[0]["valor"] == 0


@pytest.mark.integration
def test_respeta_el_rango_de_fechas_y_scraping_completo(db):
    _pedido(db, "TEST-5", fecha="2026-03-01")
    _pedido(db, "TEST-6", fecha="2026-08-01")
    db.execute("UPDATE pedidos SET scraping_completo = 0 WHERE id_pedido = 'TEST-6'")
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert list(df["id_pedido"]) == ["TEST-5"], "no filtró por scraping_completo"


@pytest.mark.integration
def test_otras_acciones_no_cuentan_como_entrega(db):
    """`Cancelar entrega` y `Pedido enviado` no son la entrega."""
    _pedido(db, "TEST-7")
    _evento(db, "TEST-7", "Pedido enviado", "2026-08-01 07:00:00")
    _evento(db, "TEST-7", "Cancelar entrega", "2026-08-01 12:00:00")
    db.commit()

    df = ddb.get_entregas("2026-01-01", "2026-12-31")

    assert df.iloc[0]["entregado_en"] is None
