"""Tests de _sql_literal del ETL — Fase 4 (E-8, revisión ETL 2026-07-16).

Los literales SQL de estados se generan desde comun/; un estado con
apóstrofe rompería el SQL de las views sin el escape estándar de SQLite.

DEC-065 retiró las views que interpolaban `_CERRADOS_SQL` y
`_ACCIONES_RENDIMIENTO_SQL`, así que ambas constantes se fueron con ellas.
El único literal generado que queda es el de estados activos de inventario,
que alimenta `v_inventario_comprometido` — y es el que se verifica acá.
"""

import pytest

from comun import ESTADOS_ACTIVOS_INVENTARIO
from etl.etl_principal import _ACTIVOS_INVENTARIO_SQL, _sql_literal


@pytest.mark.unit
def test_sql_literal_simple():
    assert _sql_literal("completado") == "'completado'"


@pytest.mark.unit
def test_sql_literal_escapa_apostrofe():
    """La duplicación de comillas ('') es el escape estándar de SQLite."""
    assert _sql_literal("d'ambrosio") == "'d''ambrosio'"


@pytest.mark.unit
def test_activos_inventario_sql_cubre_todos_los_estados():
    """Un estado que no llegue al SQL saldría en silencio del universo de
    v_inventario_comprometido, sin error visible."""
    esperado = ",".join(_sql_literal(e) for e in ESTADOS_ACTIVOS_INVENTARIO)

    assert _ACTIVOS_INVENTARIO_SQL == esperado
    for estado in ESTADOS_ACTIVOS_INVENTARIO:
        assert _sql_literal(estado) in _ACTIVOS_INVENTARIO_SQL
