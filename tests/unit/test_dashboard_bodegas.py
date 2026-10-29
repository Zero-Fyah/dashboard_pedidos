"""Selector de bodega en el dashboard (DEC-165): lo que sostiene que la caché
nunca cruce bodegas.

Pruebas estáticas sobre el código fuente (AST), sin importar Streamlit ni abrir
bases: (1) toda función de las capas de datos acepta `bodega`, y como es
argumento entra en la clave de `st.cache_data`; (2) toda llamada a esas capas
desde `app.py` y las páginas la pasa explícita. Una llamada sin `bodega`
mostraría en silencio la bodega del proceso con la otra seleccionada. Lo que
abre SQLite está en `tests/integration/test_dashboard_bodegas.py`.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"
# Lo que toca una base o una carpeta de la bodega en cada capa.
_DE_LA_BODEGA = {
    "db.py": None,  # todas las públicas
    "tareas_db.py": None,
    "conteos_io.py": {"carpeta_conteos", "existe", "guardar", "anular", "listar_activas"},
}
_AUXILIARES_DB = {
    "_conn",
    "_num_cols_exist",
    "_objeto_existe",
    "_tabla_existe",
    "_consulta_auxiliar",
}


def _funciones(archivo: str) -> list[ast.FunctionDef]:
    arbol = ast.parse((DASHBOARD / archivo).read_text(encoding="utf-8"))
    return [n for n in arbol.body if isinstance(n, ast.FunctionDef)]


def _argumentos(fn: ast.FunctionDef) -> set[str]:
    a = fn.args
    return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}


@pytest.mark.parametrize("archivo", sorted(_DE_LA_BODEGA))
def test_toda_funcion_de_la_capa_de_datos_recibe_la_bodega(archivo):
    nombres = _DE_LA_BODEGA[archivo]
    revisadas = [
        f
        for f in _funciones(archivo)
        if (nombres is None and not f.name.startswith("_")) or (nombres and f.name in nombres)
    ]
    assert revisadas
    sin_bodega = [f.name for f in revisadas if "bodega" not in _argumentos(f)]
    assert sin_bodega == []


def test_toda_funcion_cacheada_y_los_auxiliares_de_db_reciben_la_bodega():
    """La clave de `st.cache_data` son los argumentos: sin `bodega` entre
    ellos, la primera bodega consultada quedaría servida a la otra."""
    for f in _funciones("db.py"):
        cacheada = any("cache_data" in ast.unparse(d) for d in f.decorator_list)
        if cacheada or f.name in _AUXILIARES_DB:
            assert "bodega" in _argumentos(f), f.name


def _llamadas_sin_bodega(archivo: Path) -> list[str]:
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    importados: set[str] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom) and n.module in ("db", "tareas_db"):
            importados |= {a.asname or a.name for a in n.names}
    faltan = []
    for c in ast.walk(arbol):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        de_la_capa = (isinstance(f, ast.Name) and f.id in importados) or (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id in ("db", "tareas_db", "conteos_io")
        )
        if de_la_capa and not any(k.arg == "bodega" for k in c.keywords):
            faltan.append(f"{archivo.name}:{c.lineno} {ast.unparse(f)}")
    return faltan


def test_app_y_las_paginas_pasan_la_bodega_en_cada_llamada():
    archivos = [DASHBOARD / "app.py", *sorted((DASHBOARD / "pages").glob("*.py"))]
    faltan = [x for a in archivos for x in _llamadas_sin_bodega(a)]
    assert faltan == []


def test_cada_pagina_que_consulta_lee_la_bodega_del_selector():
    for pagina in sorted((DASHBOARD / "pages").glob("*.py")):
        texto = pagina.read_text(encoding="utf-8")
        if "from db import" in texto or "import conteos_io" in texto:
            assert "bodega = bodega_seleccionada()" in texto, pagina.name


def test_el_detector_ve_una_llamada_sin_bodega(tmp_path):
    """Control del propio detector: si no viera nada, la prueba de arriba
    pasaría siempre."""
    pagina = tmp_path / "pagina.py"
    pagina.write_text(
        "import conteos_io\nfrom db import get_ira\n"
        "get_ira(bodega=bodega)\nget_ira()\nconteos_io.listar_activas()\n"
    )
    assert _llamadas_sin_bodega(pagina) == [
        "pagina.py:4 get_ira",
        "pagina.py:5 conteos_io.listar_activas",
    ]
