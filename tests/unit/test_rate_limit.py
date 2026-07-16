"""Tests de la señal compartida de rate limiting (AUD-M2).

El handler de respuestas 429 solo registra la señal via
registrar_rate_limit(); los workers consultan rate_limit_pendiente()
y duermen ellos mismos. Ambas funciones aceptan el reloj inyectado
(parámetro `ahora`) para testear sin dormir ni depender del tiempo real.
"""

import pytest

from scraper.scraper_principal import (
    _RATE_LIMIT,
    CONFIG,
    rate_limit_pendiente,
    registrar_rate_limit,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """Aísla cada test: la señal es estado compartido a nivel de módulo."""
    _RATE_LIMIT["hasta"] = 0.0
    yield
    _RATE_LIMIT["hasta"] = 0.0


@pytest.mark.unit
def test_sin_senal_no_hay_pausa():
    assert rate_limit_pendiente(ahora=1000.0) == 0.0


@pytest.mark.unit
def test_retry_after_numerico_registra_espera():
    wait_s = registrar_rate_limit("30", ahora=1000.0)
    assert wait_s == 30.0
    assert rate_limit_pendiente(ahora=1000.0) == 30.0


@pytest.mark.unit
def test_retry_after_invalido_usa_default():
    """Header vacío o no numérico cae al default de CONFIG."""
    esperado = float(CONFIG["RATE_LIMIT_WAIT_S"])
    assert registrar_rate_limit("", ahora=1000.0) == esperado
    _RATE_LIMIT["hasta"] = 0.0
    assert registrar_rate_limit("abc", ahora=1000.0) == esperado


@pytest.mark.unit
def test_senal_expira_con_el_tiempo():
    registrar_rate_limit("30", ahora=1000.0)
    assert rate_limit_pendiente(ahora=1015.0) == 15.0
    assert rate_limit_pendiente(ahora=1030.0) == 0.0
    assert rate_limit_pendiente(ahora=2000.0) == 0.0


@pytest.mark.unit
def test_senal_posterior_extiende_la_ventana():
    """Un segundo 429 durante la pausa alarga la ventana."""
    registrar_rate_limit("30", ahora=1000.0)  # hasta 1030
    registrar_rate_limit("30", ahora=1020.0)  # hasta 1050
    assert rate_limit_pendiente(ahora=1020.0) == 30.0


@pytest.mark.unit
def test_senal_menor_no_acorta_ventana_activa():
    """Señales solapadas toman el máximo — nunca reducen la pausa vigente."""
    registrar_rate_limit("60", ahora=1000.0)  # hasta 1060
    registrar_rate_limit("5", ahora=1005.0)  # 1010 < 1060 → se ignora
    assert rate_limit_pendiente(ahora=1005.0) == 55.0
