"""Tests del listado de pedidos endurecido (Fase 4: N-5 + AUD-B1).

_leer_ids_pagina() se testea con fakes de Page/ElementHandle (sin browser).
obtener_lista_pedidos_con_retry() se testea monkeypatcheando
obtener_lista_pedidos() y login() a nivel de módulo: lo que se verifica es
la política de reintento (cuándo re-loguea, cuándo relanza), no el DOM.
El parcheo apunta a scraper.extractores — el módulo donde esas funciones
resuelven sus globals desde DEC-013 (el facade scraper_principal solo
re-exporta). Las esperas semánticas de N-2 solo son verificables E2E
contra la SPA real.
"""

import asyncio

import pytest

import scraper.extractores as sp
from scraper.scraper_principal import (
    SEL_BTN_BUSCAR,
    SEL_LISTA_FILAS,
    SEL_LISTA_ID,
    _leer_ids_pagina,
    obtener_lista_pedidos_con_retry,
    obtener_lista_pedidos_con_watchdog,
)

# ── Fake mínimo de Page para _leer_ids_pagina ──────────────────────────────────
# DEC-030 Fase 1: la recolección cruda vive en un page.evaluate (un solo
# round-trip); el fake devuelve la lista cruda (texto o None por fila) y los
# tests verifican la normalización, que sigue en Python.


class _FakePage:
    def __init__(self, crudos: list[str | None]):
        self._crudos = crudos

    async def evaluate(self, js: str, arg):
        assert arg == [SEL_LISTA_FILAS, SEL_LISTA_ID]
        return self._crudos


@pytest.mark.unit
async def test_leer_ids_pagina_extrae_y_normaliza():
    """N-5: extrae los IDs de las filas y hace strip del texto."""
    page = _FakePage([" TEST-001 ", "TEST-002"])
    assert await _leer_ids_pagina(page) == ["TEST-001", "TEST-002"]


@pytest.mark.unit
async def test_leer_ids_pagina_omite_filas_sin_id():
    """Filas sin celda de ID (None del evaluate) no aportan."""
    page = _FakePage(["TEST-001", None])
    assert await _leer_ids_pagina(page) == ["TEST-001"]


@pytest.mark.unit
async def test_leer_ids_pagina_omite_textos_vacios():
    """Texto en blanco (celda renderizada vacía) tampoco aporta un ID."""
    page = _FakePage(["TEST-001", "   ", ""])
    assert await _leer_ids_pagina(page) == ["TEST-001"]


@pytest.mark.unit
async def test_leer_ids_pagina_tabla_vacia():
    assert await _leer_ids_pagina(_FakePage([])) == []


@pytest.mark.unit
def test_selectores_sin_ruta_absoluta():
    """AUD-B1: ningún selector del listado vuelve a anclarse a #app > div."""
    for sel in (SEL_LISTA_FILAS, SEL_LISTA_ID, SEL_BTN_BUSCAR):
        assert "#app" not in sel


# ── Política de reintento con re-login (AUD-B1) ────────────────────────────────


@pytest.mark.unit
async def test_retry_exito_al_primer_intento_no_reloguea(monkeypatch):
    llamadas = {"lista": 0, "login": 0}

    async def lista_ok(page, desde, hasta):
        llamadas["lista"] += 1
        return ["TEST-001"]

    async def login_falso(page, usuario, clave):
        llamadas["login"] += 1

    monkeypatch.setattr(sp, "obtener_lista_pedidos", lista_ok)
    monkeypatch.setattr(sp, "login", login_falso)

    ids = await obtener_lista_pedidos_con_retry(None, "a", "b", "u", "c")
    assert ids == ["TEST-001"]
    assert llamadas == {"lista": 1, "login": 0}


