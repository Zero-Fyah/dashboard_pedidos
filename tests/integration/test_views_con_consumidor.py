"""Toda VIEW del ETL tiene que tener un consumidor vivo en el dashboard (DEC-094).

DEC-065 declaró tres VIEWs "conectadas como cumplimiento de despacho" y la
auditoría de DEC-094 encontró que **solo una lo estaba**: `v_detalle_diferencias_num`
aparecía únicamente en un docstring y `v_gestion_diferencias_num` en ningún
archivo. El grupo se cerró sin verificar consumidor por consumidor, y nada en la
suite podía notarlo.

Este test cierra esa vía. Tres trampas concretas que evita, y las tres las
encontró un cambio real, no un ejercicio:

1. **Un docstring no es un consumidor.** Se parsea con `ast` y se descartan los
   docstrings de módulo, clase y función. Las cadenas SQL normales sí cuentan
   —el SQL vive en literales de texto—, así que no basta con ignorar todas las
   cadenas: hay que distinguirlas.
2. **Una función huérfana tampoco es un consumidor.** `v_lineas_pedido_num` solo
   se usa desde `get_detalle_pedido()`, que ninguna página llama. Se calcula qué
   funciones de `dashboard/db.py` son alcanzables desde las páginas y solo se
   miran las cadenas que viven dentro de ellas.
3. **Nombrar una VIEW no es usarla.** DEC-098 agregó a la página de Pedidos un
   aviso que explica por qué **no** usa `v_inventario_comprometido`, y el
   detector contó esa prosa como consumo. Ahora exige un `FROM`/`JOIN` delante,
   o que la cadena sea exactamente el nombre (la forma de `_objeto_existe`).

El allowlist `SIN_CONSUMIDOR` es un **trinquete**: no puede crecer sin que
alguien lo escriba a mano, y tampoco puede quedar desactualizado, porque el test
también falla si una VIEW listada ahí resulta tener consumidor. Conectar una
VIEW obliga a sacarla de la lista en el mismo commit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import aiosqlite
import pytest

from etl.etl_principal import crear_views, normalizar_montos

_RAIZ = Path(__file__).resolve().parents[2]
_DASHBOARD = _RAIZ / "dashboard"
_DB_PY = _DASHBOARD / "db.py"

# VIEWs que hoy no tienen consumidor. Cada entrada necesita motivo y destino:
# o se conecta, o se retira en `_VIEWS_RETIRADAS`. No hay tercera opción — una
# VIEW sin consumidor y sin decisión escrita es la deuda que DEC-094 encontró.
SIN_CONSUMIDOR: dict[str, str] = {
    # ── VACÍO, y así debe quedarse ────────────────────────────────────────────
    # DEC-094 encontró 6 VIEWs sin consumidor. Las rebanadas de DEC-097 a
    # DEC-099 conectaron 5 y DEC-103 retiró la sexta
    # (`v_inventario_comprometido`) por semántica inválida — no le faltaba un
    # consumidor, no podía dar bien lo que prometía (DEC-098).
    #
    # Agregar una entrada acá **no es gratis**: significa que el ETL reconstruye
    # algo cada hora que nadie lee. Requiere motivo y destino escritos, como los
    # tenía el Grupo C de DEC-065.
}


def _nodos_docstring(arbol: ast.AST) -> set[int]:
    """IDs de los nodos de cadena que son docstrings, para excluirlos.

    Un docstring es el primer statement de un módulo, clase o función. Es la
    única cadena que menciona una VIEW sin consumirla — y es justo la que hizo
    pasar `v_detalle_diferencias_num` por conectada.
    """
    ids: set[int] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cuerpo = getattr(nodo, "body", None)
        if not cuerpo:
            continue
        primero = cuerpo[0]
        if isinstance(primero, ast.Expr) and isinstance(primero.value, ast.Constant):
            if isinstance(primero.value.value, str):
                ids.add(id(primero.value))
    return ids


def _cadenas_de_codigo(arbol: ast.AST, raiz: ast.AST | None = None) -> list[str]:
    """Cadenas de texto del árbol, sin docstrings ni comentarios.

    `ast` ya descarta los comentarios; acá se descartan los docstrings. Todo lo
    demás cuenta, porque el SQL del dashboard vive en literales de texto.
    """
    excluir = _nodos_docstring(raiz if raiz is not None else arbol)
    return [
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in excluir
    ]


def _funcs_de_db_py(arbol: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Funciones de nivel superior de `dashboard/db.py`, por nombre."""
    return {n.name: n for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _nombres_usados(arbol: ast.AST) -> set[str]:
    """Identificadores referenciados en el árbol (`Name` y `Attribute`)."""
    usados: set[str] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Name):
            usados.add(n.id)
        elif isinstance(n, ast.Attribute):
            usados.add(n.attr)
    return usados


