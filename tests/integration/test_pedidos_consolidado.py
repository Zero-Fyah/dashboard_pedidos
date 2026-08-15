"""get_pedidos_consolidado() — Consolidado de pedidos (DEC-045/DEC-109/DEC-111).

DEC-109 angostó "Solo previos a picking" de las 12 estados de
ESTADOS_ACTIVOS_INVENTARIO a solo 3 (comprometidos, sin alistar). DEC-111
reemplazó la columna "ID del producto" por "ID de especificación".
"""

import sqlite3

import pytest

import dashboard.db as ddb


@pytest.fixture(autouse=True)
def _sin_cache():
    """`get_pedidos_consolidado` tiene `@st.cache_data`: sin esto, un test
    reutilizaría el resultado cacheado del anterior aunque DB_PATH cambie
    (la clave de caché es por argumentos, no por ruta de base de datos)."""
    ddb.get_pedidos_consolidado.clear()
    yield


def _base(tmp_path):
    ruta = tmp_path / "consolidado.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY, fecha TEXT, hora TEXT);
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            tipo_subpedido TEXT, estado TEXT, inicio_inspeccion TEXT
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            almacen TEXT, referencia TEXT, codigo_barras TEXT,
            presentacion TEXT, cantidad_comprada REAL
        );
        CREATE TABLE catalogo_productos (
            referencia TEXT, codigo_barras TEXT, id_producto TEXT,
            id_especificacion TEXT, nombre_comercial TEXT,
            PRIMARY KEY (referencia, codigo_barras)
        );
        """
    )
    con.commit()
    con.close()
    return ruta


def _pedido(con, id_pedido, numero_subpedido, estado, *, inicio_inspeccion="-", referencia="PJ91"):
    con.execute(
        "INSERT OR IGNORE INTO pedidos (id_pedido, fecha, hora) VALUES (?, '2026-08-01', '10:00')",
        (id_pedido,),
    )
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, tipo_subpedido, estado,"
        " inicio_inspeccion) VALUES (?, ?, 'Normal', ?, ?)",
        (id_pedido, numero_subpedido, estado, inicio_inspeccion),
    )
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, almacen, referencia,"
        " codigo_barras, presentacion, cantidad_comprada) VALUES (?, ?, 'Principal', ?,"
        " '7700001', 'Unidad', 1)",
        (id_pedido, numero_subpedido, referencia),
    )


@pytest.mark.integration
def test_previos_picking_incluye_los_tres_estados_nuevos(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(con, "P1", "1", "Pendiente de pago (pago inmediato)")
    _pedido(con, "P2", "1", "Pendiente de recolección")
    _pedido(con, "P3", "1", "Aprobación de Pagos")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df, agg = ddb.get_pedidos_consolidado("2026-01-01", "2026-12-31", "Todos", (), True, 100)

    assert set(df["Pedido padre"]) == {"P1", "P2", "P3"}
    assert agg["pedidos"] == 3


@pytest.mark.integration
def test_previos_picking_excluye_estados_removidos(monkeypatch, tmp_path):
    """Pendiente de entrega, En inspección y Pendiente de confirmación ya no cuentan (DEC-109)."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(con, "P1", "1", "Pendiente de pago (pago inmediato)")  # sigue entrando
    _pedido(con, "P4", "1", "Pendiente de entrega")
    _pedido(con, "P5", "1", "En inspección")
    _pedido(con, "P6", "1", "Pendiente de confirmación")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df, agg = ddb.get_pedidos_consolidado("2026-01-01", "2026-12-31", "Todos", (), True, 100)

    assert set(df["Pedido padre"]) == {"P1"}


@pytest.mark.integration
def test_sin_filtro_previos_picking_aparecen_todos_los_estados(monkeypatch, tmp_path):
    """El cuadro principal no se restringe: DEC-109 solo toca el checkbox."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(con, "P1", "1", "Pendiente de pago (pago inmediato)")
    _pedido(con, "P4", "1", "Pendiente de entrega")
    _pedido(con, "P7", "1", "Completado")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df, agg = ddb.get_pedidos_consolidado("2026-01-01", "2026-12-31", "Todos", (), False, 100)

    assert set(df["Pedido padre"]) == {"P1", "P4", "P7"}


@pytest.mark.integration
def test_columna_id_especificacion_reemplaza_id_producto(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(con, "P1", "1", "Completado", referencia="PJ91")
    con.execute(
        "INSERT INTO catalogo_productos (referencia, codigo_barras, id_producto,"
        " id_especificacion, nombre_comercial) VALUES ('PJ91', '7700001', 'PROD-1',"
        " 'ESPEC-1', 'Peluche')"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df, agg = ddb.get_pedidos_consolidado("2026-01-01", "2026-12-31", "Todos", (), False, 100)

    assert "ID de especificación" in df.columns
    assert "ID del producto" not in df.columns
    assert df.iloc[0]["ID de especificación"] == "ESPEC-1"
    assert agg["sin_id"] == 0


@pytest.mark.integration
def test_sin_id_cuenta_id_especificacion_null_no_id_producto(monkeypatch, tmp_path):
    """DEC-111: un par con id_producto pero sin id_especificacion (ambiguo) cuenta como sin ID."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(con, "P1", "1", "Completado", referencia="PJ91")
    con.execute(
        "INSERT INTO catalogo_productos (referencia, codigo_barras, id_producto,"
        " id_especificacion, nombre_comercial) VALUES ('PJ91', '7700001', 'PROD-1',"
        " NULL, 'Peluche')"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df, agg = ddb.get_pedidos_consolidado("2026-01-01", "2026-12-31", "Todos", (), False, 100)

    assert agg["sin_id"] == 1
