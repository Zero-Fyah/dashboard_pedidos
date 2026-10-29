"""Selector de bodega en el dashboard (DEC-165), con bases y carpetas reales.

Raíz temporal con las dos bodegas (`comun._RAIZ_PROYECTO` reanclado): la
histórica sin marca y con datos, como la de producción, y `bodega2` marcada.
Verifica que la caché no cruza bodegas, que una base ajena detiene también las
comprobaciones auxiliares, que «no disponible» se decide por la fuente, que
Tareas y las hojas de conteo quedan en la carpeta de la bodega, y el selector
renderizado con AppTest.
"""

import sqlite3
from pathlib import Path

import pytest
import streamlit as st

import comun
import dashboard.conteos_io as cio
import dashboard.db as ddb
import dashboard.tareas_db as tdb
from comun import MARCA_BODEGA, BodegaIncorrectaError

pytestmark = pytest.mark.integration

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"


def _base(ruta: Path, marca: int, actualizado: str) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    if marca:
        con.execute(f"PRAGMA application_id = {marca}")
    con.execute("CREATE TABLE pedidos (id_pedido TEXT, actualizado_en TEXT)")
    con.execute("INSERT INTO pedidos VALUES ('TEST-1', ?)", (actualizado,))
    con.execute("CREATE TABLE inventario_ubicaciones (ubicacion TEXT)")
    con.commit()
    con.close()
    return ruta


@pytest.fixture
def raiz(tmp_path, monkeypatch):
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", tmp_path)
    monkeypatch.delenv("BODEGA", raising=False)
    for modulo, atributos in (
        (ddb, ("DB_PATH",)),
        (tdb, ("DB_PATH",)),
        (cio, ("CARPETA_CONTEOS", "CARPETA_ANULADOS", "CARPETA_RESPALDO")),
    ):
        for a in atributos:
            monkeypatch.setattr(modulo, a, None)
    monkeypatch.setattr(cio, "_RAIZ_RESPALDO", tmp_path / "respaldo")
    _base(tmp_path / "data" / "pedidos.db", 0, "2026-10-01 10:00:00")
    _base(
        tmp_path / "data" / "bodega2" / "pedidos.db", MARCA_BODEGA["bodega2"], "2026-10-28 09:00:00"
    )
    st.cache_data.clear()
    yield tmp_path
    st.cache_data.clear()


def test_la_cache_no_cruza_bodegas(raiz):
    """La misma función cacheada, las dos bodegas, en los dos órdenes."""
    for _ in range(2):
        assert ddb.get_ultima_actualizacion(bodega="miempresa") == "2026-10-01 10:00:00"
        assert ddb.get_ultima_actualizacion(bodega="bodega2") == "2026-10-28 09:00:00"


def test_sin_bodega_vale_la_del_proceso(raiz, monkeypatch):
    assert ddb.get_ultima_actualizacion() == "2026-10-01 10:00:00"
    monkeypatch.setenv("BODEGA", "bodega2")
    st.cache_data.clear()
    assert ddb.get_ultima_actualizacion() == "2026-10-28 09:00:00"


def test_una_base_ajena_detiene_tambien_las_comprobaciones_auxiliares(raiz):
    """Cierra el pendiente de la adenda 2 de DEC-163: las conexiones de
    existencia no verificaban la marca y leían cualquier base como «no hay»."""
    ajena = raiz / "data" / "bodega2" / "pedidos.db"
    ajena.unlink()
    _base(ajena, MARCA_BODEGA["miempresa"], "2026-10-28 09:00:00")
    with pytest.raises(BodegaIncorrectaError):
        ddb._objeto_existe("pedidos", bodega="bodega2")
    with pytest.raises(BodegaIncorrectaError):
        ddb.fuente_disponible("pedidos", bodega="bodega2")
    with pytest.raises(BodegaIncorrectaError):
        ddb.get_ultima_actualizacion(bodega="bodega2")


def test_la_fuente_esta_disponible_solo_con_filas(raiz):
    assert ddb.fuente_disponible("pedidos", bodega="bodega2")
    assert not ddb.fuente_disponible("inventario_ubicaciones", bodega="bodega2")  # vacía
    assert not ddb.fuente_disponible("no_existe", bodega="bodega2")
    assert not ddb.fuente_disponible("pedidos; DROP TABLE pedidos", bodega="bodega2")
    assert ddb.fuente_disponible("pedidos", bodega="bodega2")  # sigue ahí


def test_las_tareas_viven_en_la_base_de_cada_bodega(raiz):
    tdb.listar_tareas(bodega="bodega2")
    tdb.listar_tareas(bodega="miempresa")
    for ruta, bodega in (
        (raiz / "data" / "bodega2" / "tareas.db", "bodega2"),
        (raiz / "data" / "tareas.db", "miempresa"),
    ):
        con = sqlite3.connect(ruta)
        assert con.execute("PRAGMA application_id").fetchone()[0] == MARCA_BODEGA[bodega]
        con.close()
    assert tdb.ruta_base(bodega="bodega2") == raiz / "data" / "bodega2" / "tareas.db"


