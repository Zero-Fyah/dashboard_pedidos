"""Tests de `inventario.cancelaciones` — alistado y luego cancelado (DEC-063)."""

import sqlite3

import pandas as pd
import pytest

from inventario.cancelaciones import calcular_cancelaciones


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT, estado TEXT,
           inicio_alistamiento TEXT, alistamiento_completado TEXT, alistador TEXT,
           estado_cambiado_en TEXT)"""
    )
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT,
           referencia TEXT, cantidad_comprada REAL, monto_final_num REAL)"""
    )
    c.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT)")
    return c


def _sub(c, pid, estado, cierre, cancelado="2026-07-20T10:00:00+00:00"):
    c.execute(
        "INSERT INTO subpedidos VALUES (?,?,?,?,?,?,?)",
        (pid, "S1", estado, "2026-04-01 08:00:00", cierre, "ANA", cancelado),
    )
    c.execute("INSERT INTO pedidos VALUES (?,?)", (pid, "2026-04-01"))


def _linea(c, pid, ref, cantidad=10.0, monto=1000.0):
    c.execute("INSERT INTO lineas_pedido VALUES (?,?,?,?,?)", (pid, "S1", ref, cantidad, monto))


ADMIN = pd.DataFrame({"referencia": ["PA70", "PB10"]})


def test_solo_cuenta_los_cancelados(con):
    _sub(con, "1", "Cancelado", "2026-04-02 15:00:00")
    _linea(con, "1", "PA70")
    _sub(con, "2", "Completado", "2026-04-02 15:00:00")
    _linea(con, "2", "PA70")

    r = calcular_cancelaciones(con, ADMIN)

    assert list(r["id_pedido"]) == ["1"]


def test_exige_alistamiento_cerrado(con):
    """Si el alistamiento no cerró, no hay certeza de que el producto salió."""
    _sub(con, "1", "Cancelado", "-")
    _linea(con, "1", "PA70")

    assert calcular_cancelaciones(con, ADMIN).empty


def test_excluye_lo_que_esta_fuera_del_alcance(con):
    """Sin este filtro, el 79% de las unidades es arena por tonelada y tapa
    por completo la señal real."""
    _sub(con, "1", "Cancelado", "2026-04-02 15:00:00")
    _linea(con, "1", "PRA ARENA TONELADA", cantidad=50_000)
    _sub(con, "2", "Cancelado", "2026-04-02 15:00:00")
    _linea(con, "2", "PA70", cantidad=10)

    r = calcular_cancelaciones(con, ADMIN)

    assert list(r["id_pedido"]) == ["2"]
    assert r["unidades"].sum() == 10


def test_agrega_las_lineas_por_subpedido(con):
    _sub(con, "1", "Cancelado", "2026-04-02 15:00:00")
    _linea(con, "1", "PA70", cantidad=10, monto=1000)
    _linea(con, "1", "PB10", cantidad=5, monto=500)

    r = calcular_cancelaciones(con, ADMIN)

    assert len(r) == 1
    assert r.iloc[0]["lineas"] == 2
    assert r.iloc[0]["unidades"] == 15
    assert r.iloc[0]["valor"] == 1500


def test_calcula_el_rezago_entre_cierre_y_cancelacion(con):
    """Es el dato que desmiente el encuadre intuitivo: el 86% supera el mes."""
    _sub(con, "1", "Cancelado", "2026-04-20 10:00:00", "2026-07-20T10:00:00+00:00")
    _linea(con, "1", "PA70")

    r = calcular_cancelaciones(con, ADMIN)

    assert r.iloc[0]["dias_hasta_cancelacion"] == pytest.approx(91, abs=1)


def test_mezcla_de_zonas_horarias_no_revienta(con):
    """`estado_cambiado_en` viene con zona y `alistamiento_completado` sin
    ella; restarlas crudas lanza TypeError."""
    _sub(con, "1", "Cancelado", "2026-04-20 10:00:00", "2026-07-20T10:00:00+00:00")
    _linea(con, "1", "PA70")

    r = calcular_cancelaciones(con, ADMIN)

    assert r["dias_hasta_cancelacion"].notna().all()


def test_sin_cancelaciones_devuelve_estructura(con):
    r = calcular_cancelaciones(con, ADMIN)

    assert r.empty
    assert "dias_hasta_cancelacion" in r.columns
