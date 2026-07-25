"""Tests de inventario/layout.py (DEC-041).

pd.read_excel se monkeypatchea con DataFrames en memoria — mismo criterio
que test_inventario_normalizador.py: sin tocar el filesystem, así la
carga/validación del layout queda en tests/unit/. El resto
(clasificar_ubicaciones, solo_layout, resumen_por_tipo) son funciones
puras sobre DataFrames y se testean directo.

El layout de muestra reproduce en miniatura las tres formas reales:
- rack A pos 1: estiba completa (SI/NO/NO) + alturas 4-7 de Altura
- rack A pos 2: picking normal (SI/SI/SI)
- rack A pos 3: posición no habilitada (NO/NO/NO)
- rack B pos 25: Paso Montacarga (alturas 1-4)
"""

from pathlib import Path

import pandas as pd
import pytest

from inventario.layout import (
    TIPO_ALTURA,
    TIPO_FUERA,
    TIPO_PASO,
    TIPO_PICKING,
    cargar_layout,
    clasificar_ubicaciones,
    resumen_por_tipo,
    solo_layout,
)

pytestmark = pytest.mark.unit


def _fila(rack, posicion, altura, tipo, activa, subbodega="bodega4"):
    return {
        "CEDI": "MOSQUERA",
        "Subbodega": subbodega,
        "Ubicación": f"{rack}_{posicion}_{altura}",
        "Tipo Ubicación": tipo,
        # DEC-017: el nombre real de la empresa no va en texto plano.
        "Empresa": "miempresa",
        "Activa Para Almacenar": activa,
        "Rack": rack,
        "Posición": posicion,
        "Altura": altura,
    }


def _layout_raw() -> pd.DataFrame:
    filas = []
    # A_1_*: estiba completa (SI/NO/NO) y su altura
    filas += [_fila("A", 1, 1, TIPO_PICKING, "SI")]
    filas += [_fila("A", 1, h, TIPO_PICKING, "NO") for h in (2, 3)]
    filas += [_fila("A", 1, h, TIPO_ALTURA, "SI") for h in (4, 5, 6, 7)]
    # A_2_*: picking normal
    filas += [_fila("A", 2, h, TIPO_PICKING, "SI") for h in (1, 2, 3)]
    # A_3_*: posición no habilitada del todo
    filas += [_fila("A", 3, h, TIPO_PICKING, "NO") for h in (1, 2, 3)]
    # B_25_*: paso de montacarga (alturas 1-4)
    filas += [_fila("B", 25, h, TIPO_PASO, "NO") for h in (1, 2, 3, 4)]
    return pd.DataFrame(filas)


@pytest.fixture
def layout_read_excel(monkeypatch):
    monkeypatch.setattr(
        "inventario.layout.pd.read_excel", lambda _path, sheet_name=None: _layout_raw()
    )
    monkeypatch.setattr(Path, "exists", lambda _self: True)


@pytest.fixture
def layout(layout_read_excel) -> pd.DataFrame:
    return cargar_layout(Path("irrelevante.xlsx"))


def test_cargar_layout_normaliza_columnas(layout):
    esperadas = {
        "cedi",
        "subbodega",
        "ubicacion",
        "tipo",
        "empresa",
        "activa",
        "rack",
        "posicion",
        "altura",
        "estiba_completa",
    }
    assert esperadas <= set(layout.columns)
    assert all(isinstance(u, str) for u in layout["ubicacion"])
    assert len(layout) == 17


def test_cargar_layout_reconoce_paso_montacarga(layout):
    """`Paso Montacarga` es un tipo de primera clase, no un desconocido."""
    paso = layout[layout["tipo"] == TIPO_PASO]
    assert len(paso) == 4
    assert set(paso["ubicacion"]) == {"B_25_1", "B_25_2", "B_25_3", "B_25_4"}


def test_estiba_completa_distingue_de_posicion_deshabilitada(layout):
    """SI/NO/NO es estiba; NO/NO/NO es posición no habilitada (DEC-041)."""
    por_ubicacion = layout.set_index("ubicacion")["estiba_completa"]
    # A_1: altura 1 habilitada -> las 3 alturas de picking son estiba
    assert por_ubicacion["A_1_1"]
    assert por_ubicacion["A_1_2"]
    assert por_ubicacion["A_1_3"]
    # A_3: altura 1 deshabilitada -> no es estiba, es posición muerta
    assert not por_ubicacion["A_3_1"]
    assert not por_ubicacion["A_3_2"]
    # Las alturas (4-7) nunca son estiba, aunque compartan posición
    assert not por_ubicacion["A_1_4"]
    # El paso de montacarga tampoco
    assert not por_ubicacion["B_25_1"]


