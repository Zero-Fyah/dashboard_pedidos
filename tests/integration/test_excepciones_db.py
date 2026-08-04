"""`get_excepciones()` — el circuito de excepción del proceso (DEC-099).

Dos cosas que estos tests fijan y que en producción cambian cifras publicadas:

1. El `LEFT JOIN` contra los eventos. Con `JOIN` interno, los 412 pedidos sin
   ninguna fila en `registro_operaciones` desaparecen y el porcentaje de
   autogestión sube de 89,8% a 91,1% sin que nada lo delate.
2. `MAX(momento)` para la última entrega. Con `MIN` —que es lo natural de
   escribir— una entrega cancelada y reintentada con éxito se contaría como
   entrega fallida, porque la primera entrega es anterior a la cancelación.
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
            id_pedido TEXT PRIMARY KEY, fecha TEXT, nombre_empresa TEXT,
            metodo_entrega TEXT, vendedor TEXT, forma_pago TEXT
        );
        CREATE TABLE registro_operaciones (
            id INTEGER PRIMARY KEY, id_pedido TEXT, momento TEXT, usuario TEXT,
            tipo_usuario TEXT, accion TEXT, referencia TEXT
        );
        CREATE TABLE gestion_diferencias (
            id INTEGER PRIMARY KEY, id_pedido TEXT, total_pagar_pedido_num REAL,
            monto_final_pagar_num REAL, monto_pagado_num REAL, monto_diferencia_num REAL
        );
        CREATE TABLE detalle_diferencias (
            id INTEGER PRIMARY KEY, id_pedido TEXT, nombre_producto TEXT,
            especificacion TEXT, tipo TEXT, precio_unitario_num REAL, descuento_num REAL,
            precio_descuento_num REAL, cantidad_pedido_num REAL, cantidad_entregada_num REAL,
            diferencia_cantidad_num REAL, monto_pagar_pedido_num REAL,
            monto_final_pagar_num REAL, iva_num REAL, monto_diferencia_num REAL
        );
        CREATE VIEW v_gestion_diferencias_num AS
            SELECT id, id_pedido, total_pagar_pedido_num AS total_pagar_pedido,
                   monto_final_pagar_num AS monto_final_pagar,
                   monto_pagado_num AS monto_pagado,
                   monto_diferencia_num AS monto_diferencia
              FROM gestion_diferencias;
        CREATE VIEW v_detalle_diferencias_num AS
            SELECT id, id_pedido, nombre_producto, especificacion, tipo,
                   precio_unitario_num AS precio_unitario, descuento_num AS descuento,
                   precio_descuento_num AS precio_descuento,
                   cantidad_pedido_num AS cantidad_pedido,
                   cantidad_entregada_num AS cantidad_entregada,
                   diferencia_cantidad_num AS diferencia_cantidad,
                   monto_pagar_pedido_num AS monto_pagar_pedido,
                   monto_final_pagar_num AS monto_final_pagar, iva_num AS iva,
                   monto_diferencia_num AS monto_diferencia
              FROM detalle_diferencias;
        """
    )
    con.commit()
    return ruta, con


def _pedido(con, pid, fecha="2026-08-01"):
    con.execute(
        "INSERT INTO pedidos VALUES (?,?,'ACME','Ruta','ANA','Pago inmediato')", (pid, fecha)
    )