@pytest.mark.unit
async def test_retry_falla_una_vez_reloguea_y_recupera(monkeypatch):
    """Fallo transitorio (sesión expirada): re-login y segundo intento OK."""
    llamadas = {"lista": 0, "login": 0}

    async def lista_flaky(page, desde, hasta):
        llamadas["lista"] += 1
        if llamadas["lista"] == 1:
            raise TimeoutError("sesión expirada simulada")
        return ["TEST-001", "TEST-002"]

    async def login_falso(page, usuario, clave):
        llamadas["login"] += 1
        assert llamadas["lista"] == 1  # re-login ocurre ENTRE intentos

    monkeypatch.setattr(sp, "obtener_lista_pedidos", lista_flaky)
    monkeypatch.setattr(sp, "login", login_falso)

    ids = await obtener_lista_pedidos_con_retry(None, "a", "b", "u", "c")
    assert ids == ["TEST-001", "TEST-002"]
    assert llamadas == {"lista": 2, "login": 1}


@pytest.mark.unit
async def test_retry_agota_intentos_y_relanza_la_excepcion(monkeypatch):
    """Con todos los intentos fallidos relanza la excepción original y no
    hace un re-login final inútil."""
    llamadas = {"lista": 0, "login": 0}

    async def lista_rota(page, desde, hasta):
        llamadas["lista"] += 1
        raise ValueError("DOM cambiado simulado")

    async def login_falso(page, usuario, clave):
        llamadas["login"] += 1

    monkeypatch.setattr(sp, "obtener_lista_pedidos", lista_rota)
    monkeypatch.setattr(sp, "login", login_falso)

    with pytest.raises(ValueError, match="DOM cambiado simulado"):
        await obtener_lista_pedidos_con_retry(None, "a", "b", "u", "c")
    assert llamadas == {"lista": 2, "login": 1}


@pytest.mark.unit
async def test_retry_max_intentos_uno_no_reintenta(monkeypatch):
    llamadas = {"lista": 0, "login": 0}

    async def lista_rota(page, desde, hasta):
        llamadas["lista"] += 1
        raise ValueError("fallo")

    async def login_falso(page, usuario, clave):
        llamadas["login"] += 1

    monkeypatch.setattr(sp, "obtener_lista_pedidos", lista_rota)
    monkeypatch.setattr(sp, "login", login_falso)

    with pytest.raises(ValueError):
        await obtener_lista_pedidos_con_retry(None, "a", "b", "u", "c", max_intentos=1)
    assert llamadas == {"lista": 1, "login": 0}


# ── Watchdog global del listado (DEC-034) ──────────────────────────────────────


@pytest.mark.unit
async def test_watchdog_pasa_el_resultado_sin_alterarlo(monkeypatch):
    async def retry_rapido(page, desde, hasta, usuario, clave, max_intentos=2):
        return ["TEST-001", "TEST-002"]

    monkeypatch.setattr(sp, "obtener_lista_pedidos_con_retry", retry_rapido)

    ids = await obtener_lista_pedidos_con_watchdog(None, "a", "b", "u", "c")
    assert ids == ["TEST-001", "TEST-002"]


@pytest.mark.unit
async def test_watchdog_corta_un_listado_colgado_y_loguea_error(monkeypatch):
    """DEC-034: un listado que nunca vuelve (navegador congelado a nivel de
    protocolo) no debe colgar la corrida — el watchdog debe cortarlo."""
    eventos = []
    monkeypatch.setattr(sp, "log_event", lambda evento, **kw: eventos.append((evento, kw)))
    monkeypatch.setitem(sp.CONFIG, "LISTADO_TIMEOUT_S", 0.05)

    async def retry_colgado(page, desde, hasta, usuario, clave, max_intentos=2):
        await asyncio.sleep(10)
        return ["nunca llega"]

    monkeypatch.setattr(sp, "obtener_lista_pedidos_con_retry", retry_colgado)

    with pytest.raises(asyncio.TimeoutError):
        await obtener_lista_pedidos_con_watchdog(None, "a", "b", "u", "c")

    assert len(eventos) == 1
    assert eventos[0][0] == "listado_watchdog_timeout"
    assert eventos[0][1]["level"] == "ERROR"
