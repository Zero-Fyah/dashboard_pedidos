"""Punto de reorden y cantidad sugerida (DEC-071)."""

import math

import pandas as pd
import pytest

from comun import NIVEL_SERVICIO_Z
from comun.reposicion import (
    CV_DEFECTO,
    ESTADO_OK,
    ESTADO_PEDIR_PRONTO,
    ESTADO_PEDIR_YA,
    calcular_reposicion,
)

pytestmark = pytest.mark.unit

LT = 60
OBJ = 30


def _salud(filas):
    """filas: (referencia, demanda_diaria, disponible)."""
    return pd.DataFrame(filas, columns=["referencia", "demanda_diaria", "disponible"])


def _abc(filas):
    """filas: (referencia, clase, cv)."""
    return pd.DataFrame(
        [{"nivel": "referencia", "clave": r, "abc": c, "cv": v} for r, c, v in filas]
    )


def _calc(salud, abc, lead_time=LT):
    return calcular_reposicion(
        salud, abc, lead_time_dias=lead_time, dias_cobertura_objetivo=OBJ
    ).set_index("referencia")


def test_el_punto_de_reorden_es_demanda_por_plazo_mas_seguridad():
    r = _calc(_salud([("PA01", 10.0, 5000.0)]), _abc([("PA01", "A", 0.4)]))

    seguridad = NIVEL_SERVICIO_Z["A"] * 0.4 * 10.0 * math.sqrt(LT)
    assert r.loc["PA01", "stock_seguridad"] == pytest.approx(seguridad)
    assert r.loc["PA01", "punto_reorden"] == pytest.approx(10.0 * LT + seguridad)


def test_la_clase_a_se_protege_mas_que_la_c():
    """Mismo consumo y misma variabilidad: la diferencia es el nivel de
    servicio, que es lo único que cambia entre clases."""
    salud = _salud([("PA01", 10.0, 5000.0), ("PC01", 10.0, 5000.0)])
    r = _calc(salud, _abc([("PA01", "A", 0.4), ("PC01", "C", 0.4)]))

    assert r.loc["PA01", "stock_seguridad"] > r.loc["PC01", "stock_seguridad"]


def test_mas_variabilidad_pide_mas_colchon():
    salud = _salud([("PA01", 10.0, 5000.0), ("PA02", 10.0, 5000.0)])
    r = _calc(salud, _abc([("PA01", "A", 0.2), ("PA02", "A", 0.9)]))

    assert r.loc["PA02", "stock_seguridad"] > r.loc["PA01", "stock_seguridad"]


def test_sin_cv_medido_usa_un_valor_conservador():
    """Asumir cv=0 daría colchón cero justo en las referencias de las que
    menos se sabe."""
    r = _calc(_salud([("PA01", 10.0, 5000.0)]), _abc([("PA01", "A", None)]))

    assert r.loc["PA01", "cv"] == CV_DEFECTO
    assert r.loc["PA01", "stock_seguridad"] > 0


def test_sin_stock_y_con_demanda_es_pedir_ya():
    r = _calc(_salud([("PA01", 10.0, 0.0)]), _abc([("PA01", "A", 0.4)]))

    assert r.loc["PA01", "estado_reposicion"] == ESTADO_PEDIR_YA


def test_por_debajo_del_punto_avisa_antes_del_quiebre():
    """El valor del indicador: avisa mientras todavía hay stock."""
    r = _calc(_salud([("PA01", 10.0, 100.0)]), _abc([("PA01", "A", 0.4)]))

    assert r.loc["PA01", "disponible"] > 0
    assert r.loc["PA01", "estado_reposicion"] == ESTADO_PEDIR_PRONTO


def test_con_stock_de_sobra_no_pide_nada():
    r = _calc(_salud([("PA01", 1.0, 100000.0)]), _abc([("PA01", "A", 0.4)]))

    assert r.loc["PA01", "estado_reposicion"] == ESTADO_OK
    assert r.loc["PA01", "sugerido_pedir"] == 0


def test_la_cantidad_sugerida_nunca_es_negativa():
    """Si ya sobra, no se 'des-pide'."""
    r = _calc(_salud([("PA01", 1.0, 999999.0)]), _abc([("PA01", "A", 0.4)]))

    assert r["sugerido_pedir"].min() >= 0


def test_un_plazo_mas_largo_sube_el_punto_de_reorden():
    """Es la sensibilidad al único parámetro que no sale de los datos: hay
    que poder verla, porque toda la tabla depende de él."""
    salud, abc = _salud([("PA01", 10.0, 5000.0)]), _abc([("PA01", "A", 0.4)])

    corto = _calc(salud, abc, lead_time=30).loc["PA01", "punto_reorden"]
    largo = _calc(salud, abc, lead_time=90).loc["PA01", "punto_reorden"]

    assert largo > corto


def test_las_referencias_sin_demanda_no_aparecen():
    """Un punto de reorden sobre demanda cero es cero: llenaría la tabla de
    filas que no piden ninguna decisión."""
    r = _calc(_salud([("PA01", 0.0, 500.0), ("PB02", 5.0, 10.0)]), _abc([("PB02", "B", 0.3)]))

    assert list(r.index) == ["PB02"]


def test_sin_salud_devuelve_vacio_con_las_columnas():
    r = calcular_reposicion(
        pd.DataFrame(), pd.DataFrame(), lead_time_dias=LT, dias_cobertura_objetivo=OBJ
    )

    assert r.empty
    assert "punto_reorden" in r.columns
