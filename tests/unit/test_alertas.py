"""Tests de `inventario.alertas` — centro de excepciones (DEC-059)."""

import pandas as pd
import pytest

from inventario.alertas import ALTA, CRITICA, MEDIA, generar_alertas


@pytest.fixture
def salud():
    return pd.DataFrame(
        [
            {
                "referencia": "PA01",
                "estado": "Quiebre",
                "demanda_90d": 500,
                "disponible": 0,
                "dias_sin_salida": 2,
            },
            {
                "referencia": "PB02",
                "estado": "Quiebre",
                "demanda_90d": 100,
                "disponible": 0,
                "dias_sin_salida": 5,
            },
            {
                "referencia": "PC03",
                "estado": "Quiebre",
                "demanda_90d": 10,
                "disponible": 0,
                "dias_sin_salida": 9,
            },
            {
                "referencia": "PD04",
                "estado": "Normal",
                "demanda_90d": 0,
                "disponible": 900,
                "dias_sin_salida": 300,
            },
        ]
    )


@pytest.fixture
def abc():
    return pd.DataFrame(
        [
            {"nivel": "referencia", "clave": "PA01", "abc": "A"},
            {"nivel": "referencia", "clave": "PB02", "abc": "B"},
            {"nivel": "referencia", "clave": "PC03", "abc": "C"},
            {"nivel": "referencia", "clave": "PD04", "abc": "A"},
        ]
    )


VACIO = pd.DataFrame()


def test_el_quiebre_de_clase_a_es_critico(salud, abc):
    r = generar_alertas(salud, VACIO, VACIO, VACIO, abc)
    quiebres = r[r["tipo"] == "Quiebre de stock"].set_index("entidad")

    assert quiebres.loc["PA01", "severidad"] == CRITICA
    assert quiebres.loc["PB02", "severidad"] == ALTA


def test_el_quiebre_de_clase_c_no_alerta(salud, abc):
    """Diluiría los que sí compiten por la atención de alguien."""
    r = generar_alertas(salud, VACIO, VACIO, VACIO, abc)

    assert "PC03" not in set(r["entidad"])


def test_el_sobrante_se_agrega_en_una_sola_alerta():
    """El primer diseño listaba una por referencia: 174 filas, el 83% del centro.

    Un sobrante es una condición conocida con página propia, no una
    excepción del día, y atenderlo es ir a contar esa posición — que ya
    prioriza la cola de conteo.
    """
    comparacion = pd.DataFrame(
        [{"referencia": f"P{i:02d}", "sobrante_altura": 100 * (i + 1)} for i in range(30)]
    )
    r = generar_alertas(VACIO, comparacion, VACIO, VACIO, VACIO)
    sobrantes = r[r["tipo"] == "Sobrante físico"]

    assert len(sobrantes) == 1
    assert sobrantes.iloc[0]["valor"] == sum(100 * (i + 1) for i in range(30))


def test_la_cobertura_de_conteo_tambien_es_agregada():
    ubicaciones = pd.DataFrame(
        [{"ubicacion": f"A_{i}_5", "tipo": "Altura", "clase_posicion": "A"} for i in range(50)]
    )
    r = generar_alertas(VACIO, VACIO, ubicaciones, VACIO, VACIO)
    cobertura = r[r["tipo"] == "Cobertura de conteo"]

    assert len(cobertura) == 1
    assert cobertura.iloc[0]["valor"] == 50


def test_las_posiciones_ya_contadas_salen_de_la_cobertura():
    ubicaciones = pd.DataFrame(
        [
            {"ubicacion": "A_1_5", "tipo": "Altura", "clase_posicion": "A"},
            {"ubicacion": "A_2_5", "tipo": "Altura", "clase_posicion": "B"},
        ]
    )
    conteos = pd.DataFrame([{"ubicacion": "A_1_5", "exacta": 1, "clase": "A"}])
    r = generar_alertas(VACIO, VACIO, ubicaciones, conteos, VACIO)

    assert r[r["tipo"] == "Cobertura de conteo"].iloc[0]["valor"] == 1


