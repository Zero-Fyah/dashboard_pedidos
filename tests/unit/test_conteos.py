"""Tests de `inventario.conteos` — ingesta de conteos físicos (DEC-058)."""

import pandas as pd
import pytest

from comun import CAUSAS_DISCREPANCIA, COLUMNAS_CONTEO_REQUERIDAS, COLUMNAS_HOJA_CONTEO
from inventario.conteos import (
    calcular_ira,
    cargar_conteos,
    conformidad_por_tipo,
    evaluar_conteos,
    ira_por_periodo,
)


@pytest.fixture
def ubicaciones():
    """Lo que el sistema cree que hay, con clases de tolerancia distinta."""
    return pd.DataFrame(
        [
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "1",
                "cantidad": 100.0,
                "clase": "A",
                "tipo": "Altura",
            },
            {
                "ubicacion": "A_1_5",
                "id_especificacion": "2",
                "cantidad": 100.0,
                "clase": "C",
                "tipo": "Altura",
            },
            {
                "ubicacion": "A_2_6",
                "id_especificacion": "3",
                "cantidad": 50.0,
                "clase": "B",
                "tipo": "Picking",
            },
        ]
    )


def _conteo(ubicacion, id_esp, cantidad, fecha="2026-07-27", quien="ANA"):
    return {
        "ubicacion": ubicacion,
        "id_especificacion": id_esp,
        "cantidad_contada": cantidad,
        "fecha": fecha,
        "contado_por": quien,
        "archivo": "hoja.xlsx",
    }


def test_conteo_exacto_es_exacto(ubicaciones):
    crudo = pd.DataFrame([_conteo("A_1_5", "1", 100)])
    r = evaluar_conteos(crudo, ubicaciones)

    assert r.iloc[0]["exacta"] == 1
    assert r.iloc[0]["diferencia"] == 0
    assert r.iloc[0]["hallazgo"] == "Coincide"


def test_la_tolerancia_depende_de_la_clase(ubicaciones):
    """La misma desviación del 3% pasa en clase C (±5%) y falla en A (±1%)."""
    crudo = pd.DataFrame([_conteo("A_1_5", "1", 103), _conteo("A_1_5", "2", 103)])
    r = evaluar_conteos(crudo, ubicaciones).set_index("id_especificacion")

    assert r.loc["1", "exacta"] == 0  # clase A
    assert r.loc["2", "exacta"] == 1  # clase C


def test_faltante_y_sobrante_se_distinguen(ubicaciones):
    """El plan los separa porque tienen causas distintas."""
    crudo = pd.DataFrame([_conteo("A_1_5", "1", 50), _conteo("A_2_6", "3", 200)])
    r = evaluar_conteos(crudo, ubicaciones).set_index("id_especificacion")

    assert r.loc["1", "hallazgo"] == "Faltante"
    assert r.loc["3", "hallazgo"] == "Sobrante"


def test_contar_algo_que_el_sistema_no_tiene_es_sobrante(ubicaciones):
    """Producto en una posición donde el sistema no lo registra: esperado cero."""
    crudo = pd.DataFrame([_conteo("A_9_9", "99", 25)])
    r = evaluar_conteos(crudo, ubicaciones)

    assert r.iloc[0]["cantidad_sistema"] == 0
    assert r.iloc[0]["diferencia"] == 25
    assert r.iloc[0]["exacta"] == 0


def test_cero_contra_cero_no_es_discrepancia(ubicaciones):
    """Posición vacía confirmada vacía. Sin esto, la división daría NaN y
    el `fillna(False)` la marcaría como error."""
    crudo = pd.DataFrame([_conteo("A_9_9", "99", 0)])
    r = evaluar_conteos(crudo, ubicaciones)

    assert r.iloc[0]["exacta"] == 1


def test_filas_ilegibles_se_descartan_sin_tumbar_el_resto(ubicaciones):
    crudo = pd.DataFrame(
        [
            _conteo("A_1_5", "1", 100),
            _conteo("A_2_6", "3", "no es un numero"),
            _conteo("A_1_5", "2", 100, fecha="fecha invalida"),
        ]
    )
    r = evaluar_conteos(crudo, ubicaciones)

    assert len(r) == 1


