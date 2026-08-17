"""Alertas de quiebre de Arena por ciudad (DEC-119)."""

import datetime as dt

import pandas as pd
import pytest

from comun import Z_SERVICIO_DEFECTO
from comun.arena import alertas_quiebre_arena, demanda_arena_por_ciudad
from comun.reposicion import CV_DEFECTO, ESTADO_PEDIR_YA

pytestmark = pytest.mark.unit

HOY = dt.date(2026, 8, 15)


def _mov(filas):
    """filas: (referencia, codigo_barras, almacen, cantidad_comprada, fecha, estado)."""
    return pd.DataFrame(
        filas,
        columns=["referencia", "codigo_barras", "almacen", "cantidad_comprada", "fecha", "estado"],
    )


def _inv(filas):
    """filas: (codigo_barras, almacen, especificacion, inventario, modalidad)."""
    return pd.DataFrame(
        [
            {
                "codigo_barras": c,
                "almacen": a,
                "especificacion": e,
                "nombre_comercial": e,
                "inventario": i,
                "modalidad": m,
            }
            for c, a, e, i, m in filas
        ]
    )


# ── demanda_arena_por_ciudad ────────────────────────────────────────────


def test_demanda_excluye_cancelados():
    mov = _mov(
        [
            ("PRA ARENA TONELADA", "111", "Bogotá", 100, "2026-08-01", "completado"),
            ("PRA ARENA TONELADA", "111", "Bogotá", 900, "2026-08-01", "cancelado"),
        ]
    )
    d = demanda_arena_por_ciudad(mov, hoy=HOY).set_index(["codigo_barras", "almacen"])

    assert d.loc[("111", "Bogotá"), "demanda_diaria"] == pytest.approx(100 / 90)


def test_demanda_excluye_respaldo_y_yumbo_hub():
    mov = _mov(
        [
            ("ARENA AVERIA BOGOTA", "222", "Bogotá", 500, "2026-08-01", "completado"),
            ("YUMBO TONELADA", "333", "Yumbo", 500, "2026-08-01", "completado"),
        ]
    )
    d = demanda_arena_por_ciudad(mov, hoy=HOY)

    assert d.empty


def test_demanda_respeta_la_ventana():
    mov = _mov(
        [
            ("PRA ARENA TONELADA", "111", "Bogotá", 100, "2026-05-01", "completado"),  # fuera
            ("PRA ARENA TONELADA", "111", "Bogotá", 90, "2026-08-01", "completado"),  # dentro
        ]
    )
    d = demanda_arena_por_ciudad(mov, hoy=HOY, ventana_dias=90).set_index(
        ["codigo_barras", "almacen"]
    )

    assert d.loc[("111", "Bogotá"), "demanda_diaria"] == pytest.approx(90 / 90)


def test_demanda_reconoce_el_esquema_legado():
    """Antes de la migración nacional, la ciudad venía pegada a la referencia
    (DEC-118) — sin esta regla se pierde demanda real de pedidos viejos."""
    mov = _mov([("BOGOTA TONELADA", "111", "Bogotá", 90, "2026-08-01", "completado")])
    d = demanda_arena_por_ciudad(mov, hoy=HOY)

    assert not d.empty


def test_demanda_vacia_no_explota():
    d = demanda_arena_por_ciudad(pd.DataFrame(), hoy=HOY)

    assert d.empty
    assert "demanda_diaria" in d.columns


# ── alertas_quiebre_arena ────────────────────────────────────────────────


def _alertas(inv, dem, lead_time=60, objetivo=30):
    return alertas_quiebre_arena(
        inv, dem, lead_time_dias=lead_time, dias_cobertura_objetivo=objetivo, hoy=HOY
    ).set_index(["codigo_barras", "almacen"])


