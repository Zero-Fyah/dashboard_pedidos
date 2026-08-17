"""get_arena_movimiento() — líneas de venta de Arena con fecha/ciudad/estado (DEC-119)."""

import sqlite3

import pytest

import dashboard.db as ddb


@pytest.fixture(autouse=True)
def _sin_cache():
    ddb.get_arena_movimiento.clear()
    yield


def _base(tmp_path):
    ruta = tmp_path / "arena_mov.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE arena_inventario (
            id INTEGER PRIMARY KEY, codigo_barras TEXT, especificacion TEXT,
            nombre_comercial TEXT, referencia TEXT, almacen TEXT, peso_g REAL,
            inventario REAL, existencias_restantes REAL, producto_activo TEXT,
            modalidad TEXT, corrida_id INTEGER
        );
        CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY, fecha TEXT);
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            referencia TEXT, codigo_barras TEXT, almacen TEXT, cantidad_comprada REAL
        );
        """
    )
    con.commit()
    con.close()
    return ruta


@pytest.mark.integration
def test_filtra_por_codigo_de_barras_de_arena_inventario(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    con.execute("INSERT INTO arena_inventario (codigo_barras, almacen) VALUES ('111', 'Bogotá')")
    con.execute("INSERT INTO pedidos (id_pedido, fecha) VALUES ('P1', '2026-08-01')")
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES ('P1', 'S1', 'Completado')"
    )
    # Línea de Arena (código en arena_inventario) — debe traerse.
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " almacen, cantidad_comprada) VALUES ('P1', 'S1', 'PRA ARENA TONELADA', '111',"
        " 'Bogotá', 50)"
    )
    # Línea de un producto que NO es Arena — no debe traerse.
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " almacen, cantidad_comprada) VALUES ('P1', 'S1', 'Comedero PC85', '999',"
        " 'Bogotá', 2)"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_arena_movimiento()

    assert list(df["codigo_barras"]) == ["111"]
    assert df.iloc[0]["fecha"] == "2026-08-01"
    assert df.iloc[0]["estado"] == "Completado"
    assert df.iloc[0]["almacen"] == "Bogotá"
    assert df.iloc[0]["cantidad_comprada"] == 50


@pytest.mark.integration
def test_arena_inventario_vacia_devuelve_vacio(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)

    df = ddb.get_arena_movimiento()

    assert df.empty


@pytest.mark.integration
def test_sin_tabla_arena_inventario_no_explota(monkeypatch, tmp_path):
    ruta = tmp_path / "sin_arena.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE pedidos (id_pedido TEXT)")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_arena_movimiento()

    assert df.empty
