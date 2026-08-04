"""Tests de `dashboard/filtros.py` — los filtros globales (DEC-101).

Se prueban las dos funciones puras (`aplicar` y `recortar`). `barra_lateral()`
dibuja widgets y su comportamiento de persistencia se verifica con AppTest, no
acá.

El test que más importa es el de la dimensión ausente: `aplicar()` se llama
sobre consultas que devuelven distintos juegos de columnas —`get_comprobantes`
no trae `vendedor`, `get_auditoria_pago` no trae `nombre_empresa`— y tiene que
ignorar en silencio lo que no puede filtrar. Si en cambio reventara, cada página
tendría que envolver la llamada en condicionales; y si filtrara mal, mostraría
un recorte que el usuario cree aplicado y no lo está.
"""

import pandas as pd
import pytest

from dashboard.filtros import Filtros, aplicar, recortar

TODOS = Filtros("2026-01-01", "2026-08-03", (), (), (), ())


def _df():
    return pd.DataFrame(
        {
            "id_pedido": ["1", "2", "3", "4"],
            "vendedor": ["ANA", "BETO", "ANA", "CARLA"],
            "nombre_empresa": ["ACME", "ACME", "GLOBEX", "GLOBEX"],
            "metodo_entrega": ["Ruta", "Transportadora", "Ruta", "Almacen"],
            "forma_pago": ["Pago inmediato", "Pago a crédito", "Pago inmediato", "Pago inmediato"],
        }
    )


# ── aplicar ────────────────────────────────────────────────────────────────────


def test_sin_dimensiones_no_filtra_nada():
    assert len(aplicar(_df(), TODOS)) == 4


def test_filtra_por_vendedor():
    f = TODOS._replace(vendedores=("ANA",))
    assert list(aplicar(_df(), f)["id_pedido"]) == ["1", "3"]


def test_las_dimensiones_se_combinan_con_and():
    """Dos filtros acotan más, no menos: es una intersección, no una unión."""
    f = TODOS._replace(vendedores=("ANA",), clientes=("GLOBEX",))
    assert list(aplicar(_df(), f)["id_pedido"]) == ["3"]


def test_varios_valores_de_una_dimension_se_combinan_con_or():
    f = TODOS._replace(canales=("Ruta", "Almacen"))
    assert list(aplicar(_df(), f)["id_pedido"]) == ["1", "3", "4"]


def test_una_dimension_sin_columna_se_ignora_en_silencio():
    """`get_comprobantes` no trae `vendedor`: la llamada no puede reventar."""
    sin_vendedor = _df().drop(columns=["vendedor"])
    f = TODOS._replace(vendedores=("ANA",), clientes=("ACME",))

    vista = aplicar(sin_vendedor, f)

    assert list(vista["id_pedido"]) == ["1", "2"], "aplicó el cliente pero ignoró el vendedor"


def test_dataframe_vacio_no_rompe():
    assert aplicar(pd.DataFrame(), TODOS._replace(vendedores=("ANA",))).empty


def test_no_muta_el_dataframe_original():
    """El original viene de `st.cache_data`: mutarlo contaminaría la caché."""
    df = _df()
    aplicar(df, TODOS._replace(vendedores=("ANA",)))
    assert len(df) == 4


def test_un_valor_que_no_existe_deja_el_resultado_vacio():
    """No hay 'sin coincidencias = todos': eso ocultaría un filtro mal puesto."""
    assert aplicar(_df(), TODOS._replace(vendedores=("NADIE",))).empty


# ── recortar ───────────────────────────────────────────────────────────────────


def test_no_recorta_si_el_rango_ya_cabe():
    f, cambio = recortar(Filtros("2026-07-01", "2026-07-20", (), (), (), ()), 31)
    assert not cambio
    assert f.desde == "2026-07-01"


def test_recorta_conservando_el_extremo_reciente():
    """Se acota hacia atrás: lo último es lo que interesa en una vista operativa."""
    f, cambio = recortar(Filtros("2026-01-01", "2026-08-03", (), (), (), ()), 31)
    assert cambio
    assert f.hasta == "2026-08-03"
    assert f.desde == "2026-07-03"


def test_el_recorte_no_toca_las_dimensiones():
    original = Filtros("2026-01-01", "2026-08-03", ("ANA",), ("ACME",), ("Ruta",), ())
    f, _ = recortar(original, 31)
    assert f.vendedores == ("ANA",)
    assert f.clientes == ("ACME",)
    assert f.canales == ("Ruta",)


def test_el_borde_exacto_no_se_recorta():
    f, cambio = recortar(Filtros("2026-07-03", "2026-08-03", (), (), (), ()), 31)
    assert not cambio


# ── hay_dimensiones ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("campo", "esperado"),
    [("vendedores", True), ("clientes", True), ("canales", True), ("formas_pago", True)],
)
def test_hay_dimensiones_detecta_cualquiera(campo, esperado):
    assert TODOS._replace(**{campo: ("x",)}).hay_dimensiones is esperado


def test_sin_nada_puesto_no_hay_dimensiones():
    assert TODOS.hay_dimensiones is False