def _funciones_vivas(arbol_db: ast.Module, otros: list[ast.Module]) -> set[str]:
    """Funciones de `db.py` alcanzables desde las páginas, por cierre transitivo.

    Semilla: lo que las páginas y `app.py` nombran. Después se propaga hacia
    adentro — una función viva mantiene vivas a las que llama.
    """
    funcs = _funcs_de_db_py(arbol_db)
    vivas = {nombre for arbol in otros for nombre in _nombres_usados(arbol) if nombre in funcs}

    frontera = set(vivas)
    while frontera:
        nueva: set[str] = set()
        for nombre in frontera:
            for llamada in _nombres_usados(funcs[nombre]):
                if llamada in funcs and llamada not in vivas:
                    nueva.add(llamada)
        vivas |= nueva
        frontera = nueva
    return vivas


def _consumidores_del_dashboard() -> list[str]:
    """Todas las cadenas de código alcanzables desde las páginas del dashboard.

    De `db.py` se toman solo las funciones vivas; del resto de `dashboard/`,
    todo, porque una página entera es por definición alcanzable.
    """
    arbol_db = ast.parse(_DB_PY.read_text(encoding="utf-8"))
    otros_archivos = [p for p in _DASHBOARD.rglob("*.py") if p != _DB_PY]
    otros_arboles = [ast.parse(p.read_text(encoding="utf-8")) for p in otros_archivos]

    cadenas: list[str] = []
    for arbol in otros_arboles:
        cadenas.extend(_cadenas_de_codigo(arbol))

    vivas = _funciones_vivas(arbol_db, otros_arboles)
    funcs = _funcs_de_db_py(arbol_db)
    for nombre in vivas:
        cadenas.extend(_cadenas_de_codigo(funcs[nombre], raiz=funcs[nombre]))
    return cadenas


def _consume(vista: str, cadenas: list[str]) -> bool:
    """True si alguna cadena **usa** la VIEW, no solo la nombra.

    Tercera trampa, encontrada por un cambio real en DEC-098: la página de
    Pedidos explica en un `st.warning()` por qué **no** usa
    `v_inventario_comprometido`, y el detector contaba esa prosa como consumo.
    Nombrar una VIEW para decir que no se usa es lo contrario de usarla.

    Se aceptan dos formas, y ninguna más:

    - la VIEW detrás de un `FROM` o un `JOIN` — una consulta de verdad;
    - la cadena que es **exactamente** el nombre, que es como se pasa a
      `_objeto_existe("v_x", "view")` para comprobar que existe.
    """
    patron = re.compile(rf"\b(?:FROM|JOIN)\s+{re.escape(vista)}\b", re.IGNORECASE)
    return any(c.strip() == vista or patron.search(c) for c in cadenas)


