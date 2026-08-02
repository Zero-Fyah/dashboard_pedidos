"""Detector de campos que el origen DEJA de mostrar — DEC-091.

`vocabulario_nuevo_en_origen` vigila lo que aparece; este vigila lo contrario,
y hacía falta: la SPA dejó de renderizar la tarjeta de entrega para pedidos
viejos y nadie se enteró en meses, porque **ninguna alerta miraba si una cifra
bajaba**. Peor, el UPDATE de persistencia escribía ese vacío encima del dato
bueno.
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from inventario.hallazgos import cobertura_de_campos_cayendo

pytestmark = pytest.mark.unit


def _mes(hace: int) -> str:
    """Primer día de un mes anterior, en el formato del origen."""
    d = date.today().replace(day=1)
    for _ in range(hace):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.isoformat()


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, despachador TEXT, "
        "hora_entrega TEXT, obs_entrega TEXT, conductor TEXT, vehiculo_entrega TEXT, "
        "alistador_pedido TEXT, inspector_pedido TEXT, nombre_empresa TEXT, nit TEXT, "
        "metodo_entrega TEXT, forma_pago TEXT)"
    )
    c.execute("CREATE TABLE subpedidos (id_pedido TEXT, estado TEXT)")
    yield c
    c.close()


def _poblar(con, mes: str, n: int, con_despachador: int) -> None:
    """n pedidos CERRADOS en `mes`, de los cuales `con_despachador` tienen dato."""
    for i in range(n):
        pid = f"{mes}-{i}"
        val = "JUAN" if i < con_despachador else ""
        con.execute(
            "INSERT INTO pedidos (id_pedido, fecha, despachador) VALUES (?,?,?)",
            (pid, mes, val),
        )
        con.execute("INSERT INTO subpedidos VALUES (?, 'completado')", (pid,))


def test_una_caida_grande_se_detecta(con):
    """El caso real: `despachador` pasó de 80% a 0% cuando el origen dejó de
    renderizar la tarjeta de entrega."""
    _poblar(con, _mes(3), 100, 80)
    _poblar(con, _mes(2), 100, 80)
    _poblar(con, _mes(1), 100, 0)

    h = cobertura_de_campos_cayendo(con)

    assert h.cantidad == 1
    assert h.filas.iloc[0]["Campo"] == "pedidos.despachador"
    assert h.filas.iloc[0]["Caída (pp)"] == 80


def test_la_cobertura_estable_no_alerta(con):
    _poblar(con, _mes(3), 100, 80)
    _poblar(con, _mes(2), 100, 78)
    _poblar(con, _mes(1), 100, 82)

    assert cobertura_de_campos_cayendo(con).cantidad == 0


def test_una_variacion_chica_no_alerta(con):
    """El mix de pedidos cambia mes a mes —más `Ruta` un mes que otro— y
    alertar por eso sería la patología de DEC-070/075: un aviso que suena
    siempre enseña a ignorar el log."""
    _poblar(con, _mes(3), 100, 80)
    _poblar(con, _mes(2), 100, 80)
    _poblar(con, _mes(1), 100, 65)  # 15 pp: por debajo del umbral

    assert cobertura_de_campos_cayendo(con).cantidad == 0


def test_los_pedidos_abiertos_no_cuentan(con):
    """Un pedido de ayer sin `hora_entrega` es normal, no una pérdida.
    Incluirlos daría un aviso todos los días."""
    _poblar(con, _mes(3), 100, 80)
    _poblar(con, _mes(2), 100, 80)
    # El mes reciente entero está ABIERTO y sin dato: no debe disparar.
    for i in range(100):
        pid = f"abierto-{i}"
        con.execute(
            "INSERT INTO pedidos (id_pedido, fecha, despachador) VALUES (?,?,'')",
            (pid, _mes(1)),
        )
        con.execute("INSERT INTO subpedidos VALUES (?, 'Pendiente de entrega')", (pid,))

    assert cobertura_de_campos_cayendo(con).cantidad == 0


def test_un_mes_con_pocos_pedidos_no_dispara(con):
    """Con 3 pedidos, que 2 no tengan dato es ruido, no señal."""
    _poblar(con, _mes(3), 100, 80)
    _poblar(con, _mes(2), 100, 80)
    _poblar(con, _mes(1), 3, 0)

    assert cobertura_de_campos_cayendo(con).cantidad == 0


def test_sin_las_tablas_no_explota():
    h = cobertura_de_campos_cayendo(sqlite3.connect(":memory:"))

    assert h.cantidad == 0
    assert isinstance(h.filas, pd.DataFrame)
