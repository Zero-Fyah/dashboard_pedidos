"""Orden de recorrido físico de la hoja de conteo (DEC-078).

La geometría sale de la hoja `Plano Layout`: 8 pasillos, cada uno entre un
par de racks consecutivos (A|B, C|D … O|P), con las posiciones numeradas a
lo largo del pasillo.
"""

import pandas as pd
import pytest

from inventario.ubicaciones import _orden_recorrido

pytestmark = pytest.mark.unit


def _lineas(filas):
    """filas: (rack, posicion, nivel)."""
    return pd.DataFrame(filas, columns=["rack", "posicion", "nivel"])


def _orden(filas):
    df = _lineas(filas)
    df["_o"] = _orden_recorrido(df)
    return list(
        df.sort_values("_o").apply(lambda r: f"{r['rack']}_{r['posicion']}_{r['nivel']}", axis=1)
    )


def test_el_pasillo_manda_sobre_todo():
    """Primero se termina un pasillo y después se pasa al siguiente."""
    orden = _orden([("C", 1, 4), ("A", 50, 4)])

    assert orden == ["A_50_4", "C_1_4"]


def test_los_racks_del_mismo_par_comparten_pasillo():
    """A y B están enfrentados: se cuentan en la misma pasada, no en dos."""
    orden = _orden([("B", 5, 4), ("A", 5, 4), ("C", 1, 4)])

    assert orden[:2] == ["A_5_4", "B_5_4"]
    assert orden[2] == "C_1_4"


def test_dentro_del_pasillo_avanza_por_posicion():
    orden = _orden([("A", 30, 4), ("A", 1, 4), ("A", 15, 4)])

    assert orden == ["A_1_4", "A_15_4", "A_30_4"]


def test_la_serpentina_invierte_el_pasillo_par():
    """Quien termina el pasillo 1 en la posición 50 entra al 2 por ese mismo
    extremo. Recorrer todo en el mismo sentido obliga a volver caminando en
    vacío la longitud del rack en cada cambio."""
    orden = _orden([("C", 1, 4), ("C", 50, 4), ("A", 1, 4), ("A", 50, 4)])

    assert orden == ["A_1_4", "A_50_4", "C_50_4", "C_1_4"]


def test_en_la_misma_posicion_sube_por_altura():
    orden = _orden([("A", 1, 7), ("A", 1, 4), ("A", 1, 5)])

    assert orden == ["A_1_4", "A_1_5", "A_1_7"]


def test_un_rack_fuera_del_plano_va_al_final():
    """`PU` no está dibujado. Mejor visible y último que fuera de orden."""
    orden = _orden([("PU", 1, 1), ("P", 50, 4), ("A", 1, 4)])

    assert orden[-1] == "PU_1_1"


def test_los_ocho_pasillos_salen_en_orden():
    """A|B=1, C|D=2 … O|P=8: el recorrido va de una punta de la bodega a la
    otra sin saltar."""
    filas = [(r, 1, 4) for r in "ABCDEFGHIJKLMNOP"]
    df = _lineas(filas)
    df["_o"] = _orden_recorrido(df)
    pasillos = (df.sort_values("_o")["_o"] // 1_000_000).tolist()

    assert pasillos == sorted(pasillos)
    assert pasillos == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]


def test_es_estable_y_sin_empates_dentro_de_una_linea():
    """Dos líneas distintas nunca comparten orden: la hoja tendría filas
    intercambiables y el recorrido dejaría de ser reproducible."""
    filas = [(r, p, h) for r in "ABCD" for p in (1, 25, 50) for h in (4, 5, 6, 7)]
    df = _lineas(filas)
    df["_o"] = _orden_recorrido(df)

    assert df["_o"].is_unique
