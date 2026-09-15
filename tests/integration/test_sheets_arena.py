"""integraciones/sheets_arena.py — consultas para el export de Arena por ciudad."""

import sqlite3

import pytest

from integraciones.sheets_arena import (
    COLUMNAS_INVENTARIO,
    COLUMNAS_PEDIDOS,
    inventario_disponible_arena,
    pedidos_previos_picking_arena,
)


def _base(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE arena_inventario (
            id INTEGER PRIMARY KEY, codigo_barras TEXT, especificacion TEXT,
            nombre_comercial TEXT, referencia TEXT, almacen TEXT, peso_g REAL,
            inventario REAL, existencias_restantes REAL, producto_activo TEXT,
            modalidad TEXT, corrida_id INTEGER
        );
        CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY, fecha TEXT, hora TEXT);
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            tipo_subpedido TEXT, estado TEXT, inicio_inspeccion TEXT
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            referencia TEXT, codigo_barras TEXT, presentacion TEXT,
            almacen TEXT, cantidad_comprada REAL
        );
        """
    )


@pytest.mark.integration
def test_inventario_suma_modalidades_nucleo_por_ciudad_y_codigo(tmp_path):
    ruta = tmp_path / "arena.db"
    con = sqlite3.connect(ruta)
    _base(con)
    # Dos modalidades núcleo del mismo código+ciudad: deben sumarse en una fila.
    con.execute(
        "INSERT INTO arena_inventario (codigo_barras, especificacion, nombre_comercial,"
        " referencia, almacen, inventario, modalidad) VALUES"
        " ('111', 'PRA13, 10KG', 'Arena X', 'PRA13', 'Cali', 100, 'Unidades')"
    )
    con.execute(
        "INSERT INTO arena_inventario (codigo_barras, especificacion, nombre_comercial,"
        " referencia, almacen, inventario, modalidad) VALUES"
        " ('111', 'PRA13, 10KG', 'Arena X', 'PRA13', 'Cali', 50, 'Tonelada')"
    )
    # Respaldo no es modalidad de venta: no debe sumarse ni aparecer.
    con.execute(
        "INSERT INTO arena_inventario (codigo_barras, especificacion, nombre_comercial,"
        " referencia, almacen, inventario, modalidad) VALUES"
        " ('111', 'PRA13, 10KG', 'Arena X', 'PRA13', 'Cali', 9999, 'Respaldo')"
    )
    # Ciudad fuera del alcance (Bogotá no es "solo arena"): no debe aparecer.
    con.execute(
        "INSERT INTO arena_inventario (codigo_barras, especificacion, nombre_comercial,"
        " referencia, almacen, inventario, modalidad) VALUES"
        " ('222', 'PRA20, 5KG', 'Arena Y', 'PRA20', 'Bogotá', 30, 'Unidades')"
    )
    con.commit()
    con.close()

    con = sqlite3.connect(ruta)
    df = inventario_disponible_arena(con)
    con.close()

    assert list(df.columns) == COLUMNAS_INVENTARIO
    assert len(df) == 1
    fila = df.iloc[0]
    assert fila["Ciudad"] == "Cali"
    assert fila["Disponible para venta"] == 150  # 100 + 50, sin el Respaldo


@pytest.mark.integration
def test_pedidos_previos_picking_filtra_estado_ciudad_e_inspeccion(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    _base(con)
    con.execute("INSERT INTO arena_inventario (codigo_barras, almacen) VALUES ('111', 'Cali')")
    con.execute("INSERT INTO pedidos VALUES ('P1', '2026-09-14', '10:00:00')")
    con.execute("INSERT INTO pedidos VALUES ('P2', '2026-09-14', '11:00:00')")
    con.execute("INSERT INTO pedidos VALUES ('P3', '2026-09-14', '12:00:00')")
    con.execute("INSERT INTO pedidos VALUES ('P4', '2026-09-14', '13:00:00')")
    # P1: cumple todo — debe aparecer.
    con.execute(
        "INSERT INTO subpedidos VALUES (1, 'P1', 'S1', 'Arena',"
        " 'Pendiente de pago (pago inmediato)', '-')"
    )
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " presentacion, almacen, cantidad_comprada) VALUES"
        " ('P1', 'S1', 'PRA13', '111', 'x', 'Cali', 10)"
    )
    # P2: mismo estado pero ya empezó inspección — no debe aparecer (DEC-109).
    con.execute(
        "INSERT INTO subpedidos VALUES (2, 'P2', 'S1', 'Arena',"
        " 'Pendiente de pago (pago inmediato)', '2026-09-14 09:00:00')"
    )
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " presentacion, almacen, cantidad_comprada) VALUES"
        " ('P2', 'S1', 'PRA13', '111', 'x', 'Cali', 5)"
    )
    # P3: estado cerrado, no previo a picking — no debe aparecer.
    con.execute("INSERT INTO subpedidos VALUES (3, 'P3', 'S1', 'Arena', 'Completado', '-')")
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " presentacion, almacen, cantidad_comprada) VALUES"
        " ('P3', 'S1', 'PRA13', '111', 'x', 'Cali', 3)"
    )
    # P4: estado correcto pero código de barras que no es Arena — no debe aparecer.
    con.execute(
        "INSERT INTO subpedidos VALUES (4, 'P4', 'S1', 'Accesorio',"
        " 'Pendiente de pago (pago inmediato)', '-')"
    )
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, codigo_barras,"
        " presentacion, almacen, cantidad_comprada) VALUES"
        " ('P4', 'S1', 'Comedero', '999', 'x', 'Cali', 1)"
    )
    con.commit()
    con.close()

    con = sqlite3.connect(ruta)
    df = pedidos_previos_picking_arena(con)
    con.close()

    assert list(df.columns) == COLUMNAS_PEDIDOS
    assert list(df["Pedido padre"]) == ["P1"]
    assert df.iloc[0]["Cantidad comprada"] == 10


@pytest.mark.integration
def test_ambas_devuelven_vacio_sin_datos(tmp_path):
    ruta = tmp_path / "vacia.db"
    con = sqlite3.connect(ruta)
    _base(con)
    con.commit()

    assert inventario_disponible_arena(con).empty
    assert pedidos_previos_picking_arena(con).empty
    con.close()
