"""Tests de la ventana del carril 3 del incremental (AUD-M4, DEC-012).

calcular_desde_nuevos() es la función pura que deriva la fecha 'desde'
del carril de pedidos nuevos a partir del watermark meta.ultima_corrida_ok:

    desde = max(ultima_ok − 1 día, hoy − INCREMENTAL_LOOKBACK_MAX_DIAS)

El parámetro `hoy` es inyectable para testear sin depender del reloj real.
La persistencia del watermark se testea en
tests/integration/test_watermark.py.
"""

from datetime import date

import pytest

from scraper.scraper_principal import CONFIG, calcular_desde_nuevos

HOY = date(2026, 7, 15)


@pytest.mark.unit
def test_tope_configurado_en_siete_dias():
    """DEC-012 fija el tope en 7 días; si cambia, revisar estos tests."""
    assert CONFIG["INCREMENTAL_LOOKBACK_MAX_DIAS"] == 7


@pytest.mark.unit
def test_sin_watermark_usa_ventana_maxima():
    """Tabla meta vacía (primer run tras el despliegue) → hoy − tope."""
    assert calcular_desde_nuevos(None, hoy=HOY) == "2026-07-08"


@pytest.mark.unit
def test_marca_reciente_da_marca_menos_un_dia():
    assert calcular_desde_nuevos("2026-07-14", hoy=HOY) == "2026-07-13"


@pytest.mark.unit
def test_marca_de_hoy_da_ayer():
    """Operación normal (corridas cada 2h): ventana equivalente a ayer-hoy."""
    assert calcular_desde_nuevos("2026-07-15", hoy=HOY) == "2026-07-14"


@pytest.mark.unit
def test_outage_de_tres_dias_reescanea_el_hueco():
    """Caso que motivó AUD-M4: la ventana retrocede hasta cubrir el outage."""
    assert calcular_desde_nuevos("2026-07-12", hoy=HOY) == "2026-07-11"


@pytest.mark.unit
def test_marca_vieja_se_acota_al_tope():
    """Outage > tope: la ventana no crece sin límite (costo de paginación);
    la recuperación del resto del hueco es manual con --desde."""
    assert calcular_desde_nuevos("2026-06-01", hoy=HOY) == "2026-07-08"


@pytest.mark.unit
def test_marca_corrupta_cae_a_ventana_maxima():
    """Valor no parseable no rompe el run ni encoge la ventana."""
    assert calcular_desde_nuevos("garbage", hoy=HOY) == "2026-07-08"


@pytest.mark.unit
def test_marca_vacia_cae_a_ventana_maxima():
    assert calcular_desde_nuevos("", hoy=HOY) == "2026-07-08"


@pytest.mark.unit
def test_marca_futura_no_explota():
    """Marca adelantada (reloj mal configurado): retorna marca − 1 día sin
    lanzar excepción; el servidor simplemente no tendrá pedidos del futuro."""
    assert calcular_desde_nuevos("2026-08-01", hoy=HOY) == "2026-07-31"
