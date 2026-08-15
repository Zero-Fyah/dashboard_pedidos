"""Tests de scraper/cambios_inventario.py — normalización pura (TASK-001)."""

import numpy as np
import pytest

from scraper.cambios_inventario import _normalizar_fila

pytestmark = pytest.mark.unit


def test_normalizar_fila_convierte_nan_a_none():
    fila = ("PA01", float("nan"), 10)
    assert _normalizar_fila(fila) == ("PA01", None, 10)


def test_normalizar_fila_convierte_none_a_none():
    fila = ("PA01", None, 10)
    assert _normalizar_fila(fila) == ("PA01", None, 10)


def test_normalizar_fila_convierte_bool_a_entero():
    fila = (True, False)
    assert _normalizar_fila(fila) == (1, 0)


def test_normalizar_fila_desempaqueta_escalares_numpy():
    """numpy.int64/float64 revientan con InterfaceError si no se normalizan
    (mismo problema que inventario.persistencia._normalizar())."""
    fila = (np.int64(42), np.float64(3.5))
    resultado = _normalizar_fila(fila)
    assert resultado == (42, 3.5)
    assert isinstance(resultado[0], int)
    assert isinstance(resultado[1], float)


def test_normalizar_fila_conserva_valores_normales():
    fila = ("Bogotá", "Salida por venta", 4)
    assert _normalizar_fila(fila) == ("Bogotá", "Salida por venta", 4)
