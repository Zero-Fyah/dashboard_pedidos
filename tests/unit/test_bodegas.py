"""Dos bodegas, una base y un archivo de configuración por bodega (DEC-163).

Funciones puras: rutas por bodega, bodega del proceso, país de las URLs y
validación de la configuración. Lo que abre SQLite o lanza procesos está en
`tests/integration/test_bodegas_db.py` y `test_bodegas_proceso.py`.
"""

from pathlib import Path

import pytest

import comun
import scraper.config as cfg
import scraper.extractores as ex
from comun import (
    BODEGA_DEFECTO,
    BODEGAS,
    RECURSOS_BODEGA,
    RECURSOS_COMPARTIDOS,
    BodegaIncorrectaError,
    get_bodega,
    ruta_bodega,
)

pytestmark = pytest.mark.unit

RAIZ = Path(comun.__file__).parent.parent

# Las rutas fijas que había antes de DEC-163, escritas a mano a propósito:
# si ruta_bodega() cambia la de la bodega por defecto, esto lo detecta.
RUTAS_DE_SIEMPRE = {
    "pedidos.db": RAIZ / "data" / "pedidos.db",
    "tareas.db": RAIZ / "data" / "tareas.db",
    "inventario": RAIZ / "data" / "inventario",
    "errors": RAIZ / "data" / "errors",
    "debug": RAIZ / "data" / "debug",
    "sesiones": RAIZ / "data" / "sesiones",
    "conteos": RAIZ / "data" / "conteos",
    "backups": RAIZ / "data" / "backups",
    "logs": RAIZ / "logs",
    "env": RAIZ / ".env",
}


# ─────────────────────────────────────────────
# Bodega del proceso
# ─────────────────────────────────────────────


def test_sin_bodega_definida_es_la_por_defecto(monkeypatch):
    monkeypatch.delenv("BODEGA", raising=False)
    assert get_bodega() == BODEGA_DEFECTO == "miempresa"
    monkeypatch.setenv("BODEGA", "  ")
    assert get_bodega() == BODEGA_DEFECTO


def test_bodega_definida_y_desconocida(monkeypatch):
    monkeypatch.setenv("BODEGA", "bodega2")
    assert get_bodega() == "bodega2"
    monkeypatch.setenv("BODEGA", "otra")
    with pytest.raises(BodegaIncorrectaError, match="otra"):
        get_bodega()


# ─────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────


def test_bodega_por_defecto_conserva_las_rutas_de_siempre(monkeypatch):
    monkeypatch.delenv("BODEGA", raising=False)
    assert set(RUTAS_DE_SIEMPRE) == set(RECURSOS_BODEGA)
    for recurso, esperada in RUTAS_DE_SIEMPRE.items():
        assert ruta_bodega(recurso) == esperada, recurso
        assert ruta_bodega(recurso, BODEGA_DEFECTO) == esperada, recurso


PROPIOS = [r for r in RECURSOS_BODEGA if r not in RECURSOS_COMPARTIDOS]


def test_bodega2_vive_entera_bajo_su_carpeta():
    carpeta = RAIZ / "data" / "bodega2"
    for recurso in PROPIOS:
        ruta = ruta_bodega(recurso, "bodega2")
        if recurso == "env":
            assert ruta == RAIZ / ".env.bodega2"
        else:
            assert ruta == carpeta / recurso


def test_ninguna_ruta_propia_de_una_bodega_cae_dentro_de_la_otra():
    """Aislamiento: ni igual ni contenida, en ninguna dirección. Es lo que
    garantiza que la purga de una carpeta de una bodega no alcance a la otra."""
    for r1 in PROPIOS:
        for r2 in PROPIOS:
            a = ruta_bodega(r1, BODEGA_DEFECTO)
            b = ruta_bodega(r2, "bodega2")
            assert not b.is_relative_to(a), (r1, r2)
            assert not a.is_relative_to(b), (r1, r2)


def test_la_sesion_de_bochica_es_una_sola_para_las_dos_bodegas():
    """DEC-164: misma sesión de Google, la operación la elige el botón de país.
    Duplicarla obligaría a otro login manual (DEC-116)."""
    assert RECURSOS_COMPARTIDOS == {"sesiones"}
    assert ruta_bodega("sesiones", "bodega2") == RAIZ / "data" / "sesiones"
    assert ruta_bodega("sesiones", "bodega2") == ruta_bodega("sesiones", BODEGA_DEFECTO)


def test_la_bodega_sale_del_entorno_si_no_se_pasa(monkeypatch):
    monkeypatch.setenv("BODEGA", "bodega2")
    assert ruta_bodega("pedidos.db") == RAIZ / "data" / "bodega2" / "pedidos.db"


