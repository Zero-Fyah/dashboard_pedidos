"""`get_ventas()`, `get_ventas_por_linea()` y `get_descuentos_por_tipo()` (DEC-097).

El test que más importa es el de cancelaciones. De los $84.093 M facturados,
**$11,6 mil millones son pedidos totalmente cancelados**: si la consulta dejara
de marcarlos, la página publicaría una facturación 14% mayor que la real y nada
fallaría. Ese es el tipo de error que no se detecta después, porque el número se
ve razonable.
"""

import sqlite3

import pytest

import dashboard.db as ddb

_CONCEPTOS = (
    ("Total a pagar / Total final a pagar", 11900.0),
    ("Total antes de IVA", 10000.0),
    ("Total precio original", 12500.0),
    ("Total descuento", -900.0),
    ("Total IVA", 1900.0),
)


def _base(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, vendedor TEXT,
            nombre_empresa TEXT, nit TEXT, metodo_entrega TEXT, forma_pago TEXT
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE estadisticas_monto (
            id INTEGER PRIMARY KEY, id_pedido TEXT, concepto_base TEXT,
            monto_pagar_num REAL, monto_final_num REAL, concepto TEXT,
            concepto_tag TEXT, tasa_iva REAL, orden INTEGER
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            tipo TEXT, cantidad_comprada REAL, monto_final_num REAL,
            monto_pagar_num REAL
        );
        CREATE VIEW v_estadisticas_monto_num AS
            SELECT id, id_pedido, orden, concepto, concepto_tag, concepto_base,
                   tasa_iva, monto_pagar_num AS monto_pagar,
                   monto_final_num AS monto_final
              FROM estadisticas_monto;
        """
    )
    con.commit()
    return ruta, con


def _pedido(con, pid, fecha="2026-08-01", vendedor="ANA", empresa="ACME"):
    con.execute(
        "INSERT INTO pedidos VALUES (?,?,?,?,'900','Ruta','Pago inmediato')",
        (pid, fecha, vendedor, empresa),
    )
    for concepto, monto in _CONCEPTOS:
        con.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto_base, monto_pagar_num,"
            " monto_final_num) VALUES (?,?,?,?)",
            (pid, concepto, monto, monto),
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
    for fn in (ddb.get_ventas, ddb.get_ventas_por_linea, ddb.get_descuentos_por_tipo):
        fn.clear()
    monkeypatch.setattr(ddb, "_num_cols_exist", lambda: True)
    yield con
    con.close()


@pytest.mark.integration
def test_marca_cancelado_solo_si_todos_los_subpedidos_lo_estan(db):
    """131 pedidos reales están parcialmente cancelados: no son ni una cosa ni la otra."""
    _pedido(db, "TODO-CANC")
    _sub(db, "TODO-CANC", "cancelado")
    _sub(db, "TODO-CANC", "cancelado")

    _pedido(db, "PARCIAL")
    _sub(db, "PARCIAL", "cancelado")
    _sub(db, "PARCIAL", "completado")

    _pedido(db, "VIVO")
    _sub(db, "VIVO", "completado")
    db.commit()

    df = ddb.get_ventas("2026-01-01", "2026-12-31").set_index("id_pedido")

    assert int(df.loc["TODO-CANC", "cancelado"]) == 1
    assert int(df.loc["PARCIAL", "cancelado"]) == 0, "un parcial no puede contar como perdido"
    assert int(df.loc["VIVO", "cancelado"]) == 0


@pytest.mark.integration
def test_los_montos_se_pivotan_sin_duplicar_el_pedido(db):
    """`estadisticas_monto` tiene ~12 filas por pedido; la consulta devuelve una."""
    _pedido(db, "TEST-1")
    _sub(db, "TEST-1", "completado")
    db.commit()

    df = ddb.get_ventas("2026-01-01", "2026-12-31")

    assert len(df) == 1
    fila = df.iloc[0]
    assert fila["total"] == 11900.0
    assert fila["antes_iva"] == 10000.0
    assert fila["precio_lista"] == 12500.0
    assert fila["descuento"] == -900.0
    assert fila["iva"] == 1900.0


@pytest.mark.integration
def test_un_pedido_sin_subpedidos_no_desaparece(db):
    """El LEFT JOIN con cancelado no puede borrar pedidos sin subpedidos."""
    _pedido(db, "SIN-SUB")
    db.commit()

    df = ddb.get_ventas("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert int(df.iloc[0]["cancelado"]) == 0


@pytest.mark.integration
def test_el_mix_por_linea_excluye_los_cancelados(db):
    _pedido(db, "VIVO")
    _sub(db, "VIVO", "completado")
    _pedido(db, "CANC")
    _sub(db, "CANC", "cancelado")
    for pid, tipo, valor in (
        ("VIVO", "Arena", 5000.0),
        ("VIVO", "Accesorios", 1000.0),
        ("CANC", "Arena", 9999.0),
    ):
        db.execute(
            "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, tipo,"
            " cantidad_comprada, monto_final_num) VALUES (?,'1',?,1,?)",
            (pid, tipo, valor),
        )
    db.commit()

    df = ddb.get_ventas_por_linea("2026-01-01", "2026-12-31")

    assert set(df["linea"]) == {"Arena", "Accesorios"}
    assert df[df["linea"] == "Arena"]["valor"].sum() == 5000.0, "coló el pedido cancelado"


@pytest.mark.integration
def test_ventas_respeta_el_rango_de_fechas(db):
    _pedido(db, "DENTRO", fecha="2026-05-15")
    _sub(db, "DENTRO", "completado")
    _pedido(db, "FUERA", fecha="2026-09-15")
    _sub(db, "FUERA", "completado")
    db.commit()

    df = ddb.get_ventas("2026-05-01", "2026-05-31")

    assert list(df["id_pedido"]) == ["DENTRO"]


@pytest.mark.integration
def test_sin_la_view_devuelve_vacio_en_vez_de_reventar(db, monkeypatch):
    """El dashboard tiene que abrir aunque el ETL todavía no haya corrido."""
    db.execute("DROP VIEW v_estadisticas_monto_num")
    db.commit()
    ddb.get_ventas.clear()

    df = ddb.get_ventas("2026-01-01", "2026-12-31")

    assert df.empty