async def _views_del_etl(db_path: str) -> set[str]:
    """Nombres reales de las VIEWs que crea el ETL.

    Se leen de `sqlite_master` tras correr `crear_views()` y no del código: el
    dict vive dentro de la función y parsearlo sería otra fuente de verdad que
    se desincroniza (el error que DEC-065 documenta sobre los estados).
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        await crear_views(db)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='view'")
        return {r[0] for r in await cursor.fetchall()}


@pytest.mark.integration
async def test_toda_view_del_etl_tiene_consumidor(db_path):
    """Ninguna VIEW nueva puede quedar sin consumidor y sin decisión escrita."""
    views = await _views_del_etl(db_path)
    cadenas = _consumidores_del_dashboard()

    huerfanas = {v for v in views if not _consume(v, cadenas)}
    inesperadas = huerfanas - set(SIN_CONSUMIDOR)

    assert not inesperadas, (
        f"VIEWs del ETL sin consumidor en el dashboard: {sorted(inesperadas)}.\n"
        "El ETL las reconstruye en cada corrida y nadie las lee. Conectala a una "
        "página, retirala en `_VIEWS_RETIRADAS`, o agregala a SIN_CONSUMIDOR con "
        "el motivo y la decisión que lo respalda (DEC-094)."
    )


@pytest.mark.integration
async def test_allowlist_no_tiene_views_ya_conectadas(db_path):
    """El trinquete se aprieta solo: conectar una VIEW obliga a sacarla de la lista.

    Sin esto el allowlist se vuelve un cementerio — entradas que alguien resolvió
    y nadie borró, que después tapan una regresión real.
    """
    views = await _views_del_etl(db_path)
    cadenas = _consumidores_del_dashboard()

    ya_conectadas = {v for v in SIN_CONSUMIDOR if v in views and _consume(v, cadenas)}

    assert not ya_conectadas, (
        f"Estas VIEWs ya tienen consumidor y siguen en SIN_CONSUMIDOR: "
        f"{sorted(ya_conectadas)}. Sacalas de la lista en el mismo commit que "
        "las conectó."
    )


@pytest.mark.integration
async def test_allowlist_no_lista_views_inexistentes(db_path):
    """Una entrada que ya no corresponde a ninguna VIEW es ruido que confunde."""
    views = await _views_del_etl(db_path)
    fantasmas = set(SIN_CONSUMIDOR) - views

    assert not fantasmas, (
        f"SIN_CONSUMIDOR menciona VIEWs que el ETL ya no crea: {sorted(fantasmas)}. "
        "Si se retiraron, sacá también su entrada de la lista."
    )


@pytest.mark.integration
def test_un_docstring_no_cuenta_como_consumidor():
    """El detector distingue docstring de SQL — la trampa que se cobró a DEC-065.

    Sin esta distinción, `v_detalle_diferencias_num` habría pasado por conectada
    por figurar en el docstring de `pages/operacion.py`.
    """
    fuente = '''
"""Este módulo habla de v_solo_mencionada en su docstring."""

def consulta():
    """Y esta función también menciona v_solo_mencionada."""
    return "SELECT * FROM v_realmente_usada"
'''
    cadenas = _cadenas_de_codigo(ast.parse(fuente))
    unidas = " ".join(cadenas)

    assert "v_realmente_usada" in unidas, "el SQL en un literal normal sí debe contar"
    assert "v_solo_mencionada" not in unidas, "un docstring no puede contar como consumidor"


@pytest.mark.integration
def test_una_funcion_huerfana_no_cuenta_como_consumidor():
    """La segunda trampa: consumir desde código que ninguna página alcanza."""
    db_py = ast.parse(
        'def viva():\n    return "SELECT 1 FROM v_viva"\n'
        'def huerfana():\n    return "SELECT 1 FROM v_huerfana"\n'
    )
    pagina = ast.parse("from db import viva\nviva()\n")

    vivas = _funciones_vivas(db_py, [pagina])

    assert vivas == {"viva"}, f"alcanzabilidad mal calculada: {vivas}"


@pytest.mark.integration
def test_alcanzabilidad_es_transitiva():
    """Una función viva mantiene viva a la que llama — si no, habría falsos huérfanos."""
    db_py = ast.parse(
        'def _ayudante():\n    return "SELECT 1 FROM v_indirecta"\n'
        "def viva():\n    return _ayudante()\n"
        'def huerfana():\n    return "SELECT 1 FROM v_huerfana"\n'
    )
    pagina = ast.parse("from db import viva\nviva()\n")

    vivas = _funciones_vivas(db_py, [pagina])

    assert vivas == {"viva", "_ayudante"}, f"el cierre transitivo falló: {vivas}"


@pytest.mark.integration
def test_nombrar_una_view_en_prosa_no_cuenta_como_consumo():
    """La tercera trampa, encontrada por DEC-098 y no por un ejercicio.

    La página de Pedidos explica en pantalla por qué **no** usa
    `v_inventario_comprometido`. Esa frase es una cadena de código —no un
    docstring— y el detector la contaba como consumo, que es lo contrario de lo
    que dice.
    """
    prosa = "Esta cifra no sale de v_inventario_comprometido, y es a propósito."
    sql = "SELECT * FROM v_inventario_comprometido WHERE x = 1"

    assert not _consume("v_inventario_comprometido", [prosa])
    assert _consume("v_inventario_comprometido", [sql])


@pytest.mark.integration
def test_el_guard_de_existencia_cuenta_como_consumo():
    """`_objeto_existe("v_x", "view")` pasa el nombre pelado y sí es uso real."""
    assert _consume("v_descuentos_lineas", ["v_descuentos_lineas"])
    assert _consume("v_descuentos_lineas", ["  v_descuentos_lineas  "])


@pytest.mark.integration
def test_join_tambien_cuenta_no_solo_from():
    assert _consume("v_inventario_salud", ["LEFT JOIN v_inventario_salud s ON s.x = y"])


@pytest.mark.integration
def test_un_prefijo_no_dispara_por_error():
    """`v_conteos` no puede darse por consumida porque exista `v_conteos_ira`."""
    assert not _consume("v_conteos", ["SELECT * FROM v_conteos_ira"])