def test_recurso_o_bodega_desconocidos_fallan():
    with pytest.raises(ValueError, match="Recurso desconocido"):
        ruta_bodega("pedidos")
    with pytest.raises(BodegaIncorrectaError):
        ruta_bodega("pedidos.db", "otra")
    assert set(BODEGAS) == {"miempresa", "bodega2"}


# ─────────────────────────────────────────────
# País de las URLs
# ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,pais",
    [
        ("https://x.example/country/CO/orders/parent-orders", "CO"),
        ("https://x.example/country/pe/orders/parent-orders/detail/", "PE"),
        ("https://x.example/country/CO", "CO"),
        ("/country/CL/orders/parent-orders/detail/123", "CL"),
        ("https://x.example/login", None),
        ("https://x.example/platform", None),
        ("https://x.example/country/COL/orders", None),
        ("", None),
    ],
)
def test_pais_de_url(url, pais):
    assert cfg.pais_de_url(url) == pais


def test_pais_por_defecto_de_la_bodega_historica():
    # El .env de la bodega por defecto no declara SCRAPER_PAIS: queda CO,
    # el país que tenía fijo `_RUTA_DETALLE_PEDIDO` antes de DEC-163.
    assert cfg.PAIS_DEFECTO == "CO"


def test_ruta_del_detalle_sale_del_pais():
    assert (
        ex._RUTA_DETALLE_PEDIDO.format(pais="CO", id_pedido="2085065579")
        == "/country/CO/orders/parent-orders/detail/2085065579"
    )
    assert ex._RUTA_DETALLE_PEDIDO.format(pais="PE", id_pedido="1").startswith("/country/PE/")


def test_pagina_de_otro_pais_detiene_el_run(monkeypatch):
    monkeypatch.setitem(cfg.CONFIG, "pais", "CO")
    ex.verificar_pais_de_pagina("https://x.example/platform")  # sin país: pasa
    ex.verificar_pais_de_pagina("https://x.example/country/CO/orders/parent-orders")
    with pytest.raises(ex.PaisIncorrectoError, match="PE"):
        ex.verificar_pais_de_pagina("https://x.example/country/PE/orders/parent-orders")
    # No debe confundirse con un fallo transitorio de login: es su propia clase.
    assert issubclass(ex.PaisIncorrectoError, RuntimeError)


class _Control:
    """Botón, campo de fecha o resultado de `locator()` de la página falsa."""

    def __init__(self, pagina):
        self.pagina = pagina
        self.valor = ""

    def nth(self, i):
        return self.pagina.campos[i]

    async def click(self):
        pass

    async def fill(self, valor):
        self.valor = valor

    async def press(self, tecla):
        pass

    async def input_value(self):
        return self.valor


class _PaginaDelOrigen:
    """Lo mínimo que usan las dos descargas del sistema administrativo hasta
    el clic en «Exportar», que acá revienta si se alcanza."""

    def __init__(self, url):
        self.url = url
        self.campos = [_Control(self), _Control(self)]
        self.exporto = False

    async def goto(self, *a, **k):
        pass

    async def wait_for_selector(self, *a, **k):
        pass

    def get_by_role(self, *a, **k):
        return _Control(self)

    def locator(self, *a, **k):
        return _Control(self)

    def expect_download(self, *a, **k):
        self.exporto = True
        raise RuntimeError("llegó a exportar")


@pytest.mark.parametrize("descarga", ["inventario", "cambios"])
async def test_descargas_del_admin_no_exportan_si_la_spa_esta_en_otro_pais(
    monkeypatch, tmp_path, descarga
):
    import datetime as dt

    import scraper.cambios_inventario as ci
    import scraper.inventario as si

    monkeypatch.setitem(cfg.CONFIG, "pais", "CO")
    monkeypatch.setitem(
        cfg.CONFIG, "url_inventario", "https://x.example/country/CO/shop/goods-center"
    )
    monkeypatch.setitem(
        cfg.CONFIG, "url_cambios_inventario", "https://x.example/country/CO/shop/inventory/change"
    )

    async def _descargar(pagina):
        if descarga == "inventario":
            await si.descargar_inventario_admin(pagina, tmp_path / "a.xlsx")
        else:
            await ci.descargar_cambios_inventario(
                pagina, dt.date(2026, 10, 20), tmp_path / "c.xlsx"
            )

    # La SPA quedó en otro país aunque la URL pedida era de CO: se detiene antes de exportar.
    otra = _PaginaDelOrigen("https://x.example/country/PE/shop/goods-center")
    with pytest.raises(ex.PaisIncorrectoError):
        await _descargar(otra)
    assert not otra.exporto

    # En el país correcto sigue hasta exportar (que la página falsa corta).
    propia = _PaginaDelOrigen("https://x.example/country/CO/shop/goods-center")
    with pytest.raises(RuntimeError, match="llegó a exportar"):
        await _descargar(propia)
    assert propia.exporto


