"""Cierre del ciclo de conteo — recuento y ajuste (DEC-070).

**Todo acá es sintético.** `data/conteos/` está vacía: no hay un solo conteo
real todavía, así que estos tests son la única validación posible de la
lógica. Cuando entre el primer conteo hay que contrastar contra él.
"""

import datetime as dt

import pandas as pd
import pytest

from comun import (
    ESTADO_CONTEO_COINCIDE,
    ESTADO_CONTEO_CONFIRMADO,
    ESTADO_CONTEO_DESCARTADO,
    ESTADO_CONTEO_PENDIENTE,
)
from inventario.conteos import evaluar_conteos

pytestmark = pytest.mark.unit

HOY = dt.date(2026, 8, 1)


def _ubicaciones(filas):
    """filas: (ubicacion, id, cantidad_del_sistema_HOY)."""
    return pd.DataFrame(
        [
            {
                "ubicacion": u,
                "id_especificacion": i,
                "cantidad": c,
                "clase": "A",
                "tipo": "Altura",
            }
            for u, i, c in filas
        ]
    )


def _conteo(fecha, cantidad, ubicacion="A_1_4", ident="ID1", quien="ana"):
    return {
        "ubicacion": ubicacion,
        "id_especificacion": ident,
        "fecha": fecha,
        "contado_por": quien,
        "cantidad_contada": cantidad,
        "archivo": "hoja.xlsx",
    }


def _evaluar(conteos, ubicaciones):
    return evaluar_conteos(pd.DataFrame(conteos), ubicaciones, hoy=HOY).set_index("fecha")


def test_un_conteo_que_coincide_no_abre_ciclo():
    r = _evaluar([_conteo("2026-07-20", 100)], _ubicaciones([("A_1_4", "ID1", 100)]))

    assert r.loc["2026-07-20", "estado_ciclo"] == ESTADO_CONTEO_COINCIDE
    assert pd.isna(r.loc["2026-07-20", "dias_abierto"])


def test_una_diferencia_sin_recuento_queda_pendiente():
    # Sistema dice 100 hoy, se contó 80: nadie volvió a contar ni ajustó.
    r = _evaluar([_conteo("2026-07-20", 80)], _ubicaciones([("A_1_4", "ID1", 100)]))

    assert r.loc["2026-07-20", "estado_ciclo"] == ESTADO_CONTEO_PENDIENTE
    assert r.loc["2026-07-20", "dias_abierto"] == 12


def test_una_diferencia_ajustada_vuelve_a_coincidir():
    """DEC-070: NO hay estado "ajuste aplicado" y no puede haberlo — la
    cantidad del sistema es siempre la de hoy. Cuando el ajuste entra, el
    conteo simplemente vuelve a evaluarse como Coincide, y no se distingue
    de "el conteo estaba bien desde el principio"."""
    r = _evaluar([_conteo("2026-07-20", 80)], _ubicaciones([("A_1_4", "ID1", 80)]))

    assert r.loc["2026-07-20", "estado_ciclo"] == ESTADO_CONTEO_COINCIDE


def test_un_recuento_que_repite_la_diferencia_la_confirma():
    """Dos contadores distintos llegan al mismo número y el sistema sigue en
    otro: la diferencia es real y falta ajustarla."""
    r = _evaluar(
        [_conteo("2026-07-20", 80), _conteo("2026-07-25", 80, quien="beto")],
        _ubicaciones([("A_1_4", "ID1", 100)]),
    )

    assert set(r["estado_ciclo"]) == {ESTADO_CONTEO_CONFIRMADO}
    assert r.loc["2026-07-25", "intento"] == 2


def test_un_recuento_que_coincide_descarta_el_primero():
    """El recuento dio lo que dice el sistema: el primer conteo estaba mal.
    No hay nada que ajustar en el inventario — hay algo que revisar en la
    hoja."""
    r = _evaluar(
        [_conteo("2026-07-20", 80), _conteo("2026-07-25", 100, quien="beto")],
        _ubicaciones([("A_1_4", "ID1", 100)]),
    )

    assert r.loc["2026-07-20", "estado_ciclo"] == ESTADO_CONTEO_DESCARTADO
    assert r.loc["2026-07-25", "estado_ciclo"] == ESTADO_CONTEO_COINCIDE


def test_el_intento_se_deriva_no_se_pide():
    """El segundo conteo de una línea ES el recuento. Derivarlo evita un
    campo más que el contador pueda llenar mal."""
    r = _evaluar(
        [
            _conteo("2026-07-20", 80),
            _conteo("2026-07-22", 90, quien="beto"),
            _conteo("2026-07-25", 95, quien="caro"),
        ],
        _ubicaciones([("A_1_4", "ID1", 100)]),
    )

    assert list(r.sort_index()["intento"]) == [1, 2, 3]


def test_lineas_distintas_no_se_mezclan():
    r = evaluar_conteos(
        pd.DataFrame(
            [
                _conteo("2026-07-20", 80, ubicacion="A_1_4"),
                _conteo("2026-07-21", 50, ubicacion="B_2_5", ident="ID2"),
            ]
        ),
        _ubicaciones([("A_1_4", "ID1", 100), ("B_2_5", "ID2", 50)]),
        hoy=HOY,
    ).set_index("ubicacion")

    assert r.loc["A_1_4", "intento"] == 1
    assert r.loc["B_2_5", "intento"] == 1
    assert r.loc["B_2_5", "estado_ciclo"] == ESTADO_CONTEO_COINCIDE


def test_sin_conteos_devuelve_las_columnas_igual():
    """La página lee estas columnas aunque no haya conteos todavía — que es
    exactamente el estado de hoy."""
    r = evaluar_conteos(pd.DataFrame(), _ubicaciones([("A_1_4", "ID1", 100)]), hoy=HOY)

    for columna in ("intento", "estado_ciclo", "dias_abierto"):
        assert columna in r.columns