def test_la_misma_referencia_en_dos_ciudades_da_alertas_independientes():
    inv = _inv(
        [
            ("111", "Bogotá", "Ref", 0, "Tonelada"),  # sin stock
            ("111", "Cali", "Ref", 10000, "Tonelada"),  # sobra
        ]
    )
    dem = pd.DataFrame(
        [
            {"codigo_barras": "111", "almacen": "Bogotá", "demanda_diaria": 10.0},
            {"codigo_barras": "111", "almacen": "Cali", "demanda_diaria": 10.0},
        ]
    )
    r = _alertas(inv, dem)

    assert r.loc[("111", "Bogotá"), "estado_reposicion"] == ESTADO_PEDIR_YA
    assert r.loc[("111", "Cali"), "estado_reposicion"] != ESTADO_PEDIR_YA


def test_las_tres_modalidades_se_suman_en_un_solo_disponible():
    inv = _inv(
        [
            ("111", "Bogotá", "Ref", 100, "Unidades"),
            ("111", "Bogotá", "Ref", 200, "Tonelada"),
            ("111", "Bogotá", "Ref", 50, "Corporativo"),
        ]
    )
    dem = pd.DataFrame([{"codigo_barras": "111", "almacen": "Bogotá", "demanda_diaria": 1.0}])
    r = _alertas(inv, dem)

    assert r.loc[("111", "Bogotá"), "disponible"] == pytest.approx(350)


def test_respaldo_y_yumbo_hub_no_entran_al_calculo():
    inv = _inv([("222", "Bogotá", "Averia", 9999, "Respaldo")])
    dem = pd.DataFrame([{"codigo_barras": "222", "almacen": "Bogotá", "demanda_diaria": 1.0}])
    r = _alertas(inv, dem)

    assert r.empty


def test_sin_cv_medido_usa_los_defaults_del_catalogo_general():
    """Arena no tiene ABC-XYZ (DEC-041): se le pasa abc vacío a
    calcular_reposicion() y debe usar sus valores conservadores."""
    inv = _inv([("111", "Bogotá", "Ref", 100, "Tonelada")])
    dem = pd.DataFrame([{"codigo_barras": "111", "almacen": "Bogotá", "demanda_diaria": 10.0}])
    r = _alertas(inv, dem)

    assert r.loc[("111", "Bogotá"), "cv"] == CV_DEFECTO
    assert r.loc[("111", "Bogotá"), "stock_seguridad"] > 0
    # Z por defecto es el mismo para todas las filas sin clase: se verifica
    # indirectamente porque stock_seguridad > 0 con Z_SERVICIO_DEFECTO > 0.
    assert Z_SERVICIO_DEFECTO > 0


def test_sin_stock_la_fecha_de_quiebre_es_hoy():
    inv = _inv([("111", "Bogotá", "Ref", 0, "Tonelada")])
    dem = pd.DataFrame([{"codigo_barras": "111", "almacen": "Bogotá", "demanda_diaria": 10.0}])
    r = _alertas(inv, dem)

    assert r.loc[("111", "Bogotá"), "fecha_quiebre_proyectada"] == pd.Timestamp(HOY)


def test_sin_demanda_no_aparece():
    inv = _inv([("111", "Bogotá", "Ref", 5000, "Tonelada")])
    r = alertas_quiebre_arena(
        inv, pd.DataFrame(), lead_time_dias=60, dias_cobertura_objetivo=30, hoy=HOY
    )

    assert r.empty


def test_inventario_o_demanda_vacios_no_explota():
    inv = _inv([("111", "Bogotá", "Ref", 5000, "Tonelada")])
    dem = pd.DataFrame([{"codigo_barras": "111", "almacen": "Bogotá", "demanda_diaria": 10.0}])

    assert alertas_quiebre_arena(
        pd.DataFrame(), dem, lead_time_dias=60, dias_cobertura_objetivo=30, hoy=HOY
    ).empty
    assert alertas_quiebre_arena(
        inv, pd.DataFrame(), lead_time_dias=60, dias_cobertura_objetivo=30, hoy=HOY
    ).empty
