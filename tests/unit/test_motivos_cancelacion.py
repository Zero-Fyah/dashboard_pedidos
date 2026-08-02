"""Clasificación de motivos de cancelación (DEC-085).

Los casos vienen del texto real de `registro_operaciones.referencia`: emojis,
iniciales pegadas al final, mayúsculas inconsistentes y tildes ausentes.
"""

import pytest

from comun.motivos import (
    CAUSA_FALTA_DE_PAGO,
    CAUSA_OTRO,
    CAUSA_SIN_MOTIVO,
    DIAS_LIMITE_PAGO,
    clasificar_motivo,
    normalizar_motivo,
)

pytestmark = pytest.mark.unit


# ── Normalización ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("EXCEDE DÍAS LÍMITE DE PAGO", "excede dias limite de pago"),
        ("Excede dias limite de pago 🍀🐼❄️", "excede dias limite de pago"),
        ("SE REINTEGRA /EM✨", "se reintegra em"),
        ("  varios    espacios  ", "varios espacios"),
        (None, ""),
        ("", ""),
    ],
)
def test_normaliza_a_algo_comparable(crudo, esperado):
    assert normalizar_motivo(crudo) == esperado


def test_los_emojis_no_cambian_la_clasificacion():
    """Tres redacciones del mismo motivo, con y sin adornos, deben caer juntas."""
    variantes = [
        "EXCEDE DÍAS LÍMITE DE PAGO / SE REALIZÓ SEGUIMIENTO / SE REINTEGRA /EM✨",
        "Excede dias limites de pago",
        "CANCELADO: CLIENTE EXCEDE LÍMITE DÍAS PAGO 🔴⚡",
    ]

    assert {clasificar_motivo(v) for v in variantes} == {CAUSA_FALTA_DE_PAGO}


# ── Clasificación ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("motivo", "causa"),
    [
        ("se reintegra pedido cliente no realiza pago", CAUSA_FALTA_DE_PAGO),
        ("sin pago", CAUSA_FALTA_DE_PAGO),
        ("Se cancela pedido por exceder tiempo de pago.", CAUSA_FALTA_DE_PAGO),
        ("el cliente se encuentra en mora", CAUSA_FALTA_DE_PAGO),
        ("cliente registra error al subir pedido", "Error de captura"),
        ("pedido duplicado", "Error de captura"),
        ("confirmar devolución", "Devolución"),
        ("comercial subira otro pedido", "Cambio de pedido"),
        ("a peticion de cliente", "Petición del cliente"),
        ("a peticion de comercial", "Petición comercial"),
        ("no cumple monto minimo", "Regla comercial"),
        ("se cancela pedido por cambio de bodega de despacho", "Cambio logístico"),
    ],
)
def test_clasifica_los_motivos_reales(motivo, causa):
    assert clasificar_motivo(motivo) == causa


def test_el_pago_gana_sobre_las_otras_causas():
    """El orden de las reglas es la prioridad. Un motivo mixto —'excede días
    de pago, comercial solicita reintegro'— es una cancelación por pago, no
    una petición comercial: lo que mató el pedido fue que no se pagó."""
    mixto = "excede tiempo limite de pago sin respuesta comercial se reintegra mm"

    assert clasificar_motivo(mixto) == CAUSA_FALTA_DE_PAGO


def test_el_id_numerico_no_es_un_motivo():
    """Antes del 2026-01-23 el origen guardaba el ID del pedido en esta
    columna. Clasificarlo como `Otro` lo haría parecer texto que las reglas
    no entendieron, e infla el aparente fracaso del clasificador: al
    separarlo, lo no clasificado bajó de 19,2% a 9,5%."""
    assert clasificar_motivo("142446") == CAUSA_SIN_MOTIVO
    assert clasificar_motivo("  142079  ") == CAUSA_SIN_MOTIVO


def test_la_ausencia_de_motivo_no_es_una_causa():
    assert clasificar_motivo("") == CAUSA_SIN_MOTIVO
    assert clasificar_motivo(None) == CAUSA_SIN_MOTIVO


def test_lo_que_no_casa_queda_en_otro_sin_forzarse():
    """No se estiran las reglas hasta que todo case: un motivo genuinamente
    ambiguo debe quedar en `Otro`, visible, en vez de asignarse a la causa
    más parecida."""
    assert clasificar_motivo("modalidad erronea") == CAUSA_OTRO
    assert clasificar_motivo("almacen") == CAUSA_OTRO


def test_el_id_con_texto_alrededor_si_se_clasifica():
    """'confirmar devolucion 142079' tiene motivo Y número: no es el caso
    del ID desnudo."""
    assert clasificar_motivo("confirmar devolucion 142079") == "Devolución"


def test_el_limite_de_dias_es_el_medido():
    """Ancla el 5 a DEC-085: no es un parámetro elegido sino la mediana real
    del comportamiento de la empresa (94% entre el día 3 y el 7)."""
    assert DIAS_LIMITE_PAGO == 5