def test_la_hoja_de_conteo_queda_en_la_carpeta_de_la_bodega(raiz):
    destino = cio.guardar("hoja.xlsx", b"conteo", bodega="bodega2")
    assert destino == raiz / "data" / "bodega2" / "conteos" / "hoja.xlsx"
    assert cio.listar_activas(bodega="bodega2") == ["hoja.xlsx"]
    assert cio.listar_activas(bodega="miempresa") == []
    assert (raiz / "respaldo" / "bodega2" / "conteos" / "hoja.xlsx").exists()


# ─────────────────────────────────────────────
# El selector, renderizado
# ─────────────────────────────────────────────


def _app():
    import filtros
    import streamlit as st

    st.markdown(f"elegida:{filtros.selector_bodega()}")


def _app_filtros():
    import filtros
    import streamlit as st

    b = filtros.selector_bodega()
    filtros.franja_historico(b)
    rangos = {"miempresa": ("2026-01-01", "2026-10-28"), "bodega2": ("2026-10-18", "2026-10-28")}
    f = filtros.barra_lateral(*rangos[b])
    st.markdown(f"filtro:{b}:{f.desde}:{f.hasta}")


@pytest.fixture
def desde_funcion(raiz, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.syspath_prepend(str(DASHBOARD))  # como corre Streamlit
    (raiz / ".env.bodega2").write_text("BODEGA_ETIQUETA=Operación nueva\n")
    return AppTest.from_function


@pytest.fixture
def app(desde_funcion):
    return desde_funcion(_app)


def _elegida(at) -> str:
    return next(m.value for m in at.markdown if m.value.startswith("elegida:")).split(":")[1]


def test_abre_en_bodega2_y_recuerda_la_eleccion(app):
    at = app.run()
    assert not at.exception
    assert _elegida(at) == "bodega2"
    radio = at.radio[0]
    assert radio.options == ["Miempresa", "Operación nueva"]  # etiquetas, no identificadores

    radio.set_value("miempresa").run()
    assert _elegida(at) == "miempresa"
    assert at.session_state["flt_bodega"] == "miempresa"


def test_sin_la_base_de_bodega2_no_hay_selector(app, raiz):
    (raiz / "data" / "bodega2" / "pedidos.db").unlink()
    at = app.run()
    assert not at.exception
    assert _elegida(at) == "miempresa"
    assert len(at.radio) == 0


def _filtro(at) -> tuple[str, str, str]:
    texto = next(m.value for m in at.markdown if m.value.startswith("filtro:"))
    return tuple(texto.split(":")[1:])


def test_cada_bodega_recuerda_su_propio_rango(desde_funcion):
    """El rango de una bodega no se arrastra a la otra (verificado en navegador
    el 2026-10-28: la histórica quedaba recortada a los días de la nueva)."""
    at = desde_funcion(_app_filtros).run()
    assert _filtro(at) == ("bodega2", "2026-10-18", "2026-10-28")
    at.select_slider[0].set_value(("2026-10-20", "2026-10-25")).run()
    assert _filtro(at) == ("bodega2", "2026-10-20", "2026-10-25")

    at.radio[0].set_value("miempresa").run()
    assert _filtro(at) == ("miempresa", "2026-01-01", "2026-10-28")

    at.radio[0].set_value("bodega2").run()
    assert _filtro(at) == ("bodega2", "2026-10-20", "2026-10-25")
    assert not at.exception


def test_la_franja_de_historico_va_en_hora_colombia_y_solo_con_dos_bodegas(desde_funcion, raiz):
    at = desde_funcion(_app_filtros).run()
    assert not at.warning  # en bodega2 no hay franja
    at.radio[0].set_value("miempresa").run()
    franja = at.warning[0].value
    assert "Histórico — Miempresa" in franja
    assert "2026-10-01 05:00" in franja  # 10:00 UTC → 05:00 Colombia

    (raiz / "data" / "bodega2" / "pedidos.db").unlink()  # una sola bodega
    st.cache_data.clear()
    at = desde_funcion(_app_filtros).run()
    assert not at.warning


def test_el_catalogo_no_arena_usa_el_almacen_de_la_bodega_consultada(raiz, monkeypatch):
    """El dashboard elige la bodega por sesión: el almacén no puede salir del
    proceso. Con `Bogotá` fijo, en bodega2 (`Bogota`) el último ingreso de
    contenedor quedaba vacío para todos los productos, sin error."""
    import inventario.catalogo_no_arena as cna

    vistos: list[str] = []
    monkeypatch.setattr(
        cna, "construir_catalogo_no_arena", lambda con, *, almacen: vistos.append(almacen)
    )
    monkeypatch.setattr(ddb, "construir_catalogo_no_arena", cna.construir_catalogo_no_arena)
    ddb.get_catalogo_no_arena(bodega="bodega2")
    ddb.get_catalogo_no_arena(bodega="miempresa")
    assert vistos == ["Bogota", "Bogotá"]
