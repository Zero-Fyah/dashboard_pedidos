"""Tests de inventario/salud.py — cobertura y movimiento (DEC-049).

Unitarios: `calcular_salud()` es una función pura sobre un DataFrame y una
conexión de lectura. Se le arma un catálogo mínimo y una SQLite en memoria,
con `hoy` inyectado para que los días no dependan de cuándo corra el test.
"""

import datetime as dt
import logging
import sqlite3

import pandas as pd
import pytest

from comun import VIGENCIA_ACTIVO, VIGENCIA_DESCONTINUADO
from inventario.normalizador import ADMIN_ACTIVO, ADMIN_DESCONTINUADO
from inventario.salud import (
    COBERTURA_ALTA_D,
    ESTADO_ALTA,
    ESTADO_NORMAL,
    ESTADO_QUIEBRE,
    ESTADO_RIESGO,
    ESTADO_SIN_DEMANDA,
    ESTADO_SIN_STOCK_NI_DEMANDA,
    VENTANA_DEMANDA_D,
    calcular_salud,
)

pytestmark = pytest.mark.unit

HOY = dt.date(2026, 7, 26)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT,
           referencia TEXT, almacen TEXT, cantidad_comprada REAL)"""
    )
    c.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT)")
    c.execute("CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT, estado TEXT)")
    yield c
    c.close()


def _admin(filas, producto_activo=ADMIN_ACTIVO):
    """filas: (referencia, inventario, precio).

    `producto_activo` se agrega con el valor "vigente" por defecto (DEC-065):
    es una columna del contrato de `cargar_admin()` y sin ella la referencia
    caería como descontinuada, sacándola del universo de salud.
    """
    df = pd.DataFrame(filas, columns=["referencia", "inventario", "precio"])
    df["producto_activo"] = producto_activo
    return df


def _venta(con, ref, dias_atras, cantidad, estado="Completado", almacen="Bogotá"):
    fecha = (HOY - dt.timedelta(days=dias_atras)).isoformat()
    pid = f"P{ref}{dias_atras}{cantidad}"
    con.execute("INSERT INTO pedidos VALUES (?,?)", (pid, fecha))
    con.execute("INSERT INTO subpedidos VALUES (?,?,?)", (pid, "S1", estado))
    con.execute("INSERT INTO lineas_pedido VALUES (?,?,?,?,?)", (pid, "S1", ref, almacen, cantidad))


# ─────────────────────────────────────────────
# Cobertura
# ─────────────────────────────────────────────


def test_cobertura_es_disponible_sobre_demanda_diaria(con):
    # 900 unidades en 90 días = 10/día; con 300 disponibles → 30 días.
    _venta(con, "PA01", 10, 900)
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "demanda_diaria"] == pytest.approx(900 / VENTANA_DEMANDA_D)
    assert df.loc["PA01", "dias_cobertura"] == pytest.approx(30)


def test_sin_demanda_la_cobertura_queda_nula(con):
    """Poner cero o infinito inventaría un dato: NULL es lo honesto."""
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert pd.isna(df.loc["PA01", "dias_cobertura"])
    assert df.loc["PA01", "estado"] == ESTADO_SIN_DEMANDA


def test_demanda_fuera_de_la_ventana_no_cuenta(con):
    _venta(con, "PA01", VENTANA_DEMANDA_D + 5, 900)
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "demanda_90d"] == 0
    assert df.loc["PA01", "demanda_30d"] == 0


def test_ventana_corta_es_subconjunto_de_la_larga(con):
    _venta(con, "PA01", 5, 100)
    _venta(con, "PA01", 60, 200)
    df = calcular_salud(_admin([("PA01", 0, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "demanda_30d"] == 100
    assert df.loc["PA01", "demanda_90d"] == 300


# ─────────────────────────────────────────────
# Los cancelados no son demanda
# ─────────────────────────────────────────────


def test_subpedido_cancelado_no_cuenta_como_demanda(con):
    """Contarlo inflaría la cobertura justo donde más se cancela."""
    _venta(con, "PA01", 10, 900, estado="Cancelado")
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "demanda_90d"] == 0


def test_otro_almacen_no_cuenta(con):
    _venta(con, "PA01", 10, 900, almacen="Medellin")
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "demanda_90d"] == 0


# ─────────────────────────────────────────────
# Clasificación
# ─────────────────────────────────────────────


def test_sin_stock_con_demanda_es_quiebre(con):
    _venta(con, "PA01", 10, 900)
    df = calcular_salud(_admin([("PA01", 0, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "estado"] == ESTADO_QUIEBRE


def test_sin_stock_sin_demanda_no_es_quiebre(con):
    """La distinción que evita reportar 721 quiebres cuando son 152."""
    df = calcular_salud(_admin([("PA01", 0, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "estado"] == ESTADO_SIN_STOCK_NI_DEMANDA


def test_cobertura_corta_es_riesgo(con):
    _venta(con, "PA01", 10, 900)  # 10/día
    df = calcular_salud(_admin([("PA01", 50, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "dias_cobertura"] == pytest.approx(5)
    assert df.loc["PA01", "estado"] == ESTADO_RIESGO


def test_cobertura_larga_es_alta(con):
    _venta(con, "PA01", 10, 90)  # 1/día
    disponible = COBERTURA_ALTA_D * 2
    df = calcular_salud(_admin([("PA01", disponible, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "estado"] == ESTADO_ALTA


def test_cobertura_intermedia_es_normal(con):
    _venta(con, "PA01", 10, 900)  # 10/día
    df = calcular_salud(_admin([("PA01", 300, 1000)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "estado"] == ESTADO_NORMAL


# ─────────────────────────────────────────────
# Valor y agregación
# ─────────────────────────────────────────────


def test_valor_se_calcula_por_fila_y_luego_se_suma(con):
    """Promediar el precio daría un número que no corresponde a nada."""
    admin = _admin([("PA01", 10, 100), ("PA01", 5, 2000)])
    df = calcular_salud(admin, con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "disponible"] == 15
    assert df.loc["PA01", "valor_venta"] == 10 * 100 + 5 * 2000


def test_dias_sin_salida_y_ultima_salida(con):
    _venta(con, "PA01", 40, 10)
    _venta(con, "PA01", 200, 10)
    df = calcular_salud(_admin([("PA01", 10, 100)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PA01", "ultima_salida"] == (HOY - dt.timedelta(days=40)).isoformat()
    assert df.loc["PA01", "dias_sin_salida"] == 40


def test_referencia_que_nunca_salio_queda_sin_fecha(con):
    df = calcular_salud(_admin([("PA01", 10, 100)]), con, hoy=HOY).set_index("referencia")
    assert pd.isna(df.loc["PA01", "ultima_salida"])
    assert pd.isna(df.loc["PA01", "dias_sin_salida"])


def test_referencia_del_catalogo_sin_ventas_igual_aparece(con):
    """El universo lo define el catálogo, no la demanda."""
    _venta(con, "PA01", 10, 5)
    df = calcular_salud(_admin([("PA01", 10, 100), ("PB02", 7, 100)]), con, hoy=HOY)
    assert set(df["referencia"]) == {"PA01", "PB02"}


def test_familia_sale_de_la_referencia(con):
    df = calcular_salud(_admin([("PJ91", 10, 100)]), con, hoy=HOY).set_index("referencia")
    assert df.loc["PJ91", "familia"] == "PJ"


# ─────────────────────────────────────────────
# Vigencia del catálogo (DEC-065)
# ─────────────────────────────────────────────


def test_producto_marcado_activo_queda_vigente(con):
    df = calcular_salud(_admin([("PA01", 10, 100)]), con, hoy=HOY).set_index("referencia")

    assert df.loc["PA01", "vigencia"] == VIGENCIA_ACTIVO


def test_producto_marcado_inactivo_queda_descontinuado(con):
    """DEC-065: `No hay` es la marca de descontinuado del admin. Se estableció
    midiendo su correlación con inventario y demanda, no por la etiqueta."""
    admin = _admin([("PA01", 0, 100)], producto_activo=ADMIN_DESCONTINUADO)
    df = calcular_salud(admin, con, hoy=HOY).set_index("referencia")

    assert df.loc["PA01", "vigencia"] == VIGENCIA_DESCONTINUADO


def test_el_descontinuado_no_se_borra_del_resultado(con):
    """La separación es del consumidor, no de calcular_salud(): la página
    muestra las vigentes y las otras aparte, pero el dato medido no se tira."""
    admin = pd.concat(
        [
            _admin([("PA01", 10, 100)]),
            _admin([("PB02", 0, 100)], producto_activo=ADMIN_DESCONTINUADO),
        ]
    )
    df = calcular_salud(admin, con, hoy=HOY).set_index("referencia")

    assert set(df.index) == {"PA01", "PB02"}
    assert df.loc["PA01", "vigencia"] == VIGENCIA_ACTIVO
    assert df.loc["PB02", "vigencia"] == VIGENCIA_DESCONTINUADO


def test_una_especificacion_vigente_alcanza_para_la_referencia(con):
    """Criterio conservador: basta una especificación activa para mantener la
    referencia en el universo de salud, en vez de esconderla."""
    admin = pd.concat(
        [
            _admin([("PA01", 5, 100)], producto_activo=ADMIN_DESCONTINUADO),
            _admin([("PA01", 5, 100)]),
        ]
    )
    df = calcular_salud(admin, con, hoy=HOY).set_index("referencia")

    assert df.loc["PA01", "vigencia"] == VIGENCIA_ACTIVO


def test_valor_desconocido_de_producto_activo_avisa(con, caplog):
    """Un valor nuevo cae del lado descontinuado y sacaría referencias del
    universo sin que nadie lo note: tiene que quedar en el log."""
    admin = _admin([("PA01", 10, 100)], producto_activo="Quizás")

    with caplog.at_level(logging.WARNING, logger="inventario.normalizador"):
        df = calcular_salud(admin, con, hoy=HOY).set_index("referencia")

    assert df.loc["PA01", "vigencia"] == VIGENCIA_DESCONTINUADO
    assert "producto_activo" in caplog.text
    assert "Quizás" in caplog.text


# ─────────────────────────────────────────────
# Umbral de cobertura alta (DEC-066)
# ─────────────────────────────────────────────


def test_el_umbral_alto_es_el_definido_por_negocio():
    """DEC-066: 390 días = percentil 80 del catálogo vigente. No es un default
    de implementación, es una definición del Arquitecto — si alguien lo cambia
    sin pasar por decisions.md, este test lo frena."""
    assert COBERTURA_ALTA_D == 390


def test_cobertura_bajo_el_umbral_nuevo_sigue_normal(con):
    """Con el umbral viejo de 90, una cobertura de 200 días caía en "alta".
    Es exactamente el caso que hacía que el 57% del catálogo entrara ahí."""
    _venta(con, "PA01", 10, 90)  # 1/día
    df = calcular_salud(_admin([("PA01", 200, 1000)]), con, hoy=HOY).set_index("referencia")

    assert df.loc["PA01", "dias_cobertura"] == pytest.approx(200)
    assert df.loc["PA01", "estado"] == ESTADO_NORMAL


def test_apenas_sobre_el_umbral_es_alta(con):
    """La frontera se cruza en COBERTURA_ALTA_D, sin zona muerta entre bandas."""
    _venta(con, "PA01", 10, 90)  # 1/día → cobertura = disponible
    df = calcular_salud(_admin([("PA01", COBERTURA_ALTA_D + 1, 1000)]), con, hoy=HOY).set_index(
        "referencia"
    )

    assert df.loc["PA01", "estado"] == ESTADO_ALTA