def test_recuento_del_mismo_dia_y_persona_gana_el_ultimo(ubicaciones):
    """Corregir una hoja mal llenada no puede dejar las dos versiones."""
    crudo = pd.DataFrame([_conteo("A_1_5", "1", 50), _conteo("A_1_5", "1", 100)])
    r = evaluar_conteos(crudo, ubicaciones)

    assert len(r) == 1
    assert r.iloc[0]["cantidad_contada"] == 100


def test_dos_personas_el_mismo_dia_son_dos_conteos(ubicaciones):
    """La doble verificación del plan exige que ambos conteos sobrevivan."""
    crudo = pd.DataFrame(
        [_conteo("A_1_5", "1", 100, quien="ANA"), _conteo("A_1_5", "1", 90, quien="LUIS")]
    )
    r = evaluar_conteos(crudo, ubicaciones)

    assert len(r) == 2


def test_ira_agrupa_las_clases_heredadas_con_su_base(ubicaciones):
    """`A (por referencia)` es clase A para efectos de exactitud."""
    ubic = ubicaciones.copy()
    ubic.loc[0, "clase"] = "A (por referencia)"
    r = calcular_ira(evaluar_conteos(pd.DataFrame([_conteo("A_1_5", "1", 100)]), ubic))

    assert set(r["clase"]) == {"A", "Global"}


def test_ira_no_confunde_sin_rotacion_con_clase(ubicaciones):
    """Agrupar por la inicial mandaría 'Sin rotación' a una clase 'S'."""
    ubic = ubicaciones.copy()
    ubic.loc[0, "clase"] = "Sin rotación"
    r = calcular_ira(evaluar_conteos(pd.DataFrame([_conteo("A_1_5", "1", 100)]), ubic))

    assert "Sin clase" in set(r["clase"])
    assert "S" not in set(r["clase"])


def test_ira_es_exactas_sobre_contadas(ubicaciones):
    crudo = pd.DataFrame(
        [_conteo("A_1_5", "1", 100), _conteo("A_2_6", "3", 200), _conteo("A_1_5", "2", 100)]
    )
    r = calcular_ira(evaluar_conteos(crudo, ubicaciones)).set_index("clase")

    assert r.loc["Global", "contadas"] == 3
    assert r.loc["Global", "exactas"] == 2
    assert r.loc["Global", "ira"] == pytest.approx(66.7, abs=0.1)


def test_sin_conteos_no_explota(ubicaciones):
    assert evaluar_conteos(pd.DataFrame(), ubicaciones).empty
    assert calcular_ira(pd.DataFrame()).empty


def test_carpeta_inexistente_devuelve_vacio(tmp_path):
    assert cargar_conteos(tmp_path / "no_existe").empty


def test_archivo_sin_las_columnas_del_contrato_se_ignora(tmp_path):
    """Se rechaza el archivo entero, no se ingiere medio conteo."""
    pd.DataFrame([{"cualquier_cosa": 1}]).to_excel(tmp_path / "malo.xlsx", index=False)
    pd.DataFrame([_conteo("A_1_5", "1", 100)]).to_excel(tmp_path / "bueno.xlsx", index=False)

    r = cargar_conteos(tmp_path)

    assert len(r) == 1
    assert r.iloc[0]["archivo"] == "bueno.xlsx"


def test_la_hoja_emitida_cumple_el_contrato_de_ingesta():
    """El generador (dashboard) y el lector (pipeline) comparten `comun/`.

    Si alguien quitara una columna requerida del layout de la hoja, el
    pipeline rechazaría todos los archivos que el dashboard emite.
    """
    assert set(COLUMNAS_CONTEO_REQUERIDAS) <= set(COLUMNAS_HOJA_CONTEO)


def test_la_hoja_no_revela_la_cantidad_del_sistema():
    """Conteo ciego: si la hoja mostrara lo esperado, se confirma en vez de contar."""
    assert not {"cantidad", "cantidad_sistema", "existencia"} & set(COLUMNAS_HOJA_CONTEO)


