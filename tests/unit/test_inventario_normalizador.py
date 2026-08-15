"""Tests de inventario/normalizador.py (DEC-039).

pd.read_excel se monkeypatchea con DataFrames en memoria — sin tocar el
filesystem, así los tests de renombrado/exclusión de placeholders quedan
en tests/unit/ (sin I/O externo, mismo criterio que el resto del proyecto).
cruzar_inventarios()/resumen_cruce() son funciones puras sobre DataFrames,
se testean directo.
"""

import pandas as pd
import pytest

from inventario.normalizador import (
    cargar_admin,
    cargar_bochica,
    cruzar_inventarios,
    resumen_cruce,
)

pytestmark = pytest.mark.unit

_ADMIN_RAW = pd.DataFrame(
    {
        "Identificación del producto": [111, 222],
        "ID de especificación": [1001, 1002],
        "Nombre comercial": ["Producto A", "Producto B"],
        "Inventario": [10, 0],
        "Referencia del producto.": ["PA01", "PB02"],
        "ALMACEN": ["Bogotá", "Bogotá"],
        "Categoria del producto": ["Juguetes", "Aseo"],
        "Especificación": ["Color: Rojo;", "Talla: M;"],
        "Código de barras.": [7700000000001, 7700000000002],
        "Peso": [500, 1000],
        "Precio": [10000, 20000],
        "Existencias restantes.": [8, None],
        "Producto activo o inactivo": ["Fue", "No hay"],
        "Descuento": ["0%", "10%"],
        "IVA": ["19%", "19%"],
    }
)

_BOCHICA_RAW = pd.DataFrame(
    {
        "ID Producto": [1002, 1002, 0, 9999],
        "Referencia": ["PB02", "PB02", "REUTILIZAR-REUTILIZAR", "PX99"],
        "Especificación": ["Talla: M;", "Talla: M;", "REUTILIZAR", "Sin dato"],
        "Ubicación": ["A_1_1", "_MIGRADO", "subbodega", "A_2_3"],
        "BL": ["BL001", "BL001", "-", "BL002"],
        "Cantidad": [5, 3, 1, 2],
        "Lote": ["-", "-", "-", "-"],
        "Fecha Vencimiento": ["-", "-", "-", "-"],
    }
)


@pytest.fixture
def admin_read_excel(monkeypatch):
    monkeypatch.setattr(
        "inventario.normalizador.pd.read_excel", lambda _path, **_kw: _ADMIN_RAW.copy()
    )


@pytest.fixture
def bochica_read_excel(monkeypatch):
    monkeypatch.setattr(
        "inventario.normalizador.pd.read_excel", lambda _path, **_kw: _BOCHICA_RAW.copy()
    )


def test_cargar_admin_renombra_columnas_y_castea_id(admin_read_excel):
    df = cargar_admin("cualquier-ruta.xlsx")

    assert list(df.columns) == [
        "id_especificacion",
        "id_producto_admin",
        "nombre_comercial",
        "referencia",
        "almacen",
        "categoria",
        "especificacion",
        "codigo_barras",
        "peso",
        "inventario",
        "existencias_restantes",
        "precio",
        "producto_activo",
        "descuento",
        "iva",
    ]
    assert df["id_especificacion"].tolist() == ["1001", "1002"]
    assert all(isinstance(v, str) for v in df["id_especificacion"])


def test_cargar_bochica_excluye_placeholders_por_default(bochica_read_excel):
    df = cargar_bochica("cualquier-ruta.xlsx")

    # De las 4 filas crudas quedan 2: id=0 (REUTILIZAR) y ubicacion
    # _MIGRADO se excluyen; A_1_1 y A_2_3 se conservan.
    assert len(df) == 2
    assert "0" not in df["id_especificacion"].tolist()
    assert "_MIGRADO" not in df["ubicacion"].tolist()
    assert "subbodega" not in df["ubicacion"].tolist()
    assert set(df["ubicacion"]) == {"A_1_1", "A_2_3"}


def test_cargar_bochica_conserva_placeholders_si_se_desactiva(bochica_read_excel):
    df = cargar_bochica("cualquier-ruta.xlsx", excluir_placeholders=False)

    assert len(df) == 4
    assert "0" in df["id_especificacion"].tolist()


def test_cargar_bochica_conserva_mayusculas_de_ubicacion(bochica_read_excel):
    """DEC-039 pregunta 2 sigue abierta (P vs p) — no normalizar sin el layout."""
    df = cargar_bochica("cualquier-ruta.xlsx")
    assert "A_1_1" in df["ubicacion"].tolist()  # no forzado a mayúscula/minúscula


def test_cruzar_inventarios_outer_join_y_resumen():
    admin = pd.DataFrame({"id_especificacion": ["1", "2"], "nombre_comercial": ["A", "B"]})
    bochica = pd.DataFrame({"id_especificacion": ["2", "3"], "cantidad": [5, 7]})

    cruzado = cruzar_inventarios(admin, bochica)
    resumen = resumen_cruce(cruzado)

    assert resumen == {"both": 1, "solo_admin": 1, "solo_bochica": 1}
    # id="2" cruza en ambos lados con sus columnas propias
    fila_cruzada = cruzado[cruzado["id_especificacion"] == "2"].iloc[0]
    assert fila_cruzada["nombre_comercial"] == "B"
    assert fila_cruzada["cantidad"] == 5