def test_la_clase_heredada_cuenta_para_la_cobertura():
    """`A (por referencia)` es clase alta: startswith, no igualdad."""
    ubicaciones = pd.DataFrame(
        [{"ubicacion": "A_1_5", "tipo": "Altura", "clase_posicion": "A (por referencia)"}]
    )
    r = generar_alertas(VACIO, VACIO, ubicaciones, pd.DataFrame(), VACIO)

    assert len(r[r["tipo"] == "Cobertura de conteo"]) == 1


def test_la_discrepancia_de_clase_a_es_critica():
    """El plan exige doble verificación para cualquier ajuste de clase A."""
    conteos = pd.DataFrame(
        [
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "1",
                "fecha": "2026-07-27",
                "clase": "A",
                "exacta": 0,
                "diferencia": -20,
                "hallazgo": "Faltante",
                "contado_por": "ANA",
            },
            {
                "ubicacion": "A_2_6",
                "id_especificacion": "2",
                "fecha": "2026-07-27",
                "clase": "C",
                "exacta": 0,
                "diferencia": 40,
                "hallazgo": "Sobrante",
                "contado_por": "ANA",
            },
        ]
    )
    r = generar_alertas(VACIO, VACIO, VACIO, conteos, VACIO).set_index("entidad")

    assert r.loc["A_1_5", "severidad"] == CRITICA
    assert r.loc["A_2_6", "severidad"] == ALTA


def test_los_conteos_dentro_de_tolerancia_no_alertan():
    conteos = pd.DataFrame(
        [
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "1",
                "fecha": "2026-07-27",
                "clase": "A",
                "exacta": 1,
                "diferencia": 0,
                "hallazgo": "Coincide",
                "contado_por": "ANA",
            }
        ]
    )
    r = generar_alertas(VACIO, VACIO, VACIO, conteos, VACIO)

    assert r[r["tipo"] == "Discrepancia de conteo"].empty


def test_la_frescura_vencida_es_critica():
    """Invalida a las demás alertas: decidir con datos viejos es peor que no decidir."""
    r = generar_alertas(
        VACIO,
        VACIO,
        VACIO,
        VACIO,
        VACIO,
        frescura={"datos_desactualizados": 1, "fuente_mas_vieja_h": 9.5},
    )

    assert r.iloc[0]["severidad"] == CRITICA
    assert r.iloc[0]["tipo"] == "Datos desactualizados"


def test_la_frescura_al_dia_no_alerta():
    r = generar_alertas(
        VACIO,
        VACIO,
        VACIO,
        VACIO,
        VACIO,
        frescura={"datos_desactualizados": 0, "fuente_mas_vieja_h": 1.0},
    )

    assert r.empty


def test_el_stock_inmovilizado_alerta_como_media(salud, abc):
    r = generar_alertas(salud, VACIO, VACIO, VACIO, abc)
    quieto = r[r["tipo"] == "Sin movimiento"]

    assert len(quieto) == 1
    assert quieto.iloc[0]["entidad"] == "PD04"
    assert quieto.iloc[0]["severidad"] == MEDIA


def test_ordena_por_severidad_y_luego_magnitud(salud, abc):
    r = generar_alertas(
        salud,
        VACIO,
        VACIO,
        VACIO,
        abc,
        frescura={"datos_desactualizados": 1, "fuente_mas_vieja_h": 5.0},
    )
    severidades = list(r["severidad"])

    assert severidades == sorted(severidades, key=lambda s: [CRITICA, ALTA, MEDIA].index(s))


def test_sin_nada_fuera_de_lo_normal_devuelve_vacio():
    r = generar_alertas(VACIO, VACIO, VACIO, VACIO, VACIO)

    assert r.empty
    assert "severidad" in r.columns


def test_las_claves_son_estables_entre_corridas(salud, abc):
    """De ellas depende que la persistencia sepa que es la MISMA alerta.

    Si cambiaran entre corridas, `primera_vez` se reiniciaría y toda alerta
    parecería recién nacida — la antigüedad dejaría de medir nada.
    """
    a = generar_alertas(salud, VACIO, VACIO, VACIO, abc)
    b = generar_alertas(salud, VACIO, VACIO, VACIO, abc)

    assert list(a["clave"]) == list(b["clave"])
    assert a["clave"].is_unique
