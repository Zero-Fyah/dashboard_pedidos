"""Tests de `dashboard/tz.py` — conversión de timestamps a hora Colombia (DEC-109).

Colombia no tiene horario de verano desde 1993: un offset fijo de -5 h es
correcto todo el año, mismo criterio que `dashboard/db.py:_HOY_CO` (AUD-B6).
"""

from dashboard.tz import a_hora_colombia


def test_convierte_utc_explicito_a_hora_colombia():
    assert a_hora_colombia("2026-08-12T23:56:53+00:00") == "2026-08-12 18:56"


def test_cruza_medianoche_hacia_atras():
    """La ventana 00:00-05:00 UTC es el día anterior en Colombia (AUD-B6)."""
    assert a_hora_colombia("2026-08-13T02:50:50+00:00") == "2026-08-12 21:50"


def test_sin_offset_explicito_se_asume_utc():
    """El scraper y persistencia.py escriben con offset, pero por si no lo trae."""
    assert a_hora_colombia("2026-08-12T23:56:53") == "2026-08-12 18:56"


def test_valor_vacio_da_guion():
    assert a_hora_colombia(None) == "—"
    assert a_hora_colombia("") == "—"


def test_valor_no_parseable_se_devuelve_tal_cual():
    assert a_hora_colombia("no es una fecha") == "no es una fecha"


def test_formato_personalizado():
    assert a_hora_colombia("2026-08-12T23:56:53+00:00", fmt="%H:%M") == "18:56"