def _ev(con, pid, accion, momento, tipo="system", usuario="sys"):
    con.execute(
        "INSERT INTO registro_operaciones (id_pedido, momento, usuario, tipo_usuario,"
        " accion, referencia) VALUES (?,?,?,?,?,'')",
        (pid, momento, usuario, tipo, accion),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    ruta, con = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    ddb.get_excepciones.clear()
    yield con
    con.close()


@pytest.mark.integration
def test_un_pedido_sin_eventos_no_desaparece(db):
    """Con JOIN interno, la autogestión saltaría de 89,8% a 91,1% en producción."""
    _pedido(db, "SIN-EVENTOS")
    db.commit()

    df = ddb.get_excepciones("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert int(df.iloc[0]["autogestion"]) == 0
    assert int(df.iloc[0]["ajustes_manuales"]) == 0


@pytest.mark.integration
def test_una_entrega_reintentada_no_cuenta_como_fallida(db):
    """La segunda trampa: con MIN(momento) este caso se leería como entrega perdida."""
    _pedido(db, "REINTENTADA")
    _ev(db, "REINTENTADA", "Entrega", "2026-08-01 09:00:00")
    _ev(db, "REINTENTADA", "Cancelar entrega", "2026-08-01 12:00:00")
    _ev(db, "REINTENTADA", "Entrega", "2026-08-03 10:00:00")
    db.commit()

    fila = ddb.get_excepciones("2026-01-01", "2026-12-31").iloc[0]

    assert fila["entregado_en"] == "2026-08-01 09:00:00"
    assert fila["ultima_entrega_en"] == "2026-08-03 10:00:00"
    assert fila["ultima_entrega_en"] > fila["entrega_cancelada_en"]


@pytest.mark.integration
def test_marca_el_circuito_completo_del_faltante(db):
    _pedido(db, "FALT")
    _ev(db, "FALT", "Alistamiento con faltantes", "2026-08-01 08:00:00")
    _ev(db, "FALT", "Faltantes no aprobados", "2026-08-01 09:00:00")
    _ev(db, "FALT", "Faltantes aprobados", "2026-08-01 11:00:00")
    db.commit()

    fila = ddb.get_excepciones("2026-01-01", "2026-12-31").iloc[0]

    assert fila["faltante_en"] == "2026-08-01 08:00:00"
    assert fila["rechazado_en"] == "2026-08-01 09:00:00"
    assert fila["aprobado_en"] == "2026-08-01 11:00:00"


@pytest.mark.integration
def test_autogestion_exige_tipo_member(db):
    """Un pedido creado por personal interno no es autogestión del cliente."""
    _pedido(db, "CLIENTE")
    _ev(db, "CLIENTE", "Usuario realizó pedido", "2026-08-01 08:00:00", tipo="member")
    _pedido(db, "INTERNO")
    _ev(db, "INTERNO", "Usuario realizó pedido", "2026-08-01 08:00:00", tipo="staff")
    db.commit()

    df = ddb.get_excepciones("2026-01-01", "2026-12-31").set_index("id_pedido")

    assert int(df.loc["CLIENTE", "autogestion"]) == 1
    assert int(df.loc["INTERNO", "autogestion"]) == 0


@pytest.mark.integration
def test_cuenta_los_ajustes_manuales_y_su_autor(db):
    _pedido(db, "AJUSTADO")
    _ev(
        db,
        "AJUSTADO",
        "Modificar cantidad de entrega manualmente",
        "2026-08-01 08:00:00",
        usuario="PEPE",
    )
    _ev(
        db,
        "AJUSTADO",
        "Modificar cantidad de entrega manualmente",
        "2026-08-02 08:00:00",
        usuario="PEPE",
    )
    db.commit()

    fila = ddb.get_excepciones("2026-01-01", "2026-12-31").iloc[0]

    assert int(fila["ajustes_manuales"]) == 2
    assert fila["ajustado_por"] == "PEPE"


@pytest.mark.integration
def test_trae_el_monto_y_el_detalle_de_la_diferencia(db):
    _pedido(db, "CON-DIF")
    _ev(db, "CON-DIF", "Alistamiento con faltantes", "2026-08-01 08:00:00")
    db.execute(
        "INSERT INTO gestion_diferencias (id_pedido, monto_diferencia_num) VALUES ('CON-DIF', 5000)"
    )
    for producto, tipo in (("P1", "Arena"), ("P2", "Accesorios")):
        db.execute(
            "INSERT INTO detalle_diferencias (id_pedido, nombre_producto, tipo)"
            " VALUES ('CON-DIF',?,?)",
            (producto, tipo),
        )
    db.commit()

    fila = ddb.get_excepciones("2026-01-01", "2026-12-31").iloc[0]

    assert fila["monto_diferencia"] == 5000
    assert int(fila["productos_con_faltante"]) == 2
    assert set(fila["lineas_afectadas"].split(",")) == {"Arena", "Accesorios"}


@pytest.mark.integration
def test_el_detalle_no_multiplica_el_pedido(db):
    """`detalle_diferencias` tiene una fila por producto: no puede duplicar el pedido."""
    _pedido(db, "MULTI")
    for i in range(5):
        db.execute(
            "INSERT INTO detalle_diferencias (id_pedido, nombre_producto, tipo)"
            " VALUES ('MULTI',?, 'Arena')",
            (f"P{i}",),
        )
    db.commit()

    df = ddb.get_excepciones("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert int(df.iloc[0]["productos_con_faltante"]) == 5
