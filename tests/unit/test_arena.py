"""Tests de inventario/arena.py::calcular_arena() (DEC-118).

Unitario: función pura sobre un DataFrame, mismo patrón que
`tests/unit/test_salud.py`. El catálogo sintético usa las columnas que
`inventario.normalizador.cargar_admin()` produce.
"""

import logging

import pandas as pd
import pytest

from inventario.arena import _COLUMNAS_ARENA, calcular_arena

pytestmark = pytest.mark.unit


def _catalogo(filas):
    """filas: dicts con al menos categoria/referencia/codigo_barras/almacen/peso.

    Rellena el resto de columnas que `cargar_admin()` produce con valores
    neutros, para que el test solo declare lo que le importa a cada caso.
    """
    base = {
        "id_especificacion": "1",
        "id_producto_admin": "1",
        "nombre_comercial": "x",
        "especificacion": "x",
        "inventario": 0,
        "existencias_restantes": None,
        "producto_activo": "Fue",
        "precio": 0,
        "descuento": "0%",
        "iva": "19%",
    }
    return pd.DataFrame([{**base, **fila} for fila in filas])


def test_clasifica_por_modalidad():
    df = _catalogo(
        [
            {
                "categoria": "Arena",
                "referencia": "PRA13",
                "codigo_barras": "111",
                "almacen": "Bogotá",
                "peso": 4500,
                "inventario": 10,
            },
            {
                "categoria": "Arena",
                "referencia": "ARENA TONELADA CORPORATIVO BOGOTA",
                "codigo_barras": "222",
                "almacen": "Bogotá",
                "peso": 25000,
                "inventario": 20,
            },
        ]
    )
    resultado = calcular_arena(df)
    assert len(resultado) == 2
    assert set(resultado["modalidad"]) == {"Unidades", "Corporativo"}
    assert list(resultado.columns) == list(_COLUMNAS_ARENA)


def test_excluye_categorias_distintas_de_arena():
    df = _catalogo(
        [
            {
                "categoria": "Juguetes",
                "referencia": "PJ91",
                "codigo_barras": "333",
                "almacen": "Bogotá",
                "peso": None,
            },
        ]
    )
    resultado = calcular_arena(df)
    assert resultado.empty


def test_excluye_referencias_conocidas_sin_uso():
    df = _catalogo(
        [
            {
                "categoria": "Arena",
                "referencia": "PRA-T01",
                "codigo_barras": "444",
                "almacen": "Bogotá",
                "peso": 1000,
            },
        ]
    )
    resultado = calcular_arena(df)
    assert resultado.empty


def test_referencia_no_reconocida_queda_fuera_y_avisa(caplog):
    df = _catalogo(
        [
            {
                "categoria": "Arena",
                "referencia": "ARENA SABOR NUEVO CARTAGENA",
                "codigo_barras": "555",
                "almacen": "Bogotá",
                "peso": 4500,
            },
        ]
    )
    with caplog.at_level(logging.WARNING, logger="inventario.arena"):
        resultado = calcular_arena(df)
    assert resultado.empty
    assert "NO reconocida" in caplog.text


def test_normaliza_espacios_en_almacen():
    df = _catalogo(
        [
            {
                "categoria": "Arena",
                "referencia": "PRA13",
                "codigo_barras": "666",
                "almacen": "Villavicencio ",
                "peso": 4500,
            },
        ]
    )
    resultado = calcular_arena(df)
    assert resultado.iloc[0]["almacen"] == "Villavicencio"


def test_peso_inconsistente_por_barcode_avisa(caplog):
    df = _catalogo(
        [
            {
                "categoria": "Arena",
                "referencia": "PRA13",
                "codigo_barras": "777",
                "almacen": "Bogotá",
                "peso": 4500,
            },
            {
                "categoria": "Arena",
                "referencia": "PRA13",
                "codigo_barras": "777",
                "almacen": "Cali",
                "peso": 10000,
            },
        ]
    )
    with caplog.at_level(logging.WARNING, logger="inventario.arena"):
        calcular_arena(df)
    assert "más de un peso" in caplog.text
