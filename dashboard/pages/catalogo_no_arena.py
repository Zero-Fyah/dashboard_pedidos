"""
Catálogo no-Arena — venta, clasificación e ingreso por ID (DEC-132).

Requerimiento del Arquitecto (2026-09-04): una fila por ID de producto
(`id_especificacion`, DEC-045/DEC-111) que **no** sea Arena, con fecha de
última venta, clasificación ABC-XYZ, fecha del último ingreso al sistema
por contenedor (tipo de cambio "Entrada"), inventario actual y si está
activo para la venta.

Lee `inventario.catalogo_no_arena.construir_catalogo_no_arena()` vía
`db.get_catalogo_no_arena()` — no agrega ninguna fuente ni recalcula nada
que el scheduler ya persista.

**Alcance heredado, no una limitación de esta página:** CEDI Bogotá y el
catálogo que el sistema administrativo reconoce (`en_catalogo=1`,
DEC-072) — mismo alcance que el resto del dominio maduro de inventario
(ABC-XYZ, salud, layout).
"""

from __future__ import annotations

import sqlite3

import plotly.graph_objects as go
import streamlit as st

from comun import SUFIJO_CLASE_HEREDADA
from db import get_catalogo_no_arena
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🏷️ Catálogo no-Arena")

try:
    df = get_catalogo_no_arena()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay catálogo no-Arena calculado. Se genera en cada ciclo del "
        "scheduler, o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

st.caption(
    f"{len(df):,} ID de producto no-Arena, catálogo del admin, CEDI Bogotá "
    "(mismo alcance que el resto del módulo de inventario — DEC-132).".replace(",", ".")
)

# ── Clase base sin el sufijo de herencia, para agrupar y colorear ──────────
# "A (por referencia)" y "A" cuentan como la misma letra a nivel resumen; el
# sufijo se conserva en la tabla de detalle (columna `abc`/`xyz` tal cual)
# para no esconder de dónde salió la clase.
df["_abc_base"] = df["abc"].str.replace(SUFIJO_CLASE_HEREDADA, "", regex=False)
df["_xyz_base"] = df["xyz"].str.replace(SUFIJO_CLASE_HEREDADA, "", regex=False)

sin_clase = df["_abc_base"].isna() | ~df["_abc_base"].isin(["A", "B", "C"])
sin_venta = df["ultima_venta"].isna()
sin_ingreso = df["ultimo_ingreso_contenedor"].isna()
activos = df["activo_venta"]

# ── KPIs ─────────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.metric("ID en el catálogo", f"{len(df):,}")
k2.metric(
    "Activos para venta",
    f"{int(activos.sum()):,}",
    help=(
        "Vigencia por ID cuando el ID no tiene ubicación conocida; para el resto "
        "(la mayoría) se aproxima con la vigencia de su referencia — ver columna "
        "«Fuente de vigencia» en el detalle."
    ),
)
k3.metric(
    "Sin ABC/XYZ",
    f"{int(sin_clase.sum()):,}",
    help="Ni el ID ni su referencia tuvieron venta atribuible en los últimos 6 meses.",
)
k4.metric(
    "Sin venta registrada",
    f"{int(sin_venta.sum()):,}",
    help="Nunca aparece en una línea de pedido no cancelada con este ID atribuido.",
)

st.divider()

# ── Composición ABC ─────────────────────────────────────────────────────
st.subheader("Cómo se reparte la clasificación ABC")

ORDEN_ABC = ["A", "B", "C", "Sin clasificar"]
COLOR_ABC = {
    "A": GRAFICO_SERIES[0],
    "B": GRAFICO_SERIES[1],
    "C": GRAFICO_SERIES[2],
    "Sin clasificar": "rgba(255,255,255,0.18)",
}
_abc_para_conteo = df["_abc_base"].where(df["_abc_base"].isin(["A", "B", "C"]), "Sin clasificar")
conteo_abc = _abc_para_conteo.value_counts().reindex(ORDEN_ABC).fillna(0).astype(int)

c1, c2 = st.columns([1, 1.4])
with c1:
    for clase in ORDEN_ABC:
        st.markdown(f"**{conteo_abc[clase]:,}** · Clase {clase}")
    heredadas = int((df["abc"].fillna("").str.endswith(SUFIJO_CLASE_HEREDADA)).sum())
    st.caption(
        f"{heredadas:,} ID heredan la clase de su referencia porque no tienen venta "
        "propia atribuible en los últimos 6 meses (DEC-132) — cuentan en la letra "
        "base de arriba."
    )
with c2:
    fig = go.Figure()
    fig.add_bar(
        x=ORDEN_ABC,
        y=[conteo_abc[c] for c in ORDEN_ABC],
        marker_color=[COLOR_ABC[c] for c in ORDEN_ABC],
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} ID<extra></extra>",
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=10, b=10),
        height=260,
        showlegend=False,
    )
    fig.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    fig.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    st.plotly_chart(fig, config={"displayModeBar": False})

st.divider()

# ── Filtros ──────────────────────────────────────────────────────────────
st.subheader("Detalle por ID")

