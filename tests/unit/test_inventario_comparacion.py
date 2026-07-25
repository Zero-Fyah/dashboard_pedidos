"""Tests de inventario/comparacion.py — fórmula de negocio (DEC-041).

`comparar()` y `anomalias_layout()` son funciones puras sobre DataFrames:
se testean directo, sin tocar Excel ni SQLite. `calcular_vendido_no_alistado()`
va contra la DB y ya tiene cobertura de integración.

Los números de los fixtures están elegidos para que cada aserción se
pueda verificar a mano, no para parecerse al volumen real.
"""

import pandas as pd
import pytest

from comun import familia_de
from inventario.comparacion import anomalias_layout, comparar
from inventario.layout import TIPO_ALTURA, TIPO_PASO, TIPO_PICKING
from inventario.normalizador import filtrar_alcance_admin, marcar_averias

pytestmark = pytest.mark.unit


def _admin(filas):
    return pd.DataFrame(
        filas,
        columns=[
            "id_especificacion",
            "referencia",
            "inventario",
            "categoria",
            "almacen",
        ],
    )


ADMIN = _admin(
    [
        ("1001", "PA01", 100, "Juguetes y entretenimiento", "Bogotá"),
        # Segunda especificación de la misma referencia: debe sumarse.
        ("1002", "PA01", 50, "Juguetes y entretenimiento", "Bogotá"),
        ("1003", "PJ91", 200, "Ropa", "Bogotá"),
        ("1004", "PJ91 AVERIA", 5, "Outlet %", "Bogotá"),
    ]
)


def _bochica(filas):
    return pd.DataFrame(filas, columns=["id_especificacion", "ubicacion", "cantidad", "tipo"])


BOCHICA = _bochica(
    [
        ("1001", "A_1_1", 40, TIPO_PICKING),
        ("1002", "A_2_1", 20, TIPO_PICKING),
        ("1001", "A_1_5", 30, TIPO_ALTURA),
        ("1003", "H_1_1", 300, TIPO_PICKING),
        ("1003", "H_1_5", 90, TIPO_ALTURA),
        ("1004", "D_1_3", 7, TIPO_PICKING),
        ("1001", "B_25_2", 11, TIPO_PASO),
    ]
)

VENDIDO = pd.DataFrame({"referencia": ["PA01", "PJ91"], "vendido_no_alistado": [10, 25]})


@pytest.fixture
def resultado() -> pd.DataFrame:
    return comparar(ADMIN, BOCHICA, VENDIDO).set_index("referencia")


def test_disponible_venta_suma_especificaciones_de_la_misma_referencia(resultado):
    """PA01 tiene 2 id_especificacion (100 + 50): se agrega por referencia."""
    assert resultado.loc["PA01", "disponible_venta"] == 150


def test_inventario_teorico_suma_vendido_no_alistado(resultado):
    assert resultado.loc["PA01", "inventario_teorico"] == 160  # 150 + 10
    assert resultado.loc["PJ91", "inventario_teorico"] == 225  # 200 + 25


def test_bochica_se_separa_en_altura_picking_y_paso(resultado):
    """El total ya no se mezcla: cada tipo de ubicación es su propia columna."""
    assert resultado.loc["PA01", "bochica_picking"] == 60  # 40 + 20
    assert resultado.loc["PA01", "bochica_altura"] == 30
    assert resultado.loc["PA01", "bochica_paso"] == 11


def test_picking_estimado_descuenta_la_altura_confiable(resultado):
    """picking_estimado = teórico − altura (DEC-041)."""
    assert resultado.loc["PA01", "picking_estimado"] == 130  # 160 − 30
    assert resultado.loc["PJ91", "picking_estimado"] == 135  # 225 − 90


def test_diferencia_mide_el_sesgo_de_picking(resultado):
    """diferencia = picking reportado − picking estimado."""
    assert resultado.loc["PA01", "diferencia"] == -70  # 60 − 130
    assert resultado.loc["PJ91", "diferencia"] == 165  # 300 − 135


def test_paso_montacarga_no_entra_en_la_formula(resultado):
    """Las 11 unidades del paso se reportan pero no mueven picking ni altura."""
    fila = resultado.loc["PA01"]
    assert fila["bochica_paso"] == 11
    assert fila["picking_estimado"] == fila["inventario_teorico"] - fila["bochica_altura"]
    assert fila["diferencia"] == fila["bochica_picking"] - fila["picking_estimado"]


def test_picking_estimado_negativo_es_la_alerta(resultado):
    """Si el teórico no cubre ni la altura, la referencia queda en negativo."""
    admin = _admin([("1003", "PJ91", 10, "Ropa", "Bogotá")])
    bochica = _bochica([("1003", "H_1_5", 500, TIPO_ALTURA)])
    sin_vendido = pd.DataFrame({"referencia": [], "vendido_no_alistado": []})
    salida = comparar(admin, bochica, sin_vendido).set_index("referencia")
    assert salida.loc["PJ91", "picking_estimado"] == -490


def test_referencia_sin_stock_en_bochica_queda_con_ceros_no_nan(resultado):
    """La avería PJ91 AVERIA existe en admin y en Bochica; PA01 no tiene faltantes."""
    admin = _admin([("1005", "PC10", 70, "Comederos bebederos", "Bogotá")])
    salida = comparar(admin, _bochica([]), VENDIDO).set_index("referencia")
    assert salida.loc["PC10", "bochica_altura"] == 0
    assert salida.loc["PC10", "bochica_picking"] == 0
    assert salida.loc["PC10", "vendido_no_alistado"] == 0
    assert not salida.isna().any().any()


