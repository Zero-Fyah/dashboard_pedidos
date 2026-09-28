"""DEC-157: el dashboard puede leer tablas del scraper, pero nunca un monto
en texto — siempre la columna `*_num` que puebla el ETL, o una VIEW del ETL.

Es la parte de DEC-022 que sí protege algo: `SUM()` sobre `'COP 12.345'` da
0 en SQLite sin error. El test lee el SQL de `dashboard/db.py` y el mapeo
texto → `_num` directamente del ETL (`columnas_por_tabla`), así la lista de
columnas prohibidas tiene un solo origen de verdad.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RAIZ = Path(__file__).resolve().parents[2]
_PALABRAS_SQL = {
    "on", "where", "join", "left", "inner", "group", "order", "limit", "using",
    "as", "and", "or", "union", "having", "cross", "natural",
}  # fmt: skip


def _mapeo_texto_a_num() -> dict[str, set[str]]:
    """{tabla: columnas de texto que tienen su `_num`} leído del ETL."""
    arbol = ast.parse((RAIZ / "etl" / "etl_principal.py").read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.AnnAssign | ast.Assign) and "columnas_por_tabla" in ast.unparse(
            nodo.targets[0] if isinstance(nodo, ast.Assign) else nodo.target
        ):
            valor = ast.literal_eval(nodo.value)
            return {tabla: set(cols) for tabla, cols in valor.items()}
    raise AssertionError("no se encontró `columnas_por_tabla` en etl/etl_principal.py")


def _sql_del_dashboard() -> list[str]:
    """Todo literal (incluidas las partes fijas de los f-strings) con SQL."""
    arbol = ast.parse((RAIZ / "dashboard" / "db.py").read_text(encoding="utf-8"))
    textos = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.JoinedStr):
            textos.append("".join(v.value for v in nodo.values if isinstance(v, ast.Constant)))
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            textos.append(nodo.value)
    return [
        t for t in textos if re.search(r"\bSELECT\b", t, re.I) and re.search(r"\bFROM\b", t, re.I)
    ]


def violaciones(sql: str, mapeo: dict[str, set[str]]) -> list[str]:
    """Referencias a un monto en texto de una tabla cruda dentro de `sql`."""
    sin_comentarios = re.sub(r"--[^\n]*", "", sql)
    alias: dict[str, str] = {}
    fuentes = []
    for tabla, al in re.findall(
        r"\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", sin_comentarios, re.I
    ):
        fuentes.append(tabla)
        alias[tabla] = tabla
        if al and al.lower() not in _PALABRAS_SQL:
            alias[al] = tabla
    malas = [
        f"{al}.{col}"
        for al, col in re.findall(r"\b(\w+)\.(\w+)\b", sin_comentarios)
        if col in mapeo.get(alias.get(al, ""), set())
    ]
    # Sin alias: solo se puede atribuir si la consulta lee UNA sola fuente.
    if len(set(fuentes)) == 1 and fuentes[0] in mapeo:
        for col in mapeo[fuentes[0]]:
            if re.search(rf"(?<![.\w])(?<!AS ){col}\b(?!_num)", sin_comentarios, re.I):
                malas.append(col)
    return malas


def test_el_mapeo_del_etl_se_lee():
    mapeo = _mapeo_texto_a_num()
    assert "monto_diferencia" in mapeo["gestion_diferencias"]
    assert "monto_final" in mapeo["lineas_pedido"]


def test_el_dashboard_no_lee_montos_en_texto_de_tablas_crudas():
    mapeo = _mapeo_texto_a_num()
    encontradas = {
        m: sql.strip().splitlines()[0][:60]
        for sql in _sql_del_dashboard()
        for m in violaciones(sql, mapeo)
    }
    assert not encontradas, (
        f"Leer `*_num` o una VIEW del ETL, nunca la columna de texto (DEC-157): {encontradas}"
    )


# ── El detector detecta: casos sintéticos ─────────────────────────────────

_MAPEO = {"gestion_diferencias": {"monto_diferencia"}, "lineas_pedido": {"monto_final"}}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT gd.monto_diferencia FROM pedidos p JOIN gestion_diferencias gd ON gd.id_pedido = p.id_pedido",
        "SELECT SUM(l.monto_final) FROM lineas_pedido l",
        "SELECT SUM(monto_final) FROM lineas_pedido",
        "SELECT lineas_pedido.monto_final FROM lineas_pedido",
    ],
)
def test_detecta_monto_en_texto(sql):
    assert violaciones(sql, _MAPEO)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT gd.monto_diferencia_num FROM gestion_diferencias gd",
        "SELECT SUM(monto_final_num) AS monto_final FROM lineas_pedido",
        # En la VIEW del ETL `monto_final` ya es el número renombrado.
        "SELECT SUM(l.monto_final) FROM v_lineas_pedido_num l JOIN subpedidos s ON s.id_pedido = l.id_pedido",
        "SELECT l.referencia FROM lineas_pedido l -- l.monto_final en un comentario",
    ],
)
def test_no_marca_lo_normalizado(sql):
    assert not violaciones(sql, _MAPEO)