f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
with f1:
    busqueda = st.text_input("Buscar por ID, código de barras, referencia o descripción", "")
with f2:
    abc_sel = st.multiselect("Clase ABC", ORDEN_ABC, placeholder="Todas...")
with f3:
    xyz_opciones = ["X", "Y", "Z", "Sin clasificar"]
    xyz_sel = st.multiselect("Clase XYZ", xyz_opciones, placeholder="Todas...")
with f4:
    activo_sel = st.selectbox("Activo para venta", ["Todos", "Solo activos", "Solo inactivos"])

vista = df.copy()
if busqueda:
    campos = ["id_especificacion", "id_producto", "codigo_barras", "referencia", "descripcion"]
    mascara = False
    for campo in campos:
        mascara = mascara | vista[campo].astype(str).str.contains(busqueda, case=False, na=False)
    vista = vista[mascara]

if abc_sel:
    _abc_v = vista["_abc_base"].where(vista["_abc_base"].isin(["A", "B", "C"]), "Sin clasificar")
    vista = vista[_abc_v.isin(abc_sel)]

if xyz_sel:
    _xyz_v = vista["_xyz_base"].where(vista["_xyz_base"].isin(["X", "Y", "Z"]), "Sin clasificar")
    vista = vista[_xyz_v.isin(xyz_sel)]

if activo_sel == "Solo activos":
    vista = vista[vista["activo_venta"]]
elif activo_sel == "Solo inactivos":
    vista = vista[~vista["activo_venta"]]

if vista.empty:
    st.info("Sin ID para los filtros seleccionados.")
else:
    st.caption(f"{len(vista):,} ID.".replace(",", "."))

    tabla = vista[
        [
            "id_especificacion",
            "id_producto",
            "codigo_barras",
            "referencia",
            "descripcion",
            "familia",
            "abc",
            "xyz",
            "inventario_actual",
            "activo_venta",
            "fuente_vigencia",
            "ultima_venta",
            "ultimo_ingreso_contenedor",
        ]
    ].copy()

    # Nulos explícitos como "—": nunca 0/vacío engañoso (id_producto y
    # codigo_barras tienen 11,3% de huecos reales por ambigüedad de
    # referencia+código, DEC-045/DEC-111 — no un error de esta página).
    for columna in [
        "id_producto",
        "codigo_barras",
        "descripcion",
        "familia",
        "abc",
        "xyz",
        "ultima_venta",
        "ultimo_ingreso_contenedor",
    ]:
        tabla[columna] = tabla[columna].fillna("—")

    st.dataframe(
        tabla,
        hide_index=True,
        width="stretch",
        height=460,
        column_config={
            "id_especificacion": st.column_config.TextColumn("ID especificación", width="medium"),
            "id_producto": st.column_config.TextColumn("ID producto", width="medium"),
            "codigo_barras": st.column_config.TextColumn("Código de barras", width="medium"),
            "referencia": st.column_config.TextColumn("Referencia", width="small"),
            "descripcion": st.column_config.TextColumn("Descripción", width="large"),
            "familia": st.column_config.TextColumn("Familia", width="small"),
            "abc": st.column_config.TextColumn("ABC", width="medium"),
            "xyz": st.column_config.TextColumn("XYZ", width="medium"),
            "inventario_actual": st.column_config.NumberColumn("Inventario actual", format="%.0f"),
            "activo_venta": st.column_config.CheckboxColumn(
                "Activo para venta",
                help="Vigencia según el sistema administrativo — ver «Fuente de vigencia».",
            ),
            "fuente_vigencia": st.column_config.TextColumn(
                "Fuente de vigencia",
                width="small",
                help=(
                    "«id»: vigencia calculada para este ID exacto. «referencia»: no hay "
                    "vigencia propia por ID (el ID tiene ubicación conocida) — se aproxima "
                    "con la de su referencia, basta una especificación vigente para que la "
                    "referencia lo sea (DEC-065)."
                ),
            ),
            "ultima_venta": st.column_config.TextColumn("Última venta", width="small"),
            "ultimo_ingreso_contenedor": st.column_config.TextColumn(
                "Último ingreso (contenedor)",
                width="medium",
                help='Último movimiento con tipo de cambio literal "Entrada".',
            ),
        },
    )
    st.download_button(
        "⬇️ Descargar (CSV)",
        vista.drop(columns=["_abc_base", "_xyz_base"]).to_csv(index=False).encode("utf-8-sig"),
        file_name="catalogo_no_arena.csv",
        mime="text/csv",
    )

st.caption(
    "Alcance: catálogo no-Arena reconocido por el sistema administrativo "
    "(`en_catalogo=1`, DEC-072), almacén Bogotá — heredado del resto del "
    "cruce de inventario, no una limitación propia de esta vista. "
    "«Fuente de vigencia» distingue precisión por ID (444 ID sin ubicación) "
    "de precisión por referencia (el resto, con ubicación conocida) — no "
    "mezclarlas en la lectura (DEC-132)."
)
