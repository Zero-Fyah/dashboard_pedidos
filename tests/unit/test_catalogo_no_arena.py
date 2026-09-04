"""Tests de inventario/catalogo_no_arena.py — vista consolidada por ID no-Arena.

Unitarios: `construir_catalogo_no_arena()` es una función pura sobre una
conexión de lectura, sin Excel de por medio (todo lo que necesita ya está
persistido). Se arma una SQLite en memoria con las tablas mínimas que el
scheduler ya escribe en cada corrida.
"""

import sqlite3

import pandas as pd
import pytest

from inventario.catalogo_no_arena import (
    TIPO_CAMBIO_ENTRADA,
    construir_catalogo_no_arena,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE inventario_ubicaciones (
            ubicacion TEXT, id_especificacion TEXT, referencia TEXT,
            familia TEXT, cantidad REAL, clase TEXT, xyz TEXT,
            en_catalogo INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE inventario_sin_ubicacion (
            id_especificacion TEXT, referencia TEXT, nombre_comercial TEXT,
            vigencia TEXT, inventario REAL
        )"""
    )
    c.execute(
        """CREATE TABLE inventario_abc (
            nivel TEXT, clave TEXT, padre TEXT, abc TEXT, xyz TEXT
        )"""
    )
    c.execute("CREATE TABLE inventario_salud (referencia TEXT, vigencia TEXT)")
    c.execute(
        """CREATE TABLE catalogo_productos (
            referencia TEXT, codigo_barras TEXT, id_producto TEXT,
            id_especificacion TEXT, nombre_comercial TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE movimientos_inventario (
            sku_id TEXT, referencia TEXT, almacen TEXT, tipo_cambio TEXT,
            fecha_operacion TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE lineas_pedido (
            id_pedido TEXT, numero_subpedido TEXT, referencia TEXT, codigo_barras TEXT
        )"""
    )
    c.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT)")
    c.execute("CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT, estado TEXT)")
    yield c
    c.close()


def _seed_basico(con):
    """Un ID con posición (ID1, clase propia) y uno sin posición (ID2)."""
    con.execute(
        "INSERT INTO inventario_ubicaciones VALUES ('A-01-1','ID1','PB01','PB',10,'A','X',1)"
    )
    con.execute(
        "INSERT INTO inventario_sin_ubicacion VALUES "
        "('ID2','PB02','Producto sin ubicación','Activo',5)"
    )
    con.execute("INSERT INTO inventario_abc VALUES ('id_global','ID1',NULL,'A','X')")
    con.execute("INSERT INTO inventario_abc VALUES ('referencia','PB01','PB','A',NULL)")
    con.execute("INSERT INTO inventario_abc VALUES ('referencia','PB02','PB','B','Z')")
    con.execute("INSERT INTO inventario_salud VALUES ('PB01','Activo')")
    con.execute(
        "INSERT INTO catalogo_productos VALUES ('PB01','7501234567890','PROD1','ID1','Cama para gato')"
    )
    con.execute(
        "INSERT INTO movimientos_inventario VALUES "
        "('ID1','PB01','Bogotá','Entrada','2026-03-01 10:00:00')"
    )
    con.execute(
        "INSERT INTO movimientos_inventario VALUES "
        "('ID1','PB01','Bogotá','Entrada de compra','2026-08-01 10:00:00')"
    )
    con.execute("INSERT INTO pedidos VALUES ('P1','2026-06-15')")
    con.execute("INSERT INTO subpedidos VALUES ('P1','1','Completado')")
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','1','PB01','7501234567890')")
    con.commit()


def test_una_fila_por_id(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con)
    assert sorted(df["id_especificacion"]) == ["ID1", "ID2"]
    assert len(df) == df["id_especificacion"].nunique()


def test_id_con_ubicacion_trae_clasificacion_propia(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    assert df.loc["ID1", "abc"] == "A"
    assert df.loc["ID1", "xyz"] == "X"
    assert df.loc["ID1", "origen_clasificacion"] == "ID"
    assert df.loc["ID1", "inventario_actual"] == 10


def test_id_sin_venta_atribuible_hereda_abc_de_la_referencia(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    # ID2 no tiene fila propia en inventario_abc nivel id/id_global: hereda
    # la clase de su referencia (PB02 -> B), marcada como heredada.
    assert df.loc["ID2", "abc"] == "B (por referencia)"
    assert df.loc["ID2", "origen_clasificacion"] == "Referencia"


def test_id_sin_venta_atribuible_hereda_xyz_de_la_referencia(con):
    """DEC-132 (decisión del Arquitecto, 2026-09-04): XYZ hereda igual que ABC.

    Mismo criterio de test que `test_id_sin_venta_atribuible_hereda_abc_de_la_
    referencia`: un ID sin fila propia en `inventario_abc` nivel id/id_global
    toma el `xyz` de su referencia (PB02 -> Z), con el mismo sufijo que ABC.
    """
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    assert df.loc["ID2", "xyz"] == "Z (por referencia)"
    # ID1 sí tiene xyz propio (id_global): no se toca, no hereda nada.
    assert df.loc["ID1", "xyz"] == "X"


def test_abc_y_xyz_heredan_de_forma_independiente(con):
    """Caso raro documentado en `_clasificacion_con_respaldo`: una referencia
    puede tener `abc='Sin consumo'` (sin ingreso neto) y aun así un `xyz`
    propio (hubo movimiento en unidades). Un ID sin fila propia hereda uno
    sin heredar el otro — `origen_clasificacion` describe solo el estado de
    ABC, documentado a propósito.
    """
    _seed_basico(con)
    con.execute(
        "INSERT INTO inventario_ubicaciones VALUES ('A-03-1','ID4','PB04','PB',3,NULL,NULL,1)"
    )
    con.execute("INSERT INTO inventario_abc VALUES ('referencia','PB04','PB','Sin consumo','Y')")
    con.commit()
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    assert pd.isna(df.loc["ID4", "abc"])
    assert df.loc["ID4", "origen_clasificacion"] == "Sin ventas"
    assert df.loc["ID4", "xyz"] == "Y (por referencia)"


def test_solo_tipo_cambio_entrada_exacto(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    # Hay dos movimientos para ID1: 'Entrada' (marzo) y 'Entrada de compra'
    # (agosto, más reciente). Debe ganar el literal exacto, no el más nuevo.
    assert df.loc["ID1", "ultimo_ingreso_contenedor"] == "2026-03-01 10:00:00"
    assert TIPO_CAMBIO_ENTRADA == "Entrada"


def test_venta_cancelada_no_cuenta_como_ultima_venta(con):
    _seed_basico(con)
    con.execute("INSERT INTO pedidos VALUES ('P2','2026-08-30')")
    con.execute("INSERT INTO subpedidos VALUES ('P2','1','Cancelado')")
    con.execute("INSERT INTO lineas_pedido VALUES ('P2','1','PB01','7501234567890')")
    con.commit()
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    assert df.loc["ID1", "ultima_venta"] == "2026-06-15"


def test_vigencia_declara_su_fuente(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    # ID1: no tiene vigencia propia (viene de inventario_ubicaciones) ->
    # aproximación por referencia, vía inventario_salud.
    assert df.loc["ID1", "fuente_vigencia"] == "referencia"
    assert df.loc["ID1", "activo_venta"] is True or bool(df.loc["ID1", "activo_venta"]) is True
    # ID2: sí tiene vigencia propia (inventario_sin_ubicacion, DEC-073).
    assert df.loc["ID2", "fuente_vigencia"] == "id"


def test_en_catalogo_0_queda_excluido(con):
    _seed_basico(con)
    con.execute(
        "INSERT INTO inventario_ubicaciones VALUES ('A-02-1','ID3','PB03','PB',7,'C','Z',0)"
    )
    con.commit()
    df = construir_catalogo_no_arena(con)
    assert "ID3" not in set(df["id_especificacion"])


def test_id_sin_par_en_catalogo_productos_no_inventa_codigo_de_barras(con):
    _seed_basico(con)
    df = construir_catalogo_no_arena(con).set_index("id_especificacion")
    # ID2 no tiene fila en catalogo_productos (ambigüedad de DEC-045/111,
    # simulada por ausencia): el hueco se declara con None, no con ''.
    assert pd.isna(df.loc["ID2", "codigo_barras"])
    assert pd.isna(df.loc["ID2", "id_producto"])


def test_vacio_no_revienta(con):
    df = construir_catalogo_no_arena(con)
    assert df.empty
    assert list(df.columns) == [
        "id_especificacion",
        "id_producto",
        "codigo_barras",
        "familia",
        "referencia",
        "descripcion",
        "abc",
        "xyz",
        "origen_clasificacion",
        "inventario_actual",
        "activo_venta",
        "fuente_vigencia",
        "ultimo_ingreso_contenedor",
        "ultima_venta",
    ]
