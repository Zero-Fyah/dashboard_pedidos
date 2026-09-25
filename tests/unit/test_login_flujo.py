"""Flujo completo de `login()` con una página falsa — DEC-141.

`tests/e2e/test_login_formulario.py` prueba las piezas en un navegador real;
acá se prueba cómo se encadenan: esperar el formulario definitivo de Cognito,
llenar y verificar, fallar rápido si el envío no salió, reintentar, y la
salida sin huella. Sin navegador ni red.
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import scraper.extractores as ex

pytestmark = pytest.mark.unit


class _Handle:
    def __init__(self, valor):
        self._valor = valor

    async def json_value(self):
        return self._valor


class _Campo:
    def __init__(self, pagina, clave):
        self._p, self._k = pagina, clave

    @property
    def first(self):
        return self

    async def fill(self, valor):
        self._p.campos[self._k] = valor

    async def input_value(self):
        return self._p.campos[self._k]

    async def click(self):
        self._p.clics += 1


class _PaginaLogin:
    """Cada intento consume un guion de `resultados_envio`:
    "ok" → redirige; "vacio" → el formulario queda vacío tras el clic."""

    def __init__(self, resultados_envio, con_huella=True):
        self.resultados = list(resultados_envio)
        self.con_huella = con_huella
        self.campos = {"usuario": "", "clave": ""}
        self.clics = 0
        self.gotos = 0

    async def goto(self, *a, **k):
        self.gotos += 1
        self.campos = {"usuario": "", "clave": ""}

    async def wait_for_selector(self, *a, **k):
        pass

    def locator(self, selector):
        if selector == ex._SEL_LOGIN_USUARIO:
            return _Campo(self, "usuario")
        if selector == ex._SEL_LOGIN_CLAVE:
            return _Campo(self, "clave")
        return _Campo(self, "boton")

    async def wait_for_function(self, js, arg=None, timeout=None):
        if js == ex._JS_FORMULARIO_LOGIN_LISTO:
            if not self.con_huella:
                raise PlaywrightTimeoutError("sin huella")
            return _Handle(True)
        if js == ex._JS_ESPERA_LOGIN:
            resultado = self.resultados.pop(0)
            if resultado == "vacio":
                self.campos = {"usuario": "", "clave": ""}
            return _Handle(resultado)
        raise AssertionError(f"wait_for_function inesperado: {js[:40]}")

    async def query_selector(self, *a, **k):
        return None  # sin botón de idioma


@pytest.fixture
def eventos(monkeypatch):
    registro: list[tuple[str, dict]] = []
    monkeypatch.setattr(ex, "log_event", lambda ev, **kw: registro.append((ev, kw)))

    async def _sin_espera(_s):
        return None

    monkeypatch.setattr(ex.asyncio, "sleep", _sin_espera)
    return registro


def _nombres(eventos):
    return [e for e, _ in eventos]


async def test_login_exitoso_al_primer_intento(eventos):
    pagina = _PaginaLogin(["ok"])
    await ex.login(pagina, "u@x", "clave")
    assert _nombres(eventos) == ["login_ok"]
    assert "intento 1" in eventos[0][1]["msg"]
    assert pagina.campos == {"usuario": "u@x", "clave": "clave"}


async def test_envio_que_no_sale_falla_rapido_y_reintenta(eventos):
    """El formulario queda vacío tras el clic: el intento falla ya (no a los
    45 s) y el siguiente entra."""
    pagina = _PaginaLogin(["vacio", "ok"])
    await ex.login(pagina, "u@x", "clave")
    assert _nombres(eventos) == ["login_error", "login_ok"]
    assert "Formulario vacío" in eventos[0][1]["msg"]
    assert pagina.gotos == 2  # el reintento recarga la página


async def test_sin_huella_avisa_y_sigue(eventos):
    """Si el origen deja de usar `cognitoAsfData`, el login no se bloquea."""
    pagina = _PaginaLogin(["ok"], con_huella=False)
    await ex.login(pagina, "u@x", "clave")
    assert _nombres(eventos) == ["login_sin_huella", "login_ok"]


async def test_tres_fallos_seguidos_lanzan(eventos):
    pagina = _PaginaLogin(["vacio", "vacio", "vacio"])
    with pytest.raises(RuntimeError, match="Login fallido tras 3 intentos"):
        await ex.login(pagina, "u@x", "clave")
    assert _nombres(eventos) == ["login_error"] * 3


async def test_campos_borrados_antes_del_clic_se_rellenan(eventos, monkeypatch):
    """La página borra lo escrito una vez antes de verificar: se vuelve a
    llenar, se avisa y el login sigue."""
    pagina = _PaginaLogin(["ok"])
    llenados = {"n": 0}
    fill_original = _Campo.fill

    async def _fill_que_se_borra_una_vez(self, valor):
        await fill_original(self, valor)
        if self._k == "clave":
            llenados["n"] += 1
            if llenados["n"] == 1:
                self._p.campos = {"usuario": "", "clave": ""}

    monkeypatch.setattr(_Campo, "fill", _fill_que_se_borra_una_vez)
    await ex.login(pagina, "u@x", "clave")
    assert _nombres(eventos) == ["login_campos_borrados", "login_ok"]
