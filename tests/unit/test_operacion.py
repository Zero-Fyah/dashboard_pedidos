"""Tests de inventario/operacion.py — tiempos de ciclo (DEC-054).

Lo que más importa verificar es que los intervalos se calculen sobre las
marcas que **realmente** significan lo que se necesita, y que la atribución
por alistador no invente producción donde el trabajo fue compartido.
"""

import sqlite3

import pandas as pd
import pytest

from inventario.operacion import calcular_operacion, productividad_por_alistador

pytestmark = pytest.mark.unit


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT,
           alistador TEXT, inspector TEXT, inicio_alistamiento TEXT,
           inicio_inspeccion TEXT, inspeccion_completada TEXT)"""
    )
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT,
           cantidad_comprada REAL)"""
    )
    yield c
    c.close()


def _sub(con, pid, ia, ii, ic, alistador="ANA", inspector="LUIS", lineas=1, unidades=10):
    con.execute(
        "INSERT INTO subpedidos VALUES (?,?,?,?,?,?,?)",
        (pid, "S1", alistador, inspector, ia, ii, ic),
    )
    for _ in range(lineas):
        con.execute("INSERT INTO lineas_pedido VALUES (?,?,?)", (pid, "S1", unidades))


# ─────────────────────────────────────────────
# Intervalos
# ─────────────────────────────────────────────


def test_los_tres_intervalos_se_calculan_en_horas(con):
    _sub(con, "P1", "2026-03-01 08:00:00", "2026-03-01 12:00:00", "2026-03-01 14:00:00")
    df = calcular_operacion(con).iloc[0]
    assert df["cola_y_picking_h"] == pytest.approx(4.0)
    assert df["inspeccion_h"] == pytest.approx(2.0)
    assert df["ciclo_total_h"] == pytest.approx(6.0)


def test_el_dia_se_toma_del_cierre_de_inspeccion(con):
    """Es cuando el trabajo quedó hecho, que es lo que cuenta la productividad."""
    _sub(con, "P1", "2026-03-01 22:00:00", "2026-03-02 01:00:00", "2026-03-02 03:00:00")
    assert calcular_operacion(con).iloc[0]["dia"] == "2026-03-02"


def test_subpedido_sin_marcas_no_entra(con):
    _sub(con, "P1", "-", "-", "-")
    assert calcular_operacion(con).empty


def test_subpedido_a_medio_proceso_no_entra(con):
    """Sin cierre de inspección no hay ciclo que medir."""
    _sub(con, "P1", "2026-03-01 08:00:00", "2026-03-01 12:00:00", "-")
    assert calcular_operacion(con).empty


def test_marcas_fuera_de_orden_se_descartan(con):
    """4 casos reales de 27.286: arrastrarlos rompería cualquier promedio."""
    _sub(con, "P1", "2026-03-05 08:00:00", "2026-03-01 12:00:00", "2026-03-01 14:00:00")
    _sub(con, "P2", "2026-03-01 08:00:00", "2026-03-01 12:00:00", "2026-03-01 14:00:00")
    df = calcular_operacion(con)
    assert list(df["id_pedido"]) == ["P2"]


def test_la_carga_del_subpedido_se_agrega(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        lineas=3,
        unidades=5,
    )
    fila = calcular_operacion(con).iloc[0]
    assert fila["lineas"] == 3
    assert fila["unidades"] == 15


# ─────────────────────────────────────────────
# Atribución por alistador
# ─────────────────────────────────────────────


def test_cuenta_los_alistadores_de_un_subpedido(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="ANA,BETO,CARLA",
    )
    assert calcular_operacion(con).iloc[0]["n_alistadores"] == 3


def test_participaciones_cuenta_a_todos_los_que_intervinieron(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="ANA,BETO",
    )
    prod = productividad_por_alistador(calcular_operacion(con)).set_index("alistador")
    assert prod.loc["ANA", "participaciones"] == 1
    assert prod.loc["BETO", "participaciones"] == 1


def test_lo_compartido_no_se_atribuye_a_nadie(con):
    """Sumarlo a cada participante inflaría el total; repartirlo en partes
    iguales asumiría algo que el dato no dice."""
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="ANA,BETO",
        lineas=4,
        unidades=10,
    )
    prod = productividad_por_alistador(calcular_operacion(con)).set_index("alistador")
    assert prod.loc["ANA", "exclusivos"] == 0
    assert prod.loc["ANA", "lineas"] == 0
    assert prod.loc["BETO", "lineas"] == 0


def test_lo_exclusivo_si_se_atribuye(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="ANA",
        lineas=4,
        unidades=10,
    )
    prod = productividad_por_alistador(calcular_operacion(con)).set_index("alistador")
    assert prod.loc["ANA", "exclusivos"] == 1
    assert prod.loc["ANA", "lineas"] == 4
    assert prod.loc["ANA", "unidades"] == 40


def test_espacios_alrededor_del_nombre_no_duplican_al_operario(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="ANA, BETO",
    )
    _sub(
        con,
        "P2",
        "2026-03-02 08:00:00",
        "2026-03-02 12:00:00",
        "2026-03-02 14:00:00",
        alistador="BETO,ANA",
    )
    prod = productividad_por_alistador(calcular_operacion(con)).set_index("alistador")
    assert len(prod) == 2
    assert prod.loc["BETO", "participaciones"] == 2


def test_sin_alistador_no_rompe(con):
    _sub(
        con,
        "P1",
        "2026-03-01 08:00:00",
        "2026-03-01 12:00:00",
        "2026-03-01 14:00:00",
        alistador="-",
    )
    assert productividad_por_alistador(calcular_operacion(con)).empty


def test_sin_datos_devuelve_vacio_con_las_columnas(con):
    prod = productividad_por_alistador(pd.DataFrame(columns=["alistador"]))
    assert prod.empty
    assert "participaciones" in prod.columns
