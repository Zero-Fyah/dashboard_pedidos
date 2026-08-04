"""`get_comprometido()` — el inventario comprometido, bien medido (DEC-098).

El test central de este archivo fija **la razón por la que esta consulta existe**:
`v_inventario_comprometido` restaba `cantidad_entregada` a `cantidad_comprada`
sobre subpedidos abiertos, y el origen puebla las dos iguales mientras el
subpedido está vivo. La resta daba 251 unidades cuando lo comprometido eran
497.929. Si alguien "optimiza" esta consulta volviendo a la resta, el test cae.
"""

import sqlite3

import pytest

import dashboard.db as ddb


def _base(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            tipo_subpedido TEXT, nombre_producto TEXT, referencia TEXT,
            codigo_barras TEXT, presentacion TEXT, almacen TEXT,
            cantidad_comprada REAL, cantidad_entregada REAL,
            precio_unitario_num REAL, descuento_num REAL, precio_descuento_num REAL,
            monto_pagar_num REAL, monto_final_num REAL, iva_num REAL,
            peso_total_num REAL, observaciones TEXT, numero_caja TEXT, tipo TEXT
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE inventario_salud (
            referencia TEXT, disponible REAL, estado TEXT, familia TEXT
        );
        CREATE VIEW v_lineas_pedido_num AS
            SELECT id, id_pedido, numero_subpedido, tipo_subpedido, nombre_producto,
                   referencia, codigo_barras, presentacion, almacen,
                   cantidad_comprada, cantidad_entregada,
                   precio_unitario_num AS precio_unitario,
                   descuento_num AS descuento,
                   precio_descuento_num AS precio_descuento,
                   monto_pagar_num AS monto_pagar,
                   monto_final_num AS monto_final,
                   iva_num AS iva, peso_total_num AS peso_total,
                   observaciones, numero_caja, tipo
              FROM lineas_pedido;
        """
    )
    con.commit()
    return ruta, con


def _linea(con, pid, ref, comprada, entregada, monto=1000.0, producto="PROD"):
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, nombre_producto,"
        " referencia, cantidad_comprada, cantidad_entregada, monto_final_num, almacen, tipo)"
        " VALUES (?,'1',?,?,?,?,?,'BOD','Arena')",
        (pid, producto, ref, comprada, entregada, monto),
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
    ddb.get_comprometido.clear()
    yield con
    con.close()


@pytest.mark.integration
def test_el_comprometido_es_la_cantidad_comprada_no_la_resta(db):
    """El corazón de DEC-098.

    Se replica el patrón real del origen: subpedido abierto con
    `cantidad_entregada == cantidad_comprada`. La resta daría 0; lo comprometido
    son las 100 unidades.
    """
    _pedido = "TEST-1"
    _linea(db, _pedido, "REF-A", comprada=100.0, entregada=100.0)
    _sub(db, _pedido, "pendiente de entrega")
    db.commit()

    df = ddb.get_comprometido()

    assert len(df) == 1
    assert df.iloc[0]["comprometido"] == 100.0, "volvió a usar comprada − entregada"


@pytest.mark.integration
def test_los_subpedidos_cerrados_no_comprometen_nada(db):
    """Un subpedido completado ya salió: su mercancía no está reservada."""
    _linea(db, "CERRADO", "REF-A", 100.0, 100.0)
    _sub(db, "CERRADO", "completado")
    _linea(db, "CANCELADO", "REF-A", 50.0, 50.0)
    _sub(db, "CANCELADO", "cancelado")
    _linea(db, "ABIERTO", "REF-A", 7.0, 7.0)
    _sub(db, "ABIERTO", "en inspección")
    db.commit()

    df = ddb.get_comprometido()

    assert len(df) == 1
    assert df.iloc[0]["comprometido"] == 7.0


@pytest.mark.integration
def test_el_faltante_es_lo_comprometido_menos_el_stock(db):
    _linea(db, "P1", "REF-A", 100.0, 100.0)
    _sub(db, "P1", "pendiente de entrega")
    db.execute("INSERT INTO inventario_salud VALUES ('REF-A', 30.0, 'Normal', 'PS')")
    db.commit()

    df = ddb.get_comprometido()

    assert df.iloc[0]["faltante"] == 70.0
    assert df.iloc[0]["disponible"] == 30.0


@pytest.mark.integration
def test_el_faltante_nunca_es_negativo(db):
    """Sobrar stock no es un faltante negativo: es no tener faltante."""
    _linea(db, "P1", "REF-A", 10.0, 10.0)
    _sub(db, "P1", "pendiente de entrega")
    db.execute("INSERT INTO inventario_salud VALUES ('REF-A', 500.0, 'Normal', 'PS')")
    db.commit()

    df = ddb.get_comprometido()

    assert df.iloc[0]["faltante"] == 0.0


@pytest.mark.integration
def test_sin_dato_de_stock_no_se_asume_cero(db):
    """No saber el stock no es lo mismo que saber que es cero.

    La referencia tiene que seguir apareciendo, con `disponible` nulo, para que
    la página pueda excluirla de la cobertura en vez de contarla como faltante.
    """
    _linea(db, "P1", "REF-SIN-SALUD", 40.0, 40.0)
    _sub(db, "P1", "pendiente de entrega")
    db.commit()

    df = ddb.get_comprometido()

    assert len(df) == 1
    assert df.iloc[0]["disponible"] is None
    assert df.iloc[0]["comprometido"] == 40.0


@pytest.mark.integration
def test_agrupa_por_referencia_sumando_pedidos(db):
    _linea(db, "P1", "REF-A", 10.0, 10.0)
    _sub(db, "P1", "pendiente de entrega")
    _linea(db, "P2", "REF-A", 15.0, 15.0)
    _sub(db, "P2", "en inspección")
    db.commit()

    df = ddb.get_comprometido()

    assert len(df) == 1
    assert df.iloc[0]["comprometido"] == 25.0
    assert int(df.iloc[0]["pedidos"]) == 2
    assert int(df.iloc[0]["lineas"]) == 2


@pytest.mark.integration
def test_las_lineas_sin_producto_no_entran(db):
    """Filas vacías del origen no son mercancía comprometida."""
    _linea(db, "P1", "REF-A", 10.0, 10.0, producto="")
    _sub(db, "P1", "pendiente de entrega")
    db.commit()

    df = ddb.get_comprometido()

    assert df.empty
