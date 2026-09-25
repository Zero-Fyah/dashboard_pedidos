"""`escribir_hoja()` — formato de lo que llega a Google Sheets (DEC-138).

Sin red: un cliente falso registra lo que se escribiría. Lo que importa
fijar es el contrato visible para los asistentes de inventario: fila 1 con
la hora de captura, fila 2 el encabezado, datos desde la 3, NaN como vacío
(la API de Sheets no acepta NaN) y la hoja limpia antes de escribir.
"""

import math

import pandas as pd
import pytest

from integraciones import sheets_cliente as sc

pytestmark = pytest.mark.unit


class _Hoja:
    def __init__(self):
        self.acciones: list[str] = []
        self.escrito = None

    def clear(self):
        self.acciones.append("clear")

    def update(self, valores, value_input_option=None):
        self.acciones.append("update")
        self.escrito = valores
        self.opcion = value_input_option


class _Libro:
    def __init__(self, hoja):
        self.sheet1 = hoja


class _Cliente:
    def __init__(self):
        self.hoja = _Hoja()
        self.abiertas: list[str] = []

    def open_by_key(self, clave):
        self.abiertas.append(clave)
        return _Libro(self.hoja)


def test_escribe_marca_encabezado_y_datos(monkeypatch):
    monkeypatch.setattr(sc, "_hoy_colombia_str", lambda: "2026-09-24 23:32:13")
    gc = _Cliente()
    df = pd.DataFrame({"Ciudad": ["Bogotá", "Cali"], "Disponible para venta": [30.0, float("nan")]})

    n = sc.escribir_hoja(gc, "ID-HOJA", df)

    assert n == 2
    assert gc.abiertas == ["ID-HOJA"]
    assert gc.hoja.acciones == ["clear", "update"]  # limpia antes: es la foto de ahora
    marca, encabezado, *datos = gc.hoja.escrito
    assert marca == ["Actualizado: 2026-09-24 23:32:13 (hora Colombia, UTC-5)"]
    assert encabezado == ["Ciudad", "Disponible para venta"]
    assert datos[0] == ["Bogotá", 30.0]
    assert datos[1][1] == ""  # NaN → vacío
    assert not any(isinstance(v, float) and math.isnan(v) for fila in datos for v in fila)


def test_dataframe_vacio_deja_solo_marca_y_encabezado():
    gc = _Cliente()
    n = sc.escribir_hoja(gc, "ID", pd.DataFrame(columns=["Ciudad", "Referencia"]))
    assert n == 0
    assert len(gc.hoja.escrito) == 2
    assert gc.hoja.escrito[1] == ["Ciudad", "Referencia"]
