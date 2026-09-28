"""combinar_ids_incremental() — cómo se unen los 4 carriles del incremental
y quién se fuerza a modo completo (DEC-140/159).

Función pura: sin I/O, se testea directo con listas.
"""

import pytest

from scraper.orquestador import combinar_ids_incremental

pytestmark = pytest.mark.unit


def test_sin_solapamiento_se_combinan_todos():
    pendientes, forzar, n_nuevos = combinar_ids_incremental(["A1"], ["E1"], ["N1"], ["R1", "R2"])
    assert pendientes == ["A1", "E1", "N1", "R1", "R2"]
    assert forzar == frozenset({"R1", "R2"})
    assert n_nuevos == 2


def test_candidato_ya_activo_se_fuerza_a_completo_igual():
    """DEC-159: el bug real — un candidato de re-extracción que ya está en
    ids_activos (el caso del 100% de los 1.076 pendientes al 2026-09-28)
    antes se excluía de `ids_forzar_completo` sin querer, y nunca se
    re-extraía en modo completo. Ahora se fuerza igual."""
    pendientes, forzar, n_nuevos = combinar_ids_incremental(
        ["ACTIVO_Y_CANDIDATO"], [], [], ["ACTIVO_Y_CANDIDATO"]
    )
    assert "ACTIVO_Y_CANDIDATO" in forzar


def test_candidato_ya_en_cola_no_se_duplica_en_pendientes():
    pendientes, forzar, n_nuevos = combinar_ids_incremental(
        ["ACTIVO_Y_CANDIDATO"], [], [], ["ACTIVO_Y_CANDIDATO"]
    )
    assert pendientes == ["ACTIVO_Y_CANDIDATO"]
    assert pendientes.count("ACTIVO_Y_CANDIDATO") == 1


def test_candidato_ya_en_cola_no_cuenta_como_nuevo_en_el_carril():
    """El desglose por carril del resumen no debe inflar 'reextraccion' con
    pedidos que ya se contaron en 'activos'/'errores'/'nuevos'."""
    _, _, n_nuevos = combinar_ids_incremental(["A1", "A2"], [], [], ["A1", "R_NUEVO"])
    assert n_nuevos == 1


@pytest.mark.parametrize("indice_carril", [0, 1, 2])
def test_candidato_se_fuerza_sin_importar_en_cual_carril_ya_estaba(indice_carril):
    carriles = [[], [], []]
    carriles[indice_carril] = ["X"]
    activos, error, nuevos = carriles
    _, forzar, n_nuevos = combinar_ids_incremental(activos, error, nuevos, ["X"])
    assert "X" in forzar
    assert n_nuevos == 0


def test_sin_candidatos_de_reextraccion_no_fuerza_nada():
    pendientes, forzar, n_nuevos = combinar_ids_incremental(["A1"], ["E1"], ["N1"], [])
    assert forzar == frozenset()
    assert n_nuevos == 0
    assert pendientes == ["A1", "E1", "N1"]


def test_todo_vacio_da_todo_vacio():
    pendientes, forzar, n_nuevos = combinar_ids_incremental([], [], [], [])
    assert pendientes == []
    assert forzar == frozenset()
    assert n_nuevos == 0


def test_preserva_el_orden_activos_errores_nuevos_reextraccion():
    pendientes, _, _ = combinar_ids_incremental(["A"], ["E"], ["N"], ["R"])
    assert pendientes == ["A", "E", "N", "R"]
