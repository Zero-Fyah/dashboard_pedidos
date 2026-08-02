"""Extractores de «Operación de pago» y «Registros de pago» — DEC-087.

Mismo alcance y mismas limitaciones que `test_extractores_batch.py`: se
prueba el lado Python (mapeo por nombre, etiquetas desconocidas, secciones
ausentes) con un fake de `.evaluate()`. El JS en sí no se ejecuta acá.

Lo que sí se verifica del JS es que **sus selectores existan en el DOM
real** — ver `test_selectores_pago.py`. Un nombre de clase mal escrito es el
modo de falla más probable y el fake no lo detectaría.
"""

import pytest

import scraper.extractores as sp
from scraper.extractores import extraer_operacion_pago, extraer_registros_pago


class _FakePage:
    def __init__(self, valor_evaluate):
        self._valor = valor_evaluate

    async def evaluate(self, js, arg=None):
        return self._valor


def _espiar(monkeypatch, eventos):
    """Espía con la MISMA firma que `log_event`.

    Un fake `lambda ev, **kw` acepta cualquier keyword y por eso enmascaró un
    `TypeError` real: el extractor pasaba `columnas=` y `filas=`, que
    `log_event` no acepta. La suite quedaba verde y producción habría
    reventado la primera vez que el origen agregara una columna.
    """

    def _fake(event, *, level="INFO", worker_id=None, id_pedido=None, duracion_ms=None, msg=""):
        eventos.append((event, {"level": level, "id_pedido": id_pedido, "msg": msg}))

    monkeypatch.setattr(sp, "log_event", _fake)


class _PageQueRevienta:
    async def evaluate(self, js, arg=None):
        raise RuntimeError("navegador cerrado")


# ── Operación de pago ────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_mapea_las_etiquetas_de_la_tarjeta():
    datos = {
        "estado": "Pendiente de pago",
        "campos": {
            "Monto total del pedido": "COP 1.937.658",
            "Monto pagado": "COP 0",
            "Saldo pendiente": "COP 1.937.658",
            "Progreso de pago": "0%",
        },
    }

    res = await extraer_operacion_pago(_FakePage(datos), "T-1")

    assert res == {
        "pago_estado": "Pendiente de pago",
        "pago_total": "COP 1.937.658",
        "pago_pagado": "COP 0",
        "pago_saldo": "COP 1.937.658",
        "pago_progreso": "0%",
    }


@pytest.mark.unit
async def test_la_tarjeta_ausente_devuelve_none_sin_ruido(caplog):
    """La sección es del 2026-07-16: en un pedido de enero no renderiza, y
    eso NO es un error. Devolver None es lo que hace que la persistencia
    deje las columnas como están en vez de pisarlas con vacío."""
    res = await extraer_operacion_pago(_FakePage(None), "T-2")

    assert res is None
    assert "WARNING" not in caplog.text


@pytest.mark.unit
async def test_una_etiqueta_nueva_del_origen_avisa(caplog, monkeypatch):
    """Mismo criterio que DEC-032: una etiqueta que no se reconoce se avisa
    en vez de descartarse en silencio — así se enteró DEC-033 de que existían
    los campos de crédito."""
    eventos = []
    _espiar(monkeypatch, eventos)
    datos = {
        "estado": "Pagado",
        "campos": {"Monto total del pedido": "COP 100", "Recargo por mora": "COP 5"},
    }

    res = await extraer_operacion_pago(_FakePage(datos), "T-3")

    assert res["pago_total"] == "COP 100"
    assert "Recargo por mora" not in str(res)
    assert [e for e in eventos if e[0] == "pago_etiqueta_desconocida"]


@pytest.mark.unit
async def test_si_el_evaluate_revienta_no_tumba_el_pedido(monkeypatch):
    eventos = []
    _espiar(monkeypatch, eventos)

    assert await extraer_operacion_pago(_PageQueRevienta(), "T-4") is None
    assert [e for e in eventos if e[0] == "operacion_pago_error"]


# ── Registros de pago ────────────────────────────────────────────────────────


_FILA = {
    "Secuencia": "1-1",
    "Método de pago": "Transferencia",
    "Cuenta receptora": "Bancolombia 123",
    "Monto del comprobante": "COP 5.000.000",
    "Monto de pago": "COP 1.937.658",
    "Hora de pago": "2026-07-30 10:00:00",
    "Comprobante": "144357",
    "Fecha de envío": "2026-07-30 10:05:00",
    "Estado de revisión": "Aprobado",
    "Fecha de revisión": "2026-07-30 11:20:00",
    "Revisor": "ana",
    "Observaciones": "",
}


@pytest.mark.unit
async def test_mapea_las_doce_columnas_por_nombre():
    res = await extraer_registros_pago(_FakePage([_FILA]), "T-5")

    assert len(res) == 1
    assert res[0]["id_pedido"] == "T-5"
    assert res[0]["cuenta_receptora"] == "Bancolombia 123"
    assert res[0]["estado_revision"] == "Aprobado"
    assert res[0]["revisor"] == "ana"


