"""Test de `aplicar_nombres_canonicos_arena` contra un .xlsx real (no monkeypatch).

Va en integration/ por el mismo criterio que
`test_inventario_normalizador_excel.py`: el comportamiento depende de leer un
archivo real en disco (`ruta.exists()` + `pd.read_excel`), no solo de la
forma del DataFrame en memoria.

Contexto: la unificación de nomenclatura de producto de Arena (2026-09-07,
decisión del Arquitecto) se hizo en el sistema origen, pero la descarga del
admin no siempre la refleja — confirmado contra corridas reales que el
almacén/modalidad desfasado cambia de una corrida a otra. Este archivo de
negocio (`data/inventario/arena_nombres_canonicos.xlsx`, no versionado,
DEC-039) es la fuente de verdad que reemplaza el texto crudo del origen por
código de barras, sin depender de que esa sincronización esté al día.
"""

from pathlib import Path

import pandas as pd
import pytest

from inventario.normalizador import aplicar_nombres_canonicos_arena

pytestmark = pytest.mark.integration


def _escribir_canonicos_excel(path: Path) -> None:
    df = pd.DataFrame(
        {
            "Código de barras": ["6972228791654", "6932284300559"],
            "Especificación": [
                "PRA13, CLASICA, 4.5KG, VAINILLA",
                "PRA13, CLASICA, 4.5KG, CAFE",
            ],
        }
    )
    df.to_excel(path, index=False)


def test_sobrescribe_solo_los_codigos_de_barras_conocidos(tmp_path):
    ruta = tmp_path / "arena_nombres_canonicos.xlsx"
    _escribir_canonicos_excel(ruta)

    catalogo = pd.DataFrame(
        {
            "codigo_barras": ["6972228791654", "6932284300559", "PB02"],
            "especificacion": [
                ": PRA13, 4.5KG, VAINILLA",  # Pereira, formato viejo/incompleto
                "Presentación: PRA13, CLASICA, 4.5KG, CAFE;",  # ya correcto, otro envoltorio
                "Talla: M;",  # producto ajeno a Arena — no debe tocarse
            ],
        }
    )

    resultado = aplicar_nombres_canonicos_arena(catalogo, ruta=ruta)

    assert resultado["especificacion"].tolist() == [
        "PRA13, CLASICA, 4.5KG, VAINILLA",
        "PRA13, CLASICA, 4.5KG, CAFE",
        "Talla: M;",
    ]


def test_sin_archivo_es_no_op(tmp_path):
    ruta = tmp_path / "no_existe.xlsx"
    catalogo = pd.DataFrame(
        {"codigo_barras": ["6972228791654"], "especificacion": [": PRA13, 4.5KG, VAINILLA"]}
    )

    resultado = aplicar_nombres_canonicos_arena(catalogo, ruta=ruta)

    assert resultado["especificacion"].tolist() == [": PRA13, 4.5KG, VAINILLA"]


def test_no_toca_el_dataframe_original(tmp_path):
    ruta = tmp_path / "arena_nombres_canonicos.xlsx"
    _escribir_canonicos_excel(ruta)
    catalogo = pd.DataFrame(
        {"codigo_barras": ["6972228791654"], "especificacion": [": PRA13, 4.5KG, VAINILLA"]}
    )

    aplicar_nombres_canonicos_arena(catalogo, ruta=ruta)

    assert catalogo["especificacion"].tolist() == [": PRA13, 4.5KG, VAINILLA"]