# ─────────────────────────────────────────────
# validar_config()
# ─────────────────────────────────────────────

_URLS_CO = {
    "url_login": "https://x.example/login",
    "url_post_login": "https://x.example/platform",
    "url_pedidos": "https://x.example/country/CO/orders/parent-orders",
    "url_detalle": "https://x.example/country/CO/orders/parent-orders/detail/",
    "url_inventario": "https://x.example/country/CO/shop/goods-center",
    "url_cambios_inventario": "https://x.example/country/CO/shop/inventory/change",
}
_VARIABLES = frozenset(
    {
        "SCRAPER_URL_LOGIN",
        "SCRAPER_URL_POST_LOGIN",
        "SCRAPER_URL_PEDIDOS",
        "SCRAPER_URL_DETALLE",
        "SCRAPER_USUARIO",
        "SCRAPER_PASSWORD",
        "SCRAPER_PAIS",
    }
)


@pytest.fixture
def config_completa(monkeypatch):
    """Configuración válida de la bodega por defecto, país CO."""
    monkeypatch.setattr(cfg, "BODEGA", BODEGA_DEFECTO)
    monkeypatch.setattr(cfg, "USUARIO", "u")
    monkeypatch.setattr(cfg, "CLAVE", "c")
    monkeypatch.setattr(cfg, "_DEFINIDAS_EN_ARCHIVO", frozenset())
    monkeypatch.setitem(cfg.CONFIG, "pais", "CO")
    for clave, url in _URLS_CO.items():
        monkeypatch.setitem(cfg.CONFIG, clave, url)


def test_bodega_por_defecto_valida_como_antes(config_completa):
    # Las variables pueden venir del entorno: en la bodega por defecto no
    # se exige que estén en el archivo (comportamiento previo a DEC-163).
    cfg.validar_config()
    assert cfg.urls_fuera_de_pais() == []


def test_url_de_otro_pais_se_rechaza(config_completa, monkeypatch):
    monkeypatch.setitem(
        cfg.CONFIG, "url_cambios_inventario", "https://x.example/country/PE/shop/inventory/change"
    )
    with pytest.raises(RuntimeError, match="SCRAPER_URL_CAMBIOS_INVENTARIO apunta al país PE"):
        cfg.validar_config()


def test_otra_bodega_exige_las_variables_en_su_archivo(config_completa, monkeypatch):
    monkeypatch.setattr(cfg, "BODEGA", "bodega2")
    # Mismos valores en CONFIG, pero ninguno definido en .env.bodega2:
    # llegaron heredados del entorno y no cuentan.
    with pytest.raises(RuntimeError, match="faltantes") as exc:
        cfg.validar_config()
    for variable in _VARIABLES:
        assert variable in str(exc.value)

    monkeypatch.setattr(cfg, "_DEFINIDAS_EN_ARCHIVO", _VARIABLES)
    cfg.validar_config()


def test_otra_bodega_sin_pais_no_arranca(config_completa, monkeypatch):
    monkeypatch.setattr(cfg, "BODEGA", "bodega2")
    monkeypatch.setattr(cfg, "_DEFINIDAS_EN_ARCHIVO", _VARIABLES - {"SCRAPER_PAIS"})
    monkeypatch.setitem(cfg.CONFIG, "pais", "")
    with pytest.raises(RuntimeError, match="SCRAPER_PAIS"):
        cfg.validar_config()


def test_otra_bodega_exige_pais_en_listado_y_detalle(config_completa, monkeypatch):
    monkeypatch.setattr(cfg, "BODEGA", "bodega2")
    monkeypatch.setattr(cfg, "_DEFINIDAS_EN_ARCHIVO", _VARIABLES)
    monkeypatch.setitem(cfg.CONFIG, "url_detalle", "https://x.example/orders/detail/")
    with pytest.raises(RuntimeError, match="SCRAPER_URL_DETALLE no trae país"):
        cfg.validar_config()
    # En la bodega por defecto, la misma URL sin país pasa (como antes).
    monkeypatch.setattr(cfg, "BODEGA", BODEGA_DEFECTO)
    cfg.validar_config()
