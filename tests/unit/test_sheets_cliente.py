"""`escribir_hoja()` — formato de lo que llega a Google Sheets (DEC-138).

Sin red: un cliente falso registra lo que se escribiría. Lo que importa
fijar es el contrato visible para los asistentes de inventario: fila 1 con
la hora de captura, fila 2 el encabezado, datos desde la 3, NaN como vacío
(la API de Sheets no acepta NaN), y desde la auditoría 2026-09-25 que se
escribe ANTES de limpiar el sobrante: un fallo de red no deja la hoja vacía.
"""

import math

import pandas as pd
import pytest

from integraciones import sheets_cliente as sc

pytestmark = pytest.mark.unit


class _Hoja:
    def __init__(self, filas=1000, columnas=26, falla_update=False):
        self.acciones: list[str] = []
        self.escrito = None
        self.rango = None
        self.limpiado: list[str] = []
        self.row_count = filas
        self.col_count = columnas
        self._falla_update = falla_update

    def clear(self):
        self.acciones.append("clear")

    def update(self, valores, range_name=None, value_input_option=None):
        if self._falla_update:
            raise ConnectionError("net::ERR_CONNECTION_CLOSED")
        self.acciones.append("update")
        self.escrito = valores
        self.rango = range_name
        self.opcion = value_input_option

    def batch_clear(self, rangos):
        self.acciones.append("batch_clear")
        self.limpiado = list(rangos)


class _Libro:
    def __init__(self, hoja):
        self.sheet1 = hoja


class _Cliente:
    def __init__(self, hoja=None):
        self.hoja = hoja or _Hoja()
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
    assert gc.hoja.acciones == ["update", "batch_clear"]  # escribe y después limpia el sobrante
    assert gc.hoja.rango == "A1"
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


def test_si_la_escritura_falla_la_hoja_anterior_queda_intacta():
    """Auditoría 2026-09-25: antes se borraba primero y un corte de red
    dejaba la hoja vacía hasta el ciclo siguiente."""
    gc = _Cliente(_Hoja(falla_update=True))
    with pytest.raises(ConnectionError):
        sc.escribir_hoja(gc, "ID", pd.DataFrame({"Ciudad": ["Cali"]}))
    assert gc.hoja.acciones == []  # nada se borró


def test_limpia_solo_filas_y_columnas_sobrantes():
    gc = _Cliente(_Hoja(filas=1000, columnas=26))
    df = pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]})  # 5 filas escritas, 2 columnas

    sc.escribir_hoja(gc, "ID", df)

    assert gc.hoja.limpiado == ["A6:Z1000", "C1:Z5"]


def test_hoja_justa_no_limpia_nada():
    gc = _Cliente(_Hoja(filas=4, columnas=2))
    sc.escribir_hoja(gc, "ID", pd.DataFrame({"A": [1, 2], "B": [3, 4]}))
    assert gc.hoja.acciones == ["update"]
