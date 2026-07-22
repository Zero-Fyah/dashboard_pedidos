"""Tests de configuración y logging (Fase 6: AUD-M11 + N-4 + AUD-B8a).

Los helpers _env_bool/_env_int se testean directo con monkeypatch de env.
log_event se testea con niveles inválidos (N-4: nunca debe perder el
evento por un AttributeError del nivel).
"""

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

import scraper.config as cfg
from scraper.config import CONFIG, _env_bool, _env_int, log_event

# ── AUD-M11: _env_bool ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "Yes", "si", "sí"])
def test_env_bool_verdaderos(monkeypatch, valor):
    monkeypatch.setenv("X_TEST_BOOL", valor)
    assert _env_bool("X_TEST_BOOL", default=False) is True


@pytest.mark.unit
@pytest.mark.parametrize("valor", ["0", "false", "FALSE", "No"])
def test_env_bool_falsos(monkeypatch, valor):
    monkeypatch.setenv("X_TEST_BOOL", valor)
    assert _env_bool("X_TEST_BOOL", default=True) is False


@pytest.mark.unit
def test_env_bool_ausente_usa_default(monkeypatch):
    monkeypatch.delenv("X_TEST_BOOL", raising=False)
    assert _env_bool("X_TEST_BOOL", default=True) is True
    assert _env_bool("X_TEST_BOOL", default=False) is False


@pytest.mark.unit
def test_env_bool_no_reconocido_usa_default(monkeypatch):
    """Un valor malformado no tumba el import: cae al default."""
    monkeypatch.setenv("X_TEST_BOOL", "quizás")
    assert _env_bool("X_TEST_BOOL", default=True) is True
    assert _env_bool("X_TEST_BOOL", default=False) is False


# ── AUD-M11: _env_int ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_env_int_valido(monkeypatch):
    monkeypatch.setenv("X_TEST_INT", " 25 ")
    assert _env_int("X_TEST_INT", default=50) == 25


@pytest.mark.unit
@pytest.mark.parametrize("valor", ["", "abc", "5.5"])
def test_env_int_invalido_usa_default(monkeypatch, valor):
    monkeypatch.setenv("X_TEST_INT", valor)
    assert _env_int("X_TEST_INT", default=50) == 50


@pytest.mark.unit
def test_headless_y_slow_mo_en_config():
    """AUD-M11: las claves existen y tienen los tipos correctos."""
    assert isinstance(CONFIG["HEADLESS"], bool)
    assert isinstance(CONFIG["SLOW_MO"], int)


# ── AUD-B8a: credenciales sin releer env ────────────────────────────────────


@pytest.mark.unit
def test_usuario_clave_vienen_de_config():
    assert cfg.USUARIO == CONFIG["usuario"]
    assert cfg.CLAVE == CONFIG["clave"]


# ── N-4: log_event robusto y handler único ─────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("nivel", ["NOEXISTE", "warning", ""])
def test_log_event_nivel_invalido_no_revienta(nivel):
    """Nivel desconocido (o en minúsculas, donde getattr devolvería la
    función logging.warning) cae a INFO sin perder el evento."""
    log_event("evento_test", level=nivel, msg="no debe lanzar")


@pytest.mark.unit
def test_logger_tiene_un_solo_handler():
    """N-4: el guard de handlers evita duplicados ante re-imports.

    Se cuenta solo el TimedRotatingFileHandler exacto del módulo (retención
    de logs, 2026-07-19): el plugin de logging de pytest >= 9.1 agrega sus
    propios handlers de captura a los loggers con propagate=False (incluido
    un _FileHandler que SUBCLASEA FileHandler — por eso `type(...) is` y no
    isinstance).
    """
    propios = [
        h
        for h in logging.getLogger("scraper.config").handlers
        if type(h) is TimedRotatingFileHandler
    ]
    assert len(propios) == 1
