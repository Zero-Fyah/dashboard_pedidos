"""Detector de producto mal ubicado según la política de slotting (DEC-077).

La hoja `Distribución` del layout declara qué familia va en qué rack. Estuvo
en el archivo desde siempre y el pipeline nunca la había abierto.
"""

import pandas as pd
import pytest

from inventario.comparacion import anomalias_layout
from inventario.layout import FAMILIA_AVERIAS, TIPO_ALTURA, TIPO_PICKING

pytestmark = pytest.mark.unit

POLITICA = {
    "A": frozenset({"PS"}),
    "D": frozenset({FAMILIA_AVERIAS}),
    "M": frozenset({"PB", "PJ"}),
}


def _linea(ubicacion, rack, referencia, tipo=TIPO_PICKING, cantidad=10.0, activa="SI"):
    return {
        "ubicacion": ubicacion,
        "rack": rack,
        "referencia": referencia,
        "id_especificacion": f"ID{referencia}",
        "tipo": tipo,
        "cantidad": cantidad,
        "activa": activa,
        "altura": 1,
        "estiba_completa": False,
    }


def _anomalias(filas, politica=POLITICA):
    return anomalias_layout(pd.DataFrame(filas), politica)


def _motivos(filas, politica=POLITICA):
    r = _anomalias(filas, politica)
    return set(r["motivo"]) if not r.empty else set()


def test_el_producto_en_su_rack_no_es_anomalia():
    assert _motivos([_linea("A_1_1", "A", "PS10")]) == set()


def test_el_producto_en_otro_rack_se_detecta():
    r = _anomalias([_linea("A_1_1", "A", "PJ50")])

    assert list(r["motivo"]) == ["familia_fuera_de_rack"]
    assert r.iloc[0]["ubicacion"] == "A_1_1"


def test_un_rack_con_dos_familias_acepta_las_dos():
    filas = [_linea("M_1_1", "M", "PB10"), _linea("M_2_1", "M", "PJ20")]

    assert _motivos(filas) == set()


def test_la_averia_es_conforme_en_el_rack_de_averias():
    """`familia_de()` clasifica "PJ91 AVERIA" como PJ, por el prefijo de su
    producto de origen. Sin el caso especial saldría mal ubicada justo en el
    rack donde debe estar."""
    assert _motivos([_linea("D_1_1", "D", "PJ91 AVERIA")]) == set()


def test_un_producto_sano_en_el_rack_de_averias_si_se_detecta():
    assert _motivos([_linea("D_1_1", "D", "PS10")]) == {"familia_fuera_de_rack"}


def test_la_altura_no_se_evalua():
    """La política es de picking: la hoja se titula "Distribución picking por
    familias". La altura es almacenamiento masivo, sin asignación por familia."""
    assert _motivos([_linea("A_1_5", "A", "PJ50", tipo=TIPO_ALTURA)]) == set()


def test_una_referencia_sin_familia_reconocida_no_se_marca():
    """No es producto mal ubicado: es producto que nadie sabe qué es. Ese
    problema tiene su propio detector (DEC-072) y mezclarlos inflaría este."""
    assert _motivos([_linea("A_1_1", "A", "M140-A021")]) == set()


def test_un_rack_sin_politica_no_se_evalua():
    """No hay contra qué comparar."""
    assert _motivos([_linea("Z_1_1", "Z", "PJ50")]) == set()


def test_sin_politica_los_otros_motivos_siguen_funcionando():
    """Una copia vieja del layout no trae la hoja: el detector se apaga sin
    llevarse los otros tres."""
    filas = [_linea("A_1_1", "A", "PJ50", activa="NO")]

    assert _motivos(filas, politica={}) == {"posicion_no_habilitada"}
    assert _motivos(filas, politica=None) == {"posicion_no_habilitada"}


def test_sin_stock_no_hay_anomalia():
    assert _motivos([_linea("A_1_1", "A", "PJ50", cantidad=0.0)]) == set()


def test_el_motivo_de_posicion_manda_sobre_el_de_familia():
    """Una posición deshabilitada es un problema del contenedor; la familia,
    del contenido. Reportar la primera es más accionable: hay que vaciar la
    posición igual, sin importar qué familia sea."""
    r = _anomalias([_linea("A_1_1", "A", "PJ50", activa="NO")])

    assert list(r["motivo"]) == ["posicion_no_habilitada"]