# ─────────────────────────────────────────────
# Causa, serie temporal y conformidad (DEC-062)
# ─────────────────────────────────────────────


def test_la_causa_solo_sobrevive_donde_hubo_diferencia(ubicaciones):
    """Una causa en un conteo que coincide ensuciaría el Pareto con causas de nada."""
    crudo = pd.DataFrame(
        [
            {**_conteo("A_1_5", "1", 100), "causa": "Error de despacho"},  # exacto
            {**_conteo("A_1_5", "2", 50), "causa": "Mal ubicado"},  # discrepa
        ]
    )
    r = evaluar_conteos(crudo, ubicaciones).set_index("id_especificacion")

    assert pd.isna(r.loc["1", "causa"])
    assert r.loc["2", "causa"] == "Mal ubicado"


def test_el_conteo_arrastra_el_tipo_de_ubicacion(ubicaciones):
    """Sin `tipo` no se puede separar la conformidad de picking de la de altura."""
    crudo = pd.DataFrame([_conteo("A_1_5", "1", 100), _conteo("A_2_6", "3", 50)])
    r = evaluar_conteos(crudo, ubicaciones).set_index("ubicacion")

    assert r.loc["A_1_5", "tipo"] == "Altura"
    assert r.loc["A_2_6", "tipo"] == "Picking"


def test_ira_por_periodo_agrupa_por_mes(ubicaciones):
    crudo = pd.DataFrame(
        [
            _conteo("A_1_5", "1", 100, fecha="2026-06-10"),
            _conteo("A_1_5", "2", 100, fecha="2026-07-10"),
            _conteo("A_2_6", "3", 999, fecha="2026-07-11"),
        ]
    )
    r = ira_por_periodo(evaluar_conteos(crudo, ubicaciones))
    global_ = r[r["clase"] == "Global"].set_index("periodo")

    assert set(global_.index) == {"2026-06", "2026-07"}
    assert global_.loc["2026-06", "ira"] == 100.0
    assert global_.loc["2026-07", "ira"] == 50.0


def test_ira_por_periodo_sin_conteos_no_explota():
    assert ira_por_periodo(pd.DataFrame()).empty


def test_la_conformidad_se_mide_por_posicion_no_por_linea(ubicaciones):
    """Una posición con dos líneas y una mal es NO conforme, no «50% conforme».

    Es la diferencia con el IRA, que es a nivel de línea, y es como el plan
    define la Tasa de Conformidad en Auditoría de Picking.
    """
    crudo = pd.DataFrame(
        [
            _conteo("A_1_5", "1", 100),  # exacto
            _conteo("A_1_5", "2", 50),  # discrepa -> tumba toda la posición
        ]
    )
    r = conformidad_por_tipo(evaluar_conteos(crudo, ubicaciones)).set_index("tipo")

    assert r.loc["Altura", "posiciones"] == 1
    assert r.loc["Altura", "conformes"] == 0
    assert r.loc["Altura", "conformidad"] == 0.0


def test_la_conformidad_separa_picking_de_altura(ubicaciones):
    """Son dos gobernanzas distintas: picking se le reporta a Bodega."""
    crudo = pd.DataFrame(
        [
            _conteo("A_1_5", "1", 100),  # altura, coincide
            _conteo("A_2_6", "3", 200),  # picking, el sistema tiene 50
        ]
    )
    r = conformidad_por_tipo(evaluar_conteos(crudo, ubicaciones)).set_index("tipo")

    assert r.loc["Altura", "conformidad"] == 100.0
    assert r.loc["Picking", "conformidad"] == 0.0


def test_conformidad_sin_conteos_no_explota():
    assert conformidad_por_tipo(pd.DataFrame()).empty


def test_las_causas_son_un_vocabulario_cerrado():
    """En texto libre, tres formas de escribir lo mismo son tres causas
    distintas para el Pareto y el análisis mensual no agrupa nada."""
    assert len(CAUSAS_DISCREPANCIA) >= 5
    assert len(set(CAUSAS_DISCREPANCIA)) == len(CAUSAS_DISCREPANCIA)
    assert "causa" in COLUMNAS_HOJA_CONTEO
