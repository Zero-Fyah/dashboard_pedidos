"""Tests de `inventario.ubicaciones` — línea SKU-posición (DEC-057)."""

import pandas as pd
import pytest

from inventario.ubicaciones import SIN_ROTACION, calcular_ubicaciones, resumen_cobertura


@pytest.fixture
def bochica():
    """Dos posiciones de altura y una de picking, con densidad distinta."""
    return pd.DataFrame(
        [
            # A_1_5: dos ID distintos -> dos líneas SKU-posición
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "1",
                "referencia": "PA70",
                "cantidad": 10,
                "tipo": "Altura",
                "rack": "A",
                "posicion": 1,
                "altura": 5,
            },
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "2",
                "referencia": "PA70",
                "cantidad": 5,
                "tipo": "Altura",
                "rack": "A",
                "posicion": 1,
                "altura": 5,
            },
            # A_2_6: un solo ID, sin ventas
            {
                "ubicacion": "A_2_6",
                "id_especificacion": "3",
                "referencia": "PB10",
                "cantidad": 100,
                "tipo": "Altura",
                "rack": "A",
                "posicion": 2,
                "altura": 6,
            },
            {
                "ubicacion": "B_1_1",
                "id_especificacion": "1",
                "referencia": "PA70",
                "cantidad": 4,
                "tipo": "Picking",
                "rack": "B",
                "posicion": 1,
                "altura": 1,
            },
        ]
    )


@pytest.fixture
def abc():
    """ABC global: el ID 1 es clase A, el 2 clase C. El 3 no vendió nunca."""
    return pd.DataFrame(
        [
            {"nivel": "id_global", "clave": "1", "abc": "A", "xyz": "X"},
            {"nivel": "id_global", "clave": "2", "abc": "C", "xyz": "Z"},
            # Ruido del nivel jerárquico: NO debe usarse para priorizar.
            {"nivel": "id", "clave": "2", "abc": "A", "xyz": "X"},
        ]
    )


@pytest.fixture
def admin():
    return pd.DataFrame(
        [
            {"id_especificacion": "1", "precio": 1000},
            {"id_especificacion": "2", "precio": 50},
            {"id_especificacion": "3", "precio": 20},
        ]
    )


@pytest.fixture
def salud():
    return pd.DataFrame([{"referencia": "PA70", "dias_sin_salida": 3}])


def test_una_fila_por_id_y_posicion(bochica, abc, admin, salud):
    """La unidad es (ubicación, ID): la posición multi-referencia da varias líneas."""
    r = calcular_ubicaciones(bochica, abc, admin, salud)

    assert len(r) == 4
    assert set(r.columns) >= {"ubicacion", "id_especificacion", "clase", "valor_linea"}


def test_usa_el_abc_global_no_el_jerarquico(bochica, abc, admin, salud):
    """El ID 2 es A dentro de su referencia pero C contra todo el almacén.

    Priorizar con el jerárquico inflaría la clase A —cada referencia aporta
    las suyas— y la cola de conteo dejaría de discriminar.
    """
    r = calcular_ubicaciones(bochica, abc, admin, salud)
    clase = r.set_index(["ubicacion", "id_especificacion"])["clase"]

    assert clase[("A_1_5", "2")] == "C"


def test_id_sin_ventas_no_es_clase_c(bochica, abc, admin, salud):
    """Sin rotación es una categoría propia: es el caso que la Fase 3 existe para cubrir."""
    r = calcular_ubicaciones(bochica, abc, admin, salud)
    clase = r.set_index(["ubicacion", "id_especificacion"])["clase"]

    assert clase[("A_2_6", "3")] == SIN_ROTACION


def test_valor_linea_es_cantidad_por_precio(bochica, abc, admin, salud):
    r = calcular_ubicaciones(bochica, abc, admin, salud)
    valor = r.set_index(["ubicacion", "id_especificacion"])["valor_linea"]

    assert valor[("A_1_5", "1")] == 10 * 1000
    assert valor[("A_2_6", "3")] == 100 * 20


