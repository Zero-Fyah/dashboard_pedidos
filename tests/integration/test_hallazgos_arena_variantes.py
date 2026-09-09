"""Test de `especificacion_discrepante` contra el documento de variantes
históricas de Arena (`arena_variantes_historicas.xlsx`), con archivo real
(no monkeypatch) — mismo criterio que `test_arena_nombres_canonicos.py`:
depende de `ruta.exists()` + `pd.read_excel` sobre disco.

Contexto: `lineas_pedido.presentacion` es un registro histórico congelado.
La unificación de nomenclatura de Arena (2026-09-07) agregó el campo "Tipo
de Arena", que nunca existió en el texto de ningún pedido pasado — sin este
documento, `especificacion_discrepante` marcaría como discrepancia activa
algo que en realidad es historia inmutable.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from inventario.hallazgos import especificacion_discrepante

pytestmark = pytest.mark.integration


def _escribir_variantes_excel(path: Path) -> None:
    df = pd.DataFrame(
        {
            "Referencia": ["PRA13"],
            "Código de barras": ["6972228791654"],
            "Especificación canónica": ["PRA13, CLASICA, 4.5KG, VAINILLA"],
            "Variante histórica": ["Presentación: PRA13, 4.5KG, VAINILLA"],
        }
    )
    df.to_excel(path, index=False)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, referencia TEXT,
           codigo_barras TEXT, presentacion TEXT, nombre_producto TEXT)"""
    )
    yield c
    c.close()


def _admin(filas):
    return pd.DataFrame(
        filas,
        columns=[
            "id_especificacion",
            "referencia",
            "codigo_barras",
            "especificacion",
            "nombre_comercial",
        ],
    )


def test_variante_historica_conocida_no_cuenta_como_discrepancia(con, tmp_path):
    ruta = tmp_path / "arena_variantes_historicas.xlsx"
    _escribir_variantes_excel(ruta)

    con.execute(
        "INSERT INTO lineas_pedido VALUES "
        "('P1','PRA13','6972228791654','Presentación: PRA13, 4.5KG, VAINILLA','x')"
    )
    admin = _admin([("E1", "PRA13", "6972228791654", "PRA13, CLASICA, 4.5KG, VAINILLA", "Arena")])

    h = especificacion_discrepante(admin, con, ruta_variantes_historicas=ruta)
    assert h.cantidad == 0


def test_sin_documento_la_misma_variante_si_cuenta(con, tmp_path):
    """Confirma que el test de arriba prueba lo que dice: sin el documento,
    la misma variante histórica SIGUE siendo una discrepancia real."""
    ruta_inexistente = tmp_path / "no_existe.xlsx"

    con.execute(
        "INSERT INTO lineas_pedido VALUES "
        "('P1','PRA13','6972228791654','Presentación: PRA13, 4.5KG, VAINILLA','x')"
    )
    admin = _admin([("E1", "PRA13", "6972228791654", "PRA13, CLASICA, 4.5KG, VAINILLA", "Arena")])

    h = especificacion_discrepante(admin, con, ruta_variantes_historicas=ruta_inexistente)
    assert h.cantidad == 1


def test_variante_no_listada_sigue_siendo_discrepancia(con, tmp_path):
    """El documento no es un pase libre para Arena entero: solo cubre las
    variantes que realmente están listadas."""
    ruta = tmp_path / "arena_variantes_historicas.xlsx"
    _escribir_variantes_excel(ruta)  # solo cubre VAINILLA, no CAFE

    con.execute(
        "INSERT INTO lineas_pedido VALUES "
        "('P1','PRA13','6932284300559','Presentación: PRA13, 4.5KG, CAFE','x')"
    )
    admin = _admin([("E1", "PRA13", "6932284300559", "PRA13, CLASICA, 4.5KG, CAFE", "Arena")])

    h = especificacion_discrepante(admin, con, ruta_variantes_historicas=ruta)
    assert h.cantidad == 1