def test_cargar_layout_falla_ante_ubicaciones_duplicadas(monkeypatch):
    """Un duplicado multiplicaría filas de Bochica en el cruce: falla duro."""
    duplicado = pd.concat([_layout_raw(), _layout_raw().head(1)], ignore_index=True)
    monkeypatch.setattr("inventario.layout.pd.read_excel", lambda _path, sheet_name=None: duplicado)
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    with pytest.raises(ValueError, match="duplicadas"):
        cargar_layout(Path("irrelevante.xlsx"))


def test_cargar_layout_falla_ante_columnas_faltantes(monkeypatch):
    incompleto = _layout_raw().drop(columns=["Tipo Ubicación"])
    monkeypatch.setattr(
        "inventario.layout.pd.read_excel", lambda _path, sheet_name=None: incompleto
    )
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    with pytest.raises(ValueError, match="Tipo Ubicación"):
        cargar_layout(Path("irrelevante.xlsx"))


def test_cargar_layout_falla_si_no_existe(monkeypatch):
    """El layout es manual: si falta, el mensaje debe decir que no lo baja el scraper."""
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    with pytest.raises(FileNotFoundError, match="manual"):
        cargar_layout(Path("no_existe.xlsx"))


def test_cargar_layout_avisa_ante_tipo_desconocido(monkeypatch, caplog):
    """Un tipo nuevo no rompe la carga, pero no pasa en silencio."""
    raro = _layout_raw()
    raro.loc[0, "Tipo Ubicación"] = "Zona Nueva"
    monkeypatch.setattr("inventario.layout.pd.read_excel", lambda _path, sheet_name=None: raro)
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    with caplog.at_level("WARNING"):
        cargar_layout(Path("irrelevante.xlsx"))
    assert "Zona Nueva" in caplog.text


def test_cargar_layout_avisa_si_cambia_la_geometria_de_picking(monkeypatch, caplog):
    """La regla altura 1-3 = Picking se lee de la columna, pero se vigila."""
    movido = _layout_raw()
    movido.loc[movido["Ubicación"] == "A_1_5", "Tipo Ubicación"] = TIPO_PICKING
    monkeypatch.setattr("inventario.layout.pd.read_excel", lambda _path, sheet_name=None: movido)
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    with caplog.at_level("WARNING"):
        cargar_layout(Path("irrelevante.xlsx"))
    assert "geometría del layout cambió" in caplog.text


def _bochica(ubicaciones, cantidades):
    return pd.DataFrame(
        {
            "id_especificacion": [str(1000 + i) for i in range(len(ubicaciones))],
            "ubicacion": ubicaciones,
            "cantidad": cantidades,
        }
    )


def test_clasificar_ubicaciones_marca_fuera_de_layout(layout):
    """Lo que no está en el layout no se descarta: queda visible y marcado."""
    bochica = _bochica(["A_1_1", "A_1_5", "B_25_2", "Q_1_1"], [10, 20, 30, 40])
    clasificado = clasificar_ubicaciones(bochica, layout)
    assert list(clasificado["tipo"]) == [TIPO_PICKING, TIPO_ALTURA, TIPO_PASO, TIPO_FUERA]
    assert clasificado["tipo"].notna().all()


def test_clasificar_ubicaciones_no_normaliza_mayusculas(layout):
    """DEC-039 pregunta 2: el match es exacto. 'a_1_1' no es 'A_1_1'."""
    clasificado = clasificar_ubicaciones(_bochica(["a_1_1"], [10]), layout)
    assert clasificado.loc[0, "tipo"] == TIPO_FUERA


def test_solo_layout_descarta_lo_de_afuera(layout):
    bochica = _bochica(["A_1_1", "A_1_5", "B_25_2", "Q_1_1"], [10, 20, 30, 40])
    dentro = solo_layout(clasificar_ubicaciones(bochica, layout))
    assert len(dentro) == 3
    assert TIPO_FUERA not in set(dentro["tipo"])
    assert dentro["cantidad"].sum() == 60


def test_solo_layout_conserva_paso_montacarga(layout):
    """El paso está en el layout: se conserva acá y se excluye en la fórmula."""
    dentro = solo_layout(clasificar_ubicaciones(_bochica(["B_25_2"], [30]), layout))
    assert list(dentro["tipo"]) == [TIPO_PASO]


def test_resumen_por_tipo_agrega_filas_y_unidades(layout):
    bochica = _bochica(["A_1_1", "A_2_1", "Q_1_1"], [10, 5, 40])
    resumen = resumen_por_tipo(clasificar_ubicaciones(bochica, layout))
    assert resumen.loc[TIPO_PICKING, "filas"] == 2
    assert resumen.loc[TIPO_PICKING, "unidades"] == 15
    assert resumen.loc[TIPO_FUERA, "unidades"] == 40
