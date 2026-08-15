"""Tests de scraper/cambios_inventario.py — esquema y carga (TASK-001).

Van en integration/ porque ejercitan SQLite real y lectura de Excel: es
justamente el contrato que se quiere verificar (idempotencia, normalización
de NaN, clave natural con almacen), no algo que un mock pueda sustituir.
"""

import sqlite3
from datetime import date

import pandas as pd
import pytest

from scraper.cambios_inventario import cargar_movimientos, init_schema, ya_capturado

pytestmark = pytest.mark.integration

_COLUMNAS_FUENTE = [
    "Nombre del producto",
    "Número de producto",
    "Nombre de SKU",
    "SKU ID",
    "Almacén",
    "Tipo de cambio",
    "Valor ajustado",
    "Antes del ajuste",
    "Después del ajuste",
    "Operador",
    "Hora de operación",
]


def _fila(
    sku_id="1000000000000000001",
    referencia="PA01",
    tipo="Salida por venta",
    valor=4,
    antes=100,
    despues=96,
    almacen="Bogotá",
    hora="2026-08-13 10:00:00",
):
    return {
        "Nombre del producto": "Producto de prueba",
        "Número de producto": referencia,
        "Nombre de SKU": "Presentación estándar",
        "SKU ID": sku_id,
        "Almacén": almacen,
        "Tipo de cambio": tipo,
        "Valor ajustado": valor,
        "Antes del ajuste": antes,
        "Después del ajuste": despues,
        "Operador": "Sistema",
        "Hora de operación": hora,
    }


def _escribir_excel(tmp_path, filas, nombre="cambios.xlsx"):
    ruta = tmp_path / nombre
    pd.DataFrame(filas, columns=_COLUMNAS_FUENTE).to_excel(ruta, sheet_name="Sheet1", index=False)
    return ruta


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test_pedidos.db")
    init_schema(path)
    return path


def test_init_schema_es_idempotente(db):
    init_schema(db)  # segunda llamada no debe fallar
    con = sqlite3.connect(db)
    tablas = {fila[0] for fila in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "movimientos_inventario" in tablas


def test_cargar_movimientos_inserta_filas_nuevas(db, tmp_path):
    ruta = _escribir_excel(tmp_path, [_fila()])
    insertadas = cargar_movimientos(ruta, db, origen="diario")
    assert insertadas == 1
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM movimientos_inventario").fetchone()[0]
    con.close()
    assert total == 1


def test_cargar_movimientos_es_idempotente(db, tmp_path):
    """Cargar el mismo archivo dos veces no duplica filas (INSERT OR IGNORE)."""
    ruta = _escribir_excel(tmp_path, [_fila()])
    cargar_movimientos(ruta, db, origen="diario")
    segunda = cargar_movimientos(ruta, db, origen="diario")
    assert segunda == 0
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM movimientos_inventario").fetchone()[0]
    con.close()
    assert total == 1


def test_cargar_movimientos_normaliza_espacios_en_referencia(db, tmp_path):
    ruta = _escribir_excel(tmp_path, [_fila(referencia="  PA01 AVERIA  ")])
    cargar_movimientos(ruta, db, origen="diario")
    con = sqlite3.connect(db)
    referencia = con.execute("SELECT referencia FROM movimientos_inventario").fetchone()[0]
    con.close()
    assert referencia == "PA01 AVERIA"


def test_cargar_movimientos_antes_ajuste_nulo_queda_null(db, tmp_path):
    ruta = _escribir_excel(tmp_path, [_fila(antes=None)])
    cargar_movimientos(ruta, db, origen="diario")
    con = sqlite3.connect(db)
    fila = con.execute(
        "SELECT antes_ajuste FROM movimientos_inventario WHERE antes_ajuste IS NULL"
    ).fetchone()
    con.close()
    assert fila is not None


def test_ya_capturado_detecta_fecha_existente(db, tmp_path):
    ruta = _escribir_excel(tmp_path, [_fila(hora="2026-08-13 10:00:00")])
    cargar_movimientos(ruta, db, origen="diario")
    assert ya_capturado(db, date(2026, 8, 13)) is True
    assert ya_capturado(db, date(2026, 8, 12)) is False


def test_dos_almacenes_mismo_movimiento_no_colisionan(db, tmp_path):
    """Confirma la corrección de la clave natural (TASK-001): dos filas con
    el mismo SKU/hora/tipo/valor pero distinto almacén no se pisan entre sí."""
    filas = [
        _fila(sku_id="2000000000000000001", almacen="Bogotá", hora="2026-08-13 10:37:56"),
        _fila(sku_id="2000000000000000001", almacen="Medellin", hora="2026-08-13 10:37:56"),
    ]
    ruta = _escribir_excel(tmp_path, filas)
    insertadas = cargar_movimientos(ruta, db, origen="diario")
    assert insertadas == 2


def test_origen_se_registra_por_fila(db, tmp_path):
    ruta = _escribir_excel(tmp_path, [_fila()])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    origen = con.execute("SELECT origen FROM movimientos_inventario").fetchone()[0]
    con.close()
    assert origen == "backfill"
