"""Tests de inventario/clasificacion.py — ABC-XYZ jerárquico (DEC-050/053).

Los números están armados para que el Pareto y el coeficiente de variación
se puedan verificar a mano.
"""

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from inventario.clasificacion import (
    NIVEL_FAMILIA,
    NIVEL_ID,
    NIVEL_REFERENCIA,
    SIN_CONSUMO,
    VENTANA_MESES,
    _meses_completos,
    calcular_clasificacion,
    construir_puente_especificaciones,
)

pytestmark = pytest.mark.unit

# Mes en curso: 2026-07 → los 6 completos son 2026-01..2026-06.
HOY = dt.date(2026, 7, 26)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT,
           referencia TEXT, codigo_barras TEXT, almacen TEXT,
           cantidad_comprada REAL, monto_final_num REAL)"""
    )
    c.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT)")
    c.execute("CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT, estado TEXT)")
    yield c
    c.close()


def _admin(referencias, especificaciones=None):
    """referencias: lista de refs (un ID por ref) o de tuplas
    (ref, codigo_barras, id_especificacion, especificacion)."""
    if especificaciones is not None:
        return pd.DataFrame(
            especificaciones,
            columns=["referencia", "codigo_barras", "id_especificacion", "especificacion"],
        )
    return pd.DataFrame(
        [(r, f"CB{i}", f"E{i}", f"spec {r}") for i, r in enumerate(referencias)],
        columns=["referencia", "codigo_barras", "id_especificacion", "especificacion"],
    )


_n = 0


def _venta(con, ref, mes, unidades, monto, estado="Completado", almacen="Bogotá", cb="CB0"):
    """mes: 'YYYY-MM'. Cada venta va en un pedido propio."""
    global _n
    _n += 1
    pid = f"P{_n}"
    con.execute("INSERT INTO pedidos VALUES (?,?)", (pid, f"{mes}-15"))
    con.execute("INSERT INTO subpedidos VALUES (?,?,?)", (pid, "S1", estado))
    con.execute(
        "INSERT INTO lineas_pedido VALUES (?,?,?,?,?,?,?)",
        (pid, "S1", ref, cb, almacen, unidades, monto),
    )


def _refs(df):
    """Solo el nivel de referencia, indexado por clave."""
    return df[df.nivel == NIVEL_REFERENCIA].set_index("clave")


# ─────────────────────────────────────────────
# Ventana
# ─────────────────────────────────────────────


def test_la_ventana_excluye_el_mes_en_curso():
    meses = _meses_completos(HOY, VENTANA_MESES)
    assert meses == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert "2026-07" not in meses


def test_la_ventana_cruza_el_cambio_de_ano():
    assert _meses_completos(dt.date(2026, 2, 10), 3) == ["2025-11", "2025-12", "2026-01"]


def test_venta_del_mes_en_curso_no_cuenta(con):
    _venta(con, "PA01", "2026-07", 10, 1000)
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "abc"] == SIN_CONSUMO


# ─────────────────────────────────────────────
# ABC
# ─────────────────────────────────────────────


def test_pareto_asigna_a_b_c_por_acumulado(con):
    """Las tres en la MISMA familia: 800/150/50 → acumulados 80/95/100."""
    _venta(con, "PA01", "2026-01", 1, 800)
    _venta(con, "PA02", "2026-01", 1, 150)
    _venta(con, "PA03", "2026-01", 1, 50)
    df = _refs(calcular_clasificacion(_admin(["PA01", "PA02", "PA03"]), con, hoy=HOY))
    assert df.loc["PA01", "abc"] == "A"
    assert df.loc["PA02", "abc"] == "B"
    assert df.loc["PA03", "abc"] == "C"


def test_abc_usa_el_ingreso_real_no_las_unidades(con):
    """Muchas unidades baratas no hacen una A."""
    _venta(con, "BARATO", "2026-01", 10_000, 100)
    _venta(con, "CARO", "2026-01", 1, 900)
    df = _refs(calcular_clasificacion(_admin(["BARATO", "CARO"]), con, hoy=HOY))
    assert df.loc["CARO", "abc"] == "A"
    assert df.loc["BARATO", "abc"] != "A"


def test_sin_consumo_no_se_fuerza_a_c(con):
    """No aporta poco: no aporta nada. Meterla en C diluiría la clase."""
    _venta(con, "PA01", "2026-01", 1, 1000)
    df = _refs(calcular_clasificacion(_admin(["PA01", "PZ99"]), con, hoy=HOY))
    assert df.loc["PZ99", "abc"] == SIN_CONSUMO
    assert pd.isna(df.loc["PZ99", "pct_acumulado"])
    assert pd.isna(df.loc["PZ99", "celda"])


def test_cancelado_no_es_consumo(con):
    _venta(con, "PA01", "2026-01", 5, 5000, estado="Cancelado")
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "abc"] == SIN_CONSUMO


def test_otro_almacen_no_cuenta(con):
    _venta(con, "PA01", "2026-01", 5, 5000, almacen="Medellin")
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "abc"] == SIN_CONSUMO


# ─────────────────────────────────────────────
# XYZ
# ─────────────────────────────────────────────


def test_demanda_pareja_todos_los_meses_es_x(con):
    for mes in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"):
        _venta(con, "PA01", mes, 100, 1000)
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "cv"] == pytest.approx(0.0)
    assert df.loc["PA01", "xyz"] == "X"
    assert df.loc["PA01", "meses_con_venta"] == 6


def test_venta_en_un_solo_mes_es_z(con):
    """Cinco meses en cero: demanda intermitente, que es lo que XYZ captura."""
    _venta(con, "PA01", "2026-03", 600, 1000)
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "cv"] > 1.0
    assert df.loc["PA01", "xyz"] == "Z"
    assert df.loc["PA01", "meses_con_venta"] == 1


def test_meses_con_venta_permite_distinguir_producto_nuevo(con):
    """Sin esta columna, un producto nuevo y uno errático son idénticos."""
    for mes in ("2026-05", "2026-06"):
        _venta(con, "NUEVO", mes, 100, 1000)
    df = _refs(calcular_clasificacion(_admin(["NUEVO"]), con, hoy=HOY))
    assert df.loc["NUEVO", "xyz"] == "Z"
    assert df.loc["NUEVO", "meses_con_venta"] == 2


def test_variabilidad_intermedia_es_y(con):
    # Media 100, desviación poblacional 60 → CV 0,6.
    for mes, u in zip(
        ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"),
        (40, 40, 40, 160, 160, 160),
        strict=True,
    ):
        _venta(con, "PA01", mes, u, 1000)
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "cv"] == pytest.approx(0.6)
    assert df.loc["PA01", "xyz"] == "Y"


# ─────────────────────────────────────────────
# Celda y política
# ─────────────────────────────────────────────


def test_celda_combina_ambas_clases_y_trae_politica(con):
    for mes in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"):
        _venta(con, "PA01", mes, 100, 10_000)
    df = _refs(calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY))
    assert df.loc["PA01", "celda"] == "AX"
    assert "automática" in df.loc["PA01", "politica"]


def test_sin_ventas_en_absoluto_no_rompe(con):
    """El caso de una base recién creada o un almacén sin movimiento."""
    df = calcular_clasificacion(_admin(["PA01", "PB02"]), con, hoy=HOY)
    refs = _refs(df)
    assert len(refs) == 2
    assert set(refs["abc"]) == {SIN_CONSUMO}
    assert df["celda"].isna().all()


# ─────────────────────────────────────────────
# Jerarquía (DEC-053)
# ─────────────────────────────────────────────


def test_los_tres_niveles_se_calculan(con):
    _venta(con, "PA01", "2026-01", 10, 1000)
    df = calcular_clasificacion(_admin(["PA01"]), con, hoy=HOY)
    assert set(df["nivel"]) == {NIVEL_FAMILIA, NIVEL_REFERENCIA, NIVEL_ID}


def test_el_pareto_de_referencias_corre_dentro_de_su_familia(con):
    """Lo que hace jerárquico el análisis: una referencia chica en el total
    puede ser A dentro de su familia."""
    _venta(con, "PA01", "2026-01", 1, 100_000)  # familia PA, dominante global
    _venta(con, "PB01", "2026-01", 1, 100)  # familia PB, única de la suya
    df = _refs(calcular_clasificacion(_admin(["PA01", "PB01"]), con, hoy=HOY))
    assert df.loc["PB01", "abc"] == "A", "es el 100% del ingreso de su familia"
    assert df.loc["PB01", "pct_valor"] == pytest.approx(100.0)


def test_los_porcentajes_suman_cien_dentro_de_cada_familia(con):
    for ref, monto in (("PA01", 700), ("PA02", 300), ("PB01", 500)):
        _venta(con, ref, "2026-01", 1, monto)
    df = calcular_clasificacion(_admin(["PA01", "PA02", "PB01"]), con, hoy=HOY)
    refs = df[(df.nivel == NIVEL_REFERENCIA) & (df.abc != SIN_CONSUMO)]
    for familia, grupo in refs.groupby("padre"):
        assert grupo["pct_valor"].sum() == pytest.approx(100.0), familia


def test_el_nivel_familia_agrega_todas_sus_referencias(con):
    _venta(con, "PA01", "2026-01", 5, 700)
    _venta(con, "PA02", "2026-01", 3, 300)
    df = calcular_clasificacion(_admin(["PA01", "PA02"]), con, hoy=HOY)
    fam = df[df.nivel == NIVEL_FAMILIA].set_index("clave")
    assert fam.loc["PA", "valor_consumo"] == 1000
    assert fam.loc["PA", "unidades"] == 8


def test_los_id_se_clasifican_dentro_de_su_referencia(con):
    """Dos variantes de la misma referencia, con rotación muy distinta."""
    admin = _admin(
        None,
        especificaciones=[
            ("PA01", "CB1", "ID-ALTA", "Color rojo"),
            ("PA01", "CB2", "ID-BAJA", "Color verde"),
        ],
    )
    _venta(con, "PA01", "2026-01", 100, 9_000, cb="CB1")
    _venta(con, "PA01", "2026-01", 1, 1_000, cb="CB2")
    df = calcular_clasificacion(admin, con, hoy=HOY)
    variantes = df[df.nivel == NIVEL_ID].set_index("clave")
    assert set(variantes.index) == {"ID-ALTA", "ID-BAJA"}
    assert variantes.loc["ID-ALTA", "padre"] == "PA01"
    assert variantes.loc["ID-ALTA", "abc"] == "A"
    assert variantes.loc["ID-BAJA", "abc"] != "A", "es el 10% del ingreso de su referencia"
    assert variantes.loc["ID-ALTA", "etiqueta"] == "Color rojo"


def test_un_par_ambiguo_no_atribuye_id(con):
    """Si el par apunta a dos especificaciones, la línea queda sin ID en vez
    de duplicarse entre ambas."""
    admin = _admin(
        None,
        especificaciones=[
            ("PA01", "CB1", "ID-1", "Rojo"),
            ("PA01", "CB1", "ID-2", "Verde"),  # mismo par, dos ID
        ],
    )
    _venta(con, "PA01", "2026-01", 10, 1000, cb="CB1")
    puente = construir_puente_especificaciones(admin)
    assert puente.empty
    df = calcular_clasificacion(admin, con, hoy=HOY)
    assert df[df.nivel == NIVEL_ID].empty
    # La referencia sí se clasifica: el nivel 2 no depende del puente.
    assert _refs(df).loc["PA01", "abc"] == "A"
