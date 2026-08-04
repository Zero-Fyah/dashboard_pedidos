"""Tests de `comun/entregas.py` — el compromiso de entrega y su cumplimiento (DEC-095).

Los tres formatos de `hora_entrega` salen de medir la base real, no de leer el
código del origen: `punto` (11.046 filas, feb→may), `franja` (9.430,
may→hoy) y `Cualquier hora` (239). El de la franja es el único que la auditoría
de DEC-094 había mirado.
"""

from datetime import date, datetime

import pytest

from comun.entregas import (
    A_TIEMPO,
    ANTES,
    EN_VENTANA,
    FUERA,
    TARDE,
    TIPO_FRANJA,
    TIPO_PUNTO,
    TIPO_SIN_HORA,
    clasificar_por_dia,
    clasificar_por_ventana,
    dias_de_desvio,
    esta_vencido,
    horas_de_atraso,
    parsear_compromiso,
)

# ── Parseo de los tres formatos ────────────────────────────────────────────────


def test_parsea_franja():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert c is not None
    assert c.tipo == TIPO_FRANJA
    assert c.fecha == date(2026, 8, 1)
    assert c.inicio == datetime(2026, 8, 1, 8, 0)
    assert c.fin == datetime(2026, 8, 1, 9, 0)


def test_parsea_punto_como_ventana_de_ancho_cero():
    """El formato viejo es un instante; tratarlo como ventana evita ramificar."""
    c = parsear_compromiso("2026-03-05 14:00")
    assert c is not None
    assert c.tipo == TIPO_PUNTO
    assert c.inicio == c.fin == datetime(2026, 3, 5, 14, 0)


def test_cualquier_hora_es_dato_pero_no_compromiso():
    """No es lo mismo que vacío: el origen dice explícitamente que no hay hora."""
    c = parsear_compromiso("Cualquier hora")
    assert c is not None
    assert c.tipo == TIPO_SIN_HORA
    assert not c.es_fechado


@pytest.mark.parametrize("texto", ["", "  ", "-", "--", None])
def test_vacios_y_placeholders_no_son_compromiso(texto):
    assert parsear_compromiso(texto) is None


def test_formato_desconocido_no_se_adivina():
    """Inventar una interpretación sería peor que no tener el dato (DEC-081)."""
    assert parsear_compromiso("mañana temprano") is None
    assert parsear_compromiso("2026-08-01") is None


def test_franja_tolera_espaciado_variable():
    assert parsear_compromiso("2026-08-01 08:00~09:00") is not None


# ── Escala por día ─────────────────────────────────────────────────────────────


def test_entregado_el_dia_pactado_es_a_tiempo_aunque_sea_tarde_en_el_dia():
    """La escala por día tolera que una franja de las 8 se cumpla a las 23."""
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, "2026-08-01 23:30:00") == A_TIEMPO


def test_entregado_antes_del_dia_es_a_tiempo():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, "2026-07-30 10:00:00") == A_TIEMPO
    assert dias_de_desvio(c, "2026-07-30 10:00:00") == -2


def test_entregado_al_dia_siguiente_es_tarde():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, "2026-08-02 08:30:00") == TARDE
    assert dias_de_desvio(c, "2026-08-02 08:30:00") == 1


def test_sin_entrega_registrada_no_se_clasifica():
    """Un pedido sin entregar no es 'tarde': todavía no terminó."""
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, None) is None
    assert dias_de_desvio(c, None) is None


def test_cualquier_hora_queda_fuera_de_las_dos_escalas():
    """Contarlo como incumplido inventaría una promesa que nadie hizo."""
    c = parsear_compromiso("Cualquier hora")
    assert clasificar_por_dia(c, "2026-08-01 10:00:00") is None
    assert clasificar_por_ventana(c, "2026-08-01 10:00:00") is None


# ── Escala por ventana ─────────────────────────────────────────────────────────


def test_dentro_de_la_franja():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_ventana(c, "2026-08-01 08:30:00") == EN_VENTANA


def test_los_bordes_de_la_franja_cuentan_como_dentro():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_ventana(c, "2026-08-01 08:00:00") == EN_VENTANA
    assert clasificar_por_ventana(c, "2026-08-01 09:00:00") == EN_VENTANA


def test_antes_y_fuera_de_la_franja():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_ventana(c, "2026-08-01 07:59:00") == ANTES
    assert clasificar_por_ventana(c, "2026-08-01 09:01:00") == FUERA


# ── Atraso en horas ────────────────────────────────────────────────────────────


def test_horas_de_atraso_se_miden_desde_el_cierre_de_la_ventana():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert horas_de_atraso(c, "2026-08-01 11:00:00") == pytest.approx(2.0)


def test_sin_atraso_es_cero_y_nunca_negativo():
    """Adelantarse no es atraso negativo: es no tener atraso."""
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert horas_de_atraso(c, "2026-08-01 08:30:00") == 0.0
    assert horas_de_atraso(c, "2026-07-31 08:30:00") == 0.0


# ── Vencidos: la mitad accionable ──────────────────────────────────────────────


def test_vencido_es_promesa_cerrada_sin_entrega():
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert esta_vencido(c, None, ahora=datetime(2026, 8, 2, 10, 0))


def test_un_pedido_entregado_nunca_esta_vencido():
    """Por tarde que haya llegado, ya llegó — no es trabajo pendiente."""
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert not esta_vencido(c, "2026-08-05 10:00:00", ahora=datetime(2026, 8, 9, 10, 0))


def test_promesa_futura_no_esta_vencida():
    c = parsear_compromiso("2026-08-10 08:00 ~ 09:00")
    assert not esta_vencido(c, None, ahora=datetime(2026, 8, 2, 10, 0))


def test_sin_compromiso_fechado_no_puede_vencer():
    assert not esta_vencido(parsear_compromiso("Cualquier hora"), None)
    assert not esta_vencido(None, None)


# ── Formas en que SQLite entrega el momento ────────────────────────────────────


@pytest.mark.parametrize(
    "momento",
    ["2026-08-02 08:30:00", "2026-08-02T08:30:00", "2026-08-02 08:30:00.123456"],
)
def test_acepta_los_formatos_de_momento_que_llegan_de_sqlite(momento):
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, momento) == TARDE


def test_momento_ilegible_no_rompe():
    """Un dato malo del origen degrada a 'sin clasificar', no a excepción."""
    c = parsear_compromiso("2026-08-01 08:00 ~ 09:00")
    assert clasificar_por_dia(c, "no es una fecha") is None