def test_familia_sale_de_los_dos_primeros_caracteres(resultado):
    assert resultado.loc["PA01", "familia"] == "PA"
    assert resultado.loc["PJ91", "familia"] == "PJ"
    # La avería hereda la familia del producto de origen (DEC-041)
    assert resultado.loc["PJ91 AVERIA", "familia"] == "PJ"


def test_averia_se_marca_y_se_agrega_como_referencia_propia(resultado):
    """PJ91 y PJ91 AVERIA son referencias distintas: no se mezclan."""
    assert resultado.loc["PJ91 AVERIA", "es_averia"]
    assert not resultado.loc["PJ91", "es_averia"]
    assert resultado.loc["PJ91 AVERIA", "disponible_venta"] == 5
    assert resultado.loc["PJ91", "disponible_venta"] == 200


def test_comparar_exige_la_clasificacion_del_layout():
    """Sin columna `tipo` el cálculo sería el naive viejo: falla explícito."""
    sin_tipo = BOCHICA.drop(columns=["tipo"])
    with pytest.raises(ValueError, match="clasificar_ubicaciones"):
        comparar(ADMIN, sin_tipo, VENDIDO)


def test_bochica_sin_match_en_catalogo_queda_fuera():
    """Códigos legados fuera de catálogo: inner join deliberado (DEC-039)."""
    bochica = _bochica([("9999", "A_1_1", 1000, TIPO_PICKING)])
    salida = comparar(ADMIN, bochica, VENDIDO)
    assert salida["bochica_picking"].sum() == 0


# ─────────────────────────────────────────────
# Alcance del catálogo admin
# ─────────────────────────────────────────────


def test_filtrar_alcance_excluye_arena_y_otros_almacenes():
    """`Arena` = recibida por peso (100% fuera del layout); solo Bogotá (DEC-041)."""
    admin = _admin(
        [
            ("1001", "PA01", 100, "Juguetes y entretenimiento", "Bogotá"),
            ("2001", "PRA78", 999, "Arena", "Bogotá"),
            ("3001", "PA02", 50, "Juguetes y entretenimiento", "Medellin"),
        ]
    )
    filtrado = filtrar_alcance_admin(admin)
    assert list(filtrado["referencia"]) == ["PA01"]


def test_filtrar_alcance_no_usa_el_prefijo_de_familia():
    """PRA (Arena) sale y PR11 (Areneras) queda: el discriminador es la categoría."""
    admin = _admin(
        [
            ("2001", "PRA78", 999, "Arena", "Bogotá"),
            ("2002", "PR11", 40, "Areneras palas, bolsas", "Bogotá"),
        ]
    )
    filtrado = filtrar_alcance_admin(admin)
    assert list(filtrado["referencia"]) == ["PR11"]
    assert familia_de("PRA78") == familia_de("PR11") == "PR"


def test_marcar_averias_es_la_union_de_categoria_y_referencia():
    admin = _admin(
        [
            ("1", "PJ91", 1, "Ropa", "Bogotá"),
            ("2", "PJ91 AVERIA", 1, "Outlet %", "Bogotá"),
            ("3", "123FUNDACION", 1, "Outlet %", "Bogotá"),  # Outlet sin ser avería
            ("4", "PB50 AVERÍA", 1, "Ropa", "Bogotá"),  # avería con tilde fuera de Outlet
        ]
    )
    assert list(marcar_averias(admin)) == [False, True, True, True]


# ─────────────────────────────────────────────
# Anomalías de ubicación
# ─────────────────────────────────────────────


def _clasificado(filas):
    return pd.DataFrame(
        filas,
        columns=[
            "id_especificacion",
            "ubicacion",
            "cantidad",
            "tipo",
            "activa",
            "altura",
            "estiba_completa",
        ],
    )


def test_anomalias_distingue_estiba_de_posicion_muerta():
    """SI/NO/NO en altura 2 es estiba; NO/NO/NO en altura 2 es posición muerta."""
    df = _clasificado(
        [
            ("1", "A_1_2", 5, TIPO_PICKING, "NO", 2, True),
            ("2", "A_3_2", 7, TIPO_PICKING, "NO", 2, False),
            ("3", "B_25_1", 9, TIPO_PASO, "NO", 1, False),
        ]
    )
    motivos = anomalias_layout(df).set_index("ubicacion")["motivo"]
    assert motivos["A_1_2"] == "estiba_nivel_superior"
    assert motivos["A_3_2"] == "posicion_no_habilitada"
    assert motivos["B_25_1"] == "paso_montacarga"


def test_anomalias_ignora_la_altura_1_de_una_estiba():
    """En la estiba, el stock DEBE estar en la altura 1: no es anomalía."""
    df = _clasificado(
        [
            ("1", "A_1_1", 500, TIPO_PICKING, "SI", 1, True),
            ("2", "A_2_1", 300, TIPO_PICKING, "SI", 1, False),
            ("3", "A_1_5", 200, TIPO_ALTURA, "SI", 5, False),
        ]
    )
    assert anomalias_layout(df).empty


def test_anomalias_ignora_posiciones_sin_stock():
    df = _clasificado([("1", "A_1_2", 0, TIPO_PICKING, "NO", 2, True)])
    assert anomalias_layout(df).empty


def test_anomalias_ordena_por_cantidad_descendente():
    df = _clasificado(
        [
            ("1", "A_1_2", 5, TIPO_PICKING, "NO", 2, True),
            ("2", "B_25_1", 900, TIPO_PASO, "NO", 1, False),
        ]
    )
    assert list(anomalias_layout(df)["cantidad"]) == [900, 5]