@pytest.mark.unit
async def test_guarda_los_dos_montos_por_separado():
    """DEC-087: `Monto del comprobante` y `Monto de pago` son distintos. La
    hipótesis a falsar es que un comprobante cubra varios pedidos, lo que
    explicaría los pagos de 107 veces el pedido de DEC-084. Colapsarlos en
    una sola columna haría la pregunta irrespondible."""
    res = await extraer_registros_pago(_FakePage([_FILA]), "T-6")

    assert res[0]["monto_comprobante"] == "COP 5.000.000"
    assert res[0]["monto_pago"] == "COP 1.937.658"
    assert res[0]["monto_comprobante"] != res[0]["monto_pago"]


@pytest.mark.unit
async def test_una_columna_nueva_avisa_una_sola_vez(monkeypatch):
    """El aviso se agrega por pedido, no por fila: con 40 comprobantes, una
    columna nueva produciría 40 líneas idénticas de log. Es la misma
    patología del flood que la Fase 5 del ETL tuvo que topar."""
    eventos = []
    _espiar(monkeypatch, eventos)
    filas = [{**_FILA, "Moneda": "COP"} for _ in range(3)]

    res = await extraer_registros_pago(_FakePage(filas), "T-7")

    assert len(res) == 3
    avisos = [e for e in eventos if e[0] == "registros_pago_columna_desconocida"]
    assert len(avisos) == 1
    assert "Moneda" in avisos[0][1]["msg"]


@pytest.mark.unit
async def test_la_tabla_ausente_devuelve_lista_vacia():
    assert await extraer_registros_pago(_FakePage(None), "T-8") == []
    assert await extraer_registros_pago(_FakePage([]), "T-9") == []


@pytest.mark.unit
async def test_una_fila_incompleta_no_rompe_el_mapeo():
    """Si el origen no renderiza una celda, la clave falta. El extractor no
    debe inventarla; la persistencia la rellena con vacío."""
    res = await extraer_registros_pago(_FakePage([{"Secuencia": "1-1", "Revisor": "ana"}]), "T-10")

    assert res == [{"id_pedido": "T-10", "secuencia": "1-1", "revisor": "ana"}]


# -- Filas de subtotal: el defecto que agarro el piloto ----------------------


@pytest.mark.unit
async def test_las_filas_de_subtotal_no_entran_como_comprobantes():
    """Defecto REAL encontrado en el piloto del 2026-08-01, no hipotetico.

    La tabla intercala una fila `Subtotal del envio N:` por envio. Tiene 4
    celdas contra las 12 de un comprobante, asi que su monto cae en la
    posicion de `Cuenta receptora`. En 14 pedidos entraron 10 subtotales
    como si fueran pagos: sobre los 2.447 del rango habrian sido ~800.
    """
    filas = [
        _FILA,
        {"Secuencia": "Subtotal del envio 1:", "Cuenta receptora": "COP 5.000.000"},
        {**_FILA, "Secuencia": "1-2"},
    ]

    res = await extraer_registros_pago(_FakePage(filas), "T-11")

    assert [r["secuencia"] for r in res] == ["1-1", "1-2"]
    assert all("Subtotal" not in str(r.get("secuencia", "")) for r in res)


@pytest.mark.unit
async def test_el_descarte_se_registra(monkeypatch):
    """Un descarte silencioso es indistinguible de un extractor que no
    encuentra nada. El evento dice cuantas filas se fueron."""
    eventos = []
    _espiar(monkeypatch, eventos)
    filas = [{"Secuencia": "Subtotal del envio 1:"}, {"Secuencia": "Total:"}]

    res = await extraer_registros_pago(_FakePage(filas), "T-12")

    assert res == []
    ev = [e for e in eventos if e[0] == "registros_pago_fila_descartada"]
    assert ev and ev[0][1]["msg"].startswith("2 fila")


@pytest.mark.unit
async def test_acepta_las_dos_formas_de_comprobante_real():
    """El piloto mostro dos formas validas: 12 celdas (revisado) y 7
    (subido pero aun sin revisar). Las dos son comprobantes."""
    sin_revisar = {
        "Secuencia": "2-1",
        "Metodo de pago": "Transferencia",
        "Cuenta receptora": "Banco X",
        "Monto del comprobante": "COP 100",
        "Monto de pago": "COP 100",
        "Hora de pago": "2026-07-30 10:00:00",
        "Comprobante": "999",
    }

    res = await extraer_registros_pago(_FakePage([_FILA, sin_revisar]), "T-13")

    assert len(res) == 2
    assert res[1]["secuencia"] == "2-1"
    assert "estado_revision" not in res[1]
