"""`get_ciclo_pedidos()` y `get_ciclo_transiciones()` sobre base sintética (DEC-096).

Lo que estos tests protegen es la propiedad que hace útil la página: **el último
paso del timeline es el estado actual del pedido**. Si la consulta eligiera otro
paso —el de mayor fecha en vez del de mayor `paso`, o el primero— la página
mostraría pedidos parados en etapas por las que ya pasaron, sin que nada fallara.
"""

import sqlite3

import pytest

import dashboard.db as ddb


def _base(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, metodo_entrega TEXT,
            nombre_empresa TEXT, vendedor TEXT, forma_pago TEXT, scraping_completo INTEGER
        );
        CREATE TABLE timeline_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, paso INTEGER,
            titulo TEXT, fecha_hora TEXT, completado INTEGER
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE estadisticas_monto (
            id INTEGER PRIMARY KEY, id_pedido TEXT, concepto_base TEXT, monto_final_num REAL
        );
        """
    )
    con.commit()
    return ruta, con


def _pedido(con, pid, fecha="2026-08-01"):
    con.execute(
        "INSERT INTO pedidos VALUES (?,?,'Ruta','ACME','ANA','Pago inmediato',1)", (pid, fecha)
    )


def _paso(con, pid, paso, titulo, fecha_hora):
    con.execute(
        "INSERT INTO timeline_pedido (id_pedido, paso, titulo, fecha_hora, completado)"
        " VALUES (?,?,?,?,1)",
        (pid, paso, titulo, fecha_hora),
    )


def _sub(con, pid, estado):
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES (?,'1',?)",
        (pid, estado),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    ruta, con = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    ddb.get_ciclo_pedidos.clear()
    ddb.get_ciclo_transiciones.clear()
    yield con
    con.close()


@pytest.mark.integration
def test_el_estado_actual_es_el_ultimo_paso(db):
    """Si tomara el primero o el de menor paso, diría que está en «confirmando»."""
    _pedido(db, "TEST-1")
    _paso(db, "TEST-1", 1, "confirmando", "2026-08-01 08:00:00")
    _paso(db, "TEST-1", 2, "Alistamiento", "2026-08-02 08:00:00")
    _paso(db, "TEST-1", 3, "Listo para enviar", "2026-08-03 08:00:00")
    _sub(db, "TEST-1", "pendiente de entrega")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert len(df) == 1
    fila = df.iloc[0]
    assert fila["estado_actual"] == "Listo para enviar"
    assert fila["ultimo_evento"] == "2026-08-03 08:00:00"
    assert fila["inicio"] == "2026-08-01 08:00:00"
    assert int(fila["pasos"]) == 3


@pytest.mark.integration
def test_manda_el_numero_de_paso_no_la_fecha(db):
    """El origen es la autoridad sobre el orden; una fecha desordenada no lo cambia."""
    _pedido(db, "TEST-2")
    _paso(db, "TEST-2", 1, "confirmando", "2026-08-05 08:00:00")
    _paso(db, "TEST-2", 2, "Alistamiento", "2026-08-02 08:00:00")
    _sub(db, "TEST-2", "pendiente de entrega")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert df.iloc[0]["estado_actual"] == "Alistamiento"


@pytest.mark.integration
def test_abierto_se_deriva_de_los_subpedidos_no_del_timeline(db):
    """Un pedido con todos sus subpedidos cerrados no está abierto, esté donde esté."""
    _pedido(db, "ABIERTO")
    _paso(db, "ABIERTO", 1, "Alistamiento", "2026-08-01 08:00:00")
    _sub(db, "ABIERTO", "pendiente de entrega")

    _pedido(db, "CERRADO")
    _paso(db, "CERRADO", 1, "Alistamiento", "2026-08-01 08:00:00")
    _sub(db, "CERRADO", "completado")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31").set_index("id_pedido")

    assert int(df.loc["ABIERTO", "abierto"]) == 1
    assert int(df.loc["CERRADO", "abierto"]) == 0


@pytest.mark.integration
def test_un_subpedido_abierto_basta_para_que_el_pedido_lo_este(db):
    _pedido(db, "MIXTO")
    _paso(db, "MIXTO", 1, "Alistamiento", "2026-08-01 08:00:00")
    _sub(db, "MIXTO", "completado")
    _sub(db, "MIXTO", "pendiente de entrega")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert int(df.iloc[0]["abierto"]) == 1


@pytest.mark.integration
def test_entregado_en_sale_del_paso_recibido_y_recibido(db):
    _pedido(db, "TEST-3")
    _paso(db, "TEST-3", 1, "pdt despachar", "2026-08-01 08:00:00")
    _paso(db, "TEST-3", 2, "Recibido y recibido", "2026-08-04 15:00:00")
    _sub(db, "TEST-3", "completado")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert df.iloc[0]["entregado_en"] == "2026-08-04 15:00:00"


@pytest.mark.integration
def test_pedido_sin_entregar_no_inventa_fecha(db):
    _pedido(db, "TEST-4")
    _paso(db, "TEST-4", 1, "Alistamiento", "2026-08-01 08:00:00")
    _sub(db, "TEST-4", "pendiente de entrega")
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert df.iloc[0]["entregado_en"] is None


@pytest.mark.integration
def test_el_join_de_monto_no_duplica_el_pedido(db):
    """`estadisticas_monto` tiene ~12 filas por pedido; solo una es el total."""
    _pedido(db, "TEST-5")
    _paso(db, "TEST-5", 1, "Alistamiento", "2026-08-01 08:00:00")
    _sub(db, "TEST-5", "completado")
    for concepto, monto in (
        ("Total a pagar / Total final a pagar", 9000.0),
        ("Total IVA", 1400.0),
        ("Total descuento", -500.0),
    ):
        db.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto_base, monto_final_num)"
            " VALUES ('TEST-5',?,?)",
            (concepto, monto),
        )
    db.commit()

    df = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert len(df) == 1, "el join con estadisticas_monto multiplicó filas"
    assert df.iloc[0]["valor"] == 9000.0


@pytest.mark.integration
def test_transiciones_son_entre_pasos_consecutivos(db):
    _pedido(db, "TEST-6")
    _paso(db, "TEST-6", 1, "confirmando", "2026-08-01 00:00:00")
    _paso(db, "TEST-6", 2, "Alistamiento", "2026-08-01 12:00:00")
    _paso(db, "TEST-6", 3, "Listo para enviar", "2026-08-02 12:00:00")
    db.commit()

    df = ddb.get_ciclo_transiciones("2026-01-01", "2026-12-31")

    assert len(df) == 2, "debe haber n-1 transiciones, no todas las combinaciones"
    pares = set(zip(df["origen"], df["destino"], strict=True))
    assert pares == {("confirmando", "Alistamiento"), ("Alistamiento", "Listo para enviar")}
    horas = dict(zip(df["origen"], df["horas"], strict=True))
    assert horas["confirmando"] == pytest.approx(12.0)
    assert horas["Alistamiento"] == pytest.approx(24.0)


@pytest.mark.integration
def test_un_hueco_en_la_numeracion_corta_la_cadena(db):
    """`paso = paso + 1` es literal: si el origen salta un número, no se inventa el salto."""
    _pedido(db, "TEST-7")
    _paso(db, "TEST-7", 1, "confirmando", "2026-08-01 00:00:00")
    _paso(db, "TEST-7", 3, "Listo para enviar", "2026-08-02 00:00:00")
    db.commit()

    df = ddb.get_ciclo_transiciones("2026-01-01", "2026-12-31")

    assert df.empty


@pytest.mark.integration
def test_las_transiciones_no_cruzan_pedidos(db):
    """Sin la condición de `id_pedido` se medirían saltos entre pedidos distintos."""
    _pedido(db, "A")
    _paso(db, "A", 1, "confirmando", "2026-08-01 00:00:00")
    _pedido(db, "B")
    _paso(db, "B", 2, "Alistamiento", "2026-08-01 06:00:00")
    db.commit()

    df = ddb.get_ciclo_transiciones("2026-01-01", "2026-12-31")

    assert df.empty
