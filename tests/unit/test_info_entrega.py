"""Tests de extraer_info_entrega — DEC-023 (BUG-019).

La tabla 'Información de entrega' cambia de layout según el método:
'Ruta' agrega las filas Conductor y Vehículo de entrega. El extractor
lee por etiqueta, no por posición; estos fakes reproducen los dos
layouts reales observados en el DOM del sistema origen (2026-07-18).
"""

import pytest

import scraper.extractores as sp
from scraper.extractores import extraer_info_entrega

_LABEL = "el-descriptions__cell el-descriptions__label is-bordered-label"
_CONTENT = "el-descriptions__cell el-descriptions__content is-bordered-content"


class _FakeCelda:
    def __init__(self, texto: str, es_label: bool, tags: list[tuple[str, str]] | None = None):
        self._texto = texto
        self._clase = _LABEL if es_label else _CONTENT
        # tags: lista de (clase_tag, texto) para la celda del método
        self._tags = tags or []

    async def get_attribute(self, nombre: str):
        return self._clase if nombre == "class" else None

    async def inner_text(self) -> str:
        return self._texto

    async def query_selector_all(self, sel: str):
        if sel == ".el-tag":
            return [_FakeCelda(t, False) for _, t in self._tags]
        return []

    async def query_selector(self, sel: str):
        for clase, texto in self._tags:
            if sel.startswith(f".{clase} "):
                return _FakeCelda(texto, False)
        return None


class _FakeTabla:
    def __init__(self, celdas: list[_FakeCelda]):
        self._celdas = celdas

    async def query_selector_all(self, sel: str):
        assert sel == "td"
        return self._celdas


class _FakePage:
    def __init__(self, tabla):
        self._tabla = tabla

    async def query_selector(self, sel: str):
        return self._tabla


def _par(label: str, valor: str, tags=None) -> list[_FakeCelda]:
    return [_FakeCelda(label, True), _FakeCelda(valor, False, tags)]


def _tabla_transportadora() -> _FakeTabla:
    """Layout sin Conductor/Vehículo (Transportadora, Almacen)."""
    celdas = (
        _par("Método de entrega", "Transportadora")
        + _par("Despachador", "DESPACHADOR X")
        + _par("Hora de entrega", "2026-03-26 00:00")
        + _par("Número de estante de inspección", "Ver")
        + _par("Observaciones", "NACIONAL")
    )
    return _FakeTabla(celdas)


def _tabla_ruta() -> _FakeTabla:
    """Layout de Ruta: agrega Conductor y Vehículo de entrega."""
    celdas = (
        _par("Método de entrega", "Ruta")
        + _par("Despachador", "-")
        + _par("Conductor", "CONDUCTOR DE PRUEBA")
        + _par("Hora de entrega", "2026-07-18 15:30")
        + _par("Vehículo de entrega", "37")
        + _par("Número de estante de inspección", "Ver")
        + _par("Observaciones", "OBS REAL DE RUTA")
    )
    return _FakeTabla(celdas)


@pytest.mark.unit
async def test_entrega_layout_sin_conductor():
    """Transportadora/Almacen: hora y observaciones en su lugar."""
    res = await extraer_info_entrega(_FakePage(_tabla_transportadora()), "TEST-1")
    assert res["entrega_metodo_texto"] == "Transportadora"
    assert res["despachador"] == "DESPACHADOR X"
    assert res["hora_entrega"] == "2026-03-26 00:00"
    assert res["obs_entrega"] == "NACIONAL"
    assert res["conductor"] == ""
    assert res["vehiculo_entrega"] == ""


@pytest.mark.unit
async def test_entrega_layout_ruta_no_desplaza_campos():
    """BUG-019: con Ruta, conductor y vehículo van a SUS columnas y la
    hora/observaciones reales dejan de perderse.

    Antes del fix (indexado posicional), hora_entrega recibía el nombre
    del conductor y obs_entrega el número de vehículo.
    """
    res = await extraer_info_entrega(_FakePage(_tabla_ruta()), "TEST-2")
    assert res["conductor"] == "CONDUCTOR DE PRUEBA"
    assert res["vehiculo_entrega"] == "37"
    assert res["hora_entrega"] == "2026-07-18 15:30"
    assert res["obs_entrega"] == "OBS REAL DE RUTA"
    assert res["entrega_metodo_texto"] == "Ruta"


@pytest.mark.unit
async def test_entrega_extrae_tags_del_metodo():
    """Los tags de ruta/descuento salen aparte y no ensucian el texto."""
    celdas = _par(
        "Método de entrega",
        "Ruta RUTA-NORTE DTO",
        tags=[("el-tag--primary", "RUTA-NORTE"), ("el-tag--warning", "DTO")],
    ) + _par("Despachador", "-")
    res = await extraer_info_entrega(_FakePage(_FakeTabla(celdas)), "TEST-3")
    assert res["entrega_ruta_tag"] == "RUTA-NORTE"
    assert res["entrega_descuento_tag"] == "DTO"
    assert "RUTA-NORTE" not in res["entrega_metodo_texto"]
    assert "DTO" not in res["entrega_metodo_texto"]


@pytest.mark.unit
async def test_entrega_etiqueta_desconocida_emite_warning(monkeypatch):
    """DEC-023: un campo nuevo de la SPA se reporta, no se pierde callado."""
    eventos: list[tuple] = []
    monkeypatch.setattr(sp, "log_event", lambda evento, **kw: eventos.append((evento, kw)))
    celdas = _par("Método de entrega", "Ruta") + _par("Campo Nuevo De La SPA", "valor")
    res = await extraer_info_entrega(_FakePage(_FakeTabla(celdas)), "TEST-4")

    assert res["entrega_metodo_texto"] == "Ruta"
    desconocidas = [e for e in eventos if e[0] == "entrega_etiqueta_desconocida"]
    assert len(desconocidas) == 1
    assert desconocidas[0][1]["etiquetas"] == ["Campo Nuevo De La SPA"]
    assert desconocidas[0][1]["level"] == "WARNING"


@pytest.mark.unit
async def test_entrega_sin_card_retorna_vacio():
    """Sin tabla en el DOM, todas las claves quedan vacías."""
    res = await extraer_info_entrega(_FakePage(None), "TEST-5")
    assert set(res) == {
        "entrega_metodo_texto",
        "entrega_ruta_tag",
        "entrega_descuento_tag",
        "despachador",
        "conductor",
        "hora_entrega",
        "vehiculo_entrega",
        "obs_entrega",
    }
    assert all(v == "" for v in res.values())