def test_la_posicion_hereda_la_clase_mas_alta(bochica, abc, admin, salud):
    """Una sola línea A eleva toda la posición: la visita física es a la posición."""
    r = calcular_ubicaciones(bochica, abc, admin, salud)
    pos = r.set_index(["ubicacion", "id_especificacion"])["clase_posicion"]

    # A_1_5 mezcla un A y un C -> la posición entera es A.
    assert pos[("A_1_5", "1")] == "A"
    assert pos[("A_1_5", "2")] == "A"


def test_prioridad_ordena_por_clase_y_luego_valor(bochica, abc, admin, salud):
    """La clase manda en bloque; dentro del bloque manda el valor."""
    r = calcular_ubicaciones(bochica, abc, admin, salud).sort_values("prioridad")

    # A_1_5 (10.250) pesa más que B_1_1 (4.000), ambas posiciones clase A.
    # La sin rotación va última aunque valga 2.000.
    assert list(r["ubicacion"]) == ["A_1_5", "A_1_5", "B_1_1", "A_2_6"]
    assert r.iloc[-1]["clase"] == SIN_ROTACION
    assert list(r["prioridad"]) == [1, 2, 3, 4]


def test_las_lineas_de_una_posicion_no_se_parten(bochica, abc, admin, salud):
    """Quien sube a una estiba cuenta todo lo que hay en ella, en una visita.

    Ordenar por valor de línea suelto intercalaría líneas de otras
    posiciones entre las dos de A_1_5 (10.000 y 250, con B_1_1 en 4.000 en
    medio) y obligaría a subir dos veces al mismo sitio.
    """
    r = calcular_ubicaciones(bochica, abc, admin, salud).sort_values("prioridad")

    for ubicacion in r["ubicacion"].unique():
        posiciones = r.index[r["ubicacion"] == ubicacion].tolist()
        assert posiciones == list(range(min(posiciones), max(posiciones) + 1)), (
            f"las líneas de {ubicacion} quedaron partidas"
        )


def test_precio_faltante_no_rompe_el_valor(bochica, abc, salud):
    """Un ID sin precio en el catálogo vale 0, no NaN: NaN envenenaría el orden."""
    r = calcular_ubicaciones(
        bochica, abc, pd.DataFrame(columns=["id_especificacion", "precio"]), salud
    )

    assert r["valor_linea"].notna().all()
    assert (r["valor_linea"] == 0).all()


def test_bochica_vacio_devuelve_estructura(abc, admin, salud):
    """El caso 'todavía no hay descarga' no puede explotar."""
    r = calcular_ubicaciones(pd.DataFrame(), abc, admin, salud)

    assert r.empty
    assert "prioridad" in r.columns


def test_resumen_no_cuenta_las_posiciones_vacias(bochica, abc, admin, salud):
    """El error de dimensionamiento que corrige DEC-057.

    El layout declara 3 posiciones de altura activas pero solo 2 tienen
    inventario. Estimar el trabajo como posiciones × densidad contaría la
    vacía.
    """
    layout = pd.DataFrame(
        [
            {"ubicacion": "A_1_5", "tipo": "Altura", "activa": "SI"},
            {"ubicacion": "A_2_6", "tipo": "Altura", "activa": "SI"},
            {"ubicacion": "A_3_7", "tipo": "Altura", "activa": "SI"},
            {"ubicacion": "B_1_1", "tipo": "Picking", "activa": "SI"},
            {"ubicacion": "Z_9_9", "tipo": "Altura", "activa": "NO"},
        ]
    )
    r = calcular_ubicaciones(bochica, abc, admin, salud)
    res = resumen_cobertura(r, layout).set_index("tipo")

    assert res.loc["Altura", "posiciones_activas"] == 3
    assert res.loc["Altura", "posiciones_ocupadas"] == 2
    assert res.loc["Altura", "posiciones_vacias"] == 1
    assert res.loc["Altura", "lineas"] == 3
    assert res.loc["Altura", "lineas_por_ocupada"] == 1.5
