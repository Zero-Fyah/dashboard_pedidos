"""Tests de _sql_literal del ETL — Fase 4 (E-8, revisión ETL 2026-07-16).

Los literales SQL de estados se generan desde comun/; un estado con
apóstrofe rompería el SQL de las views sin el escape estándar de SQLite.
"""

import pytest

from comun import ACCIONES_RENDIMIENTO
from etl.etl_principal import _ACCIONES_RENDIMIENTO_SQL, _CERRADOS_SQL, _sql_literal


@pytest.mark.unit
def test_sql_literal_simple():
    assert _sql_literal("completado") == "'completado'"


@pytest.mark.unit
def test_sql_literal_escapa_apostrofe():
    """La duplicación de comillas ('') es el escape estándar de SQLite."""
    assert _sql_literal("d'ambrosio") == "'d''ambrosio'"


@pytest.mark.unit
def test_cerrados_sql_identico_al_previo():
    """El escape no altera el SQL generado con los estados actuales
    (ninguno contiene apóstrofes): mismas views que antes de E-8."""
    assert _CERRADOS_SQL == "'cancelado','comentado','completado'"


@pytest.mark.unit
def test_acciones_rendimiento_exactamente_cuatro():
    """HAL-008: las 4 acciones verificadas en DB real el 2026-07-17."""
    assert ACCIONES_RENDIMIENTO == (
        "Alistamiento sin diferencia",
        "Alistamiento con faltantes",
        "Inspección sin diferencia",
        "Inspección con diferencia",
    )


@pytest.mark.unit
def test_acciones_rendimiento_sql_identico_al_hardcodeado_previo():
    """HAL-008 (Fase 6): el SQL generado desde comun/ es idéntico al
    literal que estaba hardcodeado en v_rendimiento_operadores."""
    assert _ACCIONES_RENDIMIENTO_SQL == (
        "'Alistamiento sin diferencia','Alistamiento con faltantes',"
        "'Inspección sin diferencia','Inspección con diferencia'"
    )
