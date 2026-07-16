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

import pytest

import scraper.extractores as sp
from scraper.scraper_principal import (
    SEL_BTN_BUSCAR,
    SEL_LISTA_FILAS,
    SEL_LISTA_ID,
    _leer_ids_pagina,
    obtener_lista_pedidos_con_retry,
)

# ── Fakes mínimos del árbol DOM que _leer_ids_pagina consulta ──────────────────


class _FakeEl:
    def __init__(self, texto: str):
        self._texto = texto

    async def inner_text(self) -> str:
        return self._texto


class _FakeFila:
    """Fila de tabla; texto None simula una fila sin celda de ID."""

    def __init__(self, texto: str | None):
        self._texto = texto

    async def query_selector(self, sel: str):
        assert sel == SEL_LISTA_ID
        return _FakeEl(self._texto) if self._texto is not None else None


class _FakePage:
    def __init__(self, filas: list[_FakeFila]):
        self._filas = filas

    async def query_selector_all(self, sel: str):
        assert sel == SEL_LISTA_FILAS
        return self._filas


@pytest.mark.unit
async def test_leer_ids_pagina_extrae_y_normaliza():
    """N-5: extrae los IDs de las filas y hace strip del texto."""
    page = _FakePage([_FakeFila(" TEST-001 "), _FakeFila("TEST-002")])
    assert await _leer_ids_pagina(page) == ["TEST-001", "TEST-002"]


@pytest.mark.unit
async def test_leer_ids_pagina_omite_filas_sin_id():
    """Filas sin celda de ID (placeholder de tabla vacía) no aportan."""
    page = _FakePage([_FakeFila("TEST-001"), _FakeFila(None)])
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
