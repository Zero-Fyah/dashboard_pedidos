"""Tests de _log_event del ETL — Fase 1 (E-5, revisión ETL 2026-07-16).

Verifica que el nivel stdlib del registro coincide con el campo "level"
del JSON: antes del fix, WARNING salía como INFO y cualquier filtro o
handler con umbral WARNING lo perdía.
"""

import json
import logging

import pytest

from etl.etl_principal import _log_event


@pytest.mark.unit
def test_log_event_warning_usa_nivel_stdlib_warning(caplog):
    """E-5: level="WARNING" emite con logger.warning, no logger.info."""
    with caplog.at_level(logging.INFO, logger="etl"):
        _log_event("etl_conversion_fallida", level="WARNING", msg="prueba")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


@pytest.mark.unit
def test_log_event_error_usa_nivel_stdlib_error(caplog):
    with caplog.at_level(logging.INFO, logger="etl"):
        _log_event("etl_fallido", level="ERROR", msg="prueba")
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR


@pytest.mark.unit
def test_log_event_info_por_defecto(caplog):
    """Sin level explícito el evento sale como INFO y el JSON es consistente."""
    with caplog.at_level(logging.INFO, logger="etl"):
        _log_event("etl_completado", msg="prueba", db_path="test.db")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    payload = json.loads(record.getMessage())
    assert payload["event"] == "etl_completado"
    assert payload["level"] == "INFO"
    assert payload["modulo"] == "etl"
    assert payload["db_path"] == "test.db"
