"""
Faltantes de inventario — de lo macro a lo micro (DEC-115/DEC-117).

DEC-115 encontró que "pedido con diferencia registrada" mezclaba tres cosas
distintas: líneas de relleno para completar el monto mínimo ("Interno
Acc/Are/Ali", sin faltante real detrás), faltantes reales de mercancía, y
diferencias puramente monetarias sin faltante físico. Esta página navega esa
clasificación de año → mes → semana → día → pedido padre → subpedido
afectado.

**El grupo "Diferencia monetaria" no siempre llega a subpedido.**
`gestion_diferencias`/`detalle_diferencias` no tienen columna de subpedido —
es una limitación del origen, no del scraper (confirmado en
`scraper/extractores.py`). Cuando el pedido tiene un solo subpedido, se
atribuye igual; con varios, no hay forma de saber cuál causó la diferencia
monetaria y la fila queda a nivel pedido, marcada como tal.

**Cancelados fuera por defecto.** Un pedido cancelado devuelve la mercancía
y no representa un faltante real para Inventarios (mismo criterio que cerró
DEC-115) — hay un checkbox para verlos aparte.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from filtros import aviso_alcance, barra_lateral

from db import get_faltantes_clasificados, get_rango_fechas
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("📉 Faltantes de inventario")

try:
    df = get_faltantes_clasificados()
    _min_fecha, _max_fecha = get_rango_fechas()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if not _min_fecha:
    st.info("Todavía no hay pedidos en la base de datos.")
    st.stop()

if df.empty:
    st.success("No hay pedidos con diferencia registrada.")
    st.stop()

# Sin filtro de vendedor/cliente/canal/forma de pago a propósito: esta vista
# no tiene esas columnas (no vienen de gestion_diferencias/lineas_pedido a
# ese nivel) y el análisis que la originó excluyó deliberadamente el nivel
# de vendedor (alcance Inventarios, no Comercial).
_f = barra_lateral(_min_fecha, _max_fecha)
aviso_alcance(_f, aplica_dimensiones=False)

incluir_cancelados = st.checkbox(
    "Incluir pedidos cancelados",
    value=False,
    help=(
        "Un pedido cancelado devuelve la mercancía: por defecto no cuenta como "
        "faltante real (DEC-115)."
    ),
)

vista = df[(df["fecha"] >= _f.desde) & (df["fecha"] <= _f.hasta)].copy()
if not incluir_cancelados:
    vista = vista[~vista["cancelado"]]

if vista.empty:
    st.info("Ningún pedido con diferencia en el período/alcance seleccionado.")
    st.stop()

ORDEN = ["Real", "Interno", "Monetario"]
COLOR = dict(zip(ORDEN, GRAFICO_SERIES, strict=True))
ETIQUETA = {
    "Real": "Faltante real de unidades",
    "Interno": "Relleno de monto mínimo",
    "Monetario": "Diferencia monetaria",
}

# ── Resumen ──────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
for columna, grupo in zip((c1, c2, c3), ORDEN, strict=True):
    columna.metric(ETIQUETA[grupo], int((vista["grupo"] == grupo).sum()))

_sin_atribuir = int((~vista["atribuible"]).sum())
st.caption(
    f"{vista['id_pedido'].nunique():,} pedidos distintos en el período. "
    f"{_sin_atribuir} fila(s) de 'Diferencia monetaria' quedan a nivel pedido — "
    "el origen no identifica el subpedido cuando hay varios.".replace(",", ".")
)

st.divider()

# ── Nivel temporal ───────────────────────────────────────────────────────
st.subheader("Cómo viene evolucionando")

granularidad = st.segmented_control("Agrupar por", ["Año", "Mes", "Semana", "Día"], default="Mes")
granularidad = granularidad or "Mes"

vista["_fecha_dt"] = pd.to_datetime(vista["fecha"], errors="coerce")
if granularidad == "Año":
    vista["_periodo"] = vista["_fecha_dt"].dt.strftime("%Y")
elif granularidad == "Mes":
    vista["_periodo"] = vista["_fecha_dt"].dt.strftime("%Y-%m")
elif granularidad == "Semana":
    # Semana ISO (%G-W%V), no strftime('%Y-W%W') — ese formato ya mostró un
    # artefacto real en esta misma investigación: una "2026-W00" de 3
    # pedidos sueltos por el corte de año.
    _iso = vista["_fecha_dt"].dt.isocalendar()
    vista["_periodo"] = _iso["year"].astype(str) + "-W" + _iso["week"].astype(str).str.zfill(2)
else:
    vista["_periodo"] = vista["fecha"]

serie = vista.groupby(["_periodo", "grupo"]).size().unstack(fill_value=0)
for grupo in ORDEN:
    if grupo not in serie.columns:
        serie[grupo] = 0
serie = serie[ORDEN].sort_index()

fig = go.Figure()
for grupo in ORDEN:
    fig.add_trace(
        go.Bar(x=serie.index, y=serie[grupo], name=ETIQUETA[grupo], marker_color=COLOR[grupo])
    )
fig.update_layout(
    barmode="stack",
    height=340,
    margin={"l": 10, "r": 10, "t": 20, "b": 10},
    paper_bgcolor=BG_DEEP,
    plot_bgcolor=BG_DEEP,
    font={"color": TEXT_PRIMARY},
    legend={"orientation": "h", "y": -0.25, "font": {"color": TEXT_SECONDARY}},
    xaxis={"gridcolor": GRAFICO_GRID, "automargin": True},
    yaxis={
        "gridcolor": GRAFICO_GRID,
        "automargin": True,
        "title": "Subpedidos/pedidos afectados",
    },
)
st.plotly_chart(fig, width="stretch")

if granularidad == "Año":
    st.caption("Toda la base es de 2026 por ahora — este nivel se llena solo con el tiempo.")

st.divider()

# ── Drill a pedido ───────────────────────────────────────────────────────
st.subheader("Pedidos afectados")

grupos_sel = st.multiselect("Grupo", ORDEN, default=ORDEN)
tabla_pedidos = vista[vista["grupo"].isin(grupos_sel)][
    [
        "id_pedido",
        "fecha",
        "grupo",
        "unidades_faltantes",
        "monto_diferencia",
        "atribuible",
        "cancelado",
    ]
].sort_values("fecha", ascending=False)

seleccion = st.dataframe(
    tabla_pedidos,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="tabla_faltantes",
    width="stretch",
    height=420,
    column_config={
        "id_pedido": st.column_config.TextColumn("Pedido", width="medium"),
        "fecha": st.column_config.TextColumn("Fecha", width="small"),
        "grupo": st.column_config.TextColumn("Grupo", width="small"),
        "unidades_faltantes": st.column_config.NumberColumn("Unid. faltantes", format="%.0f"),
        "monto_diferencia": st.column_config.TextColumn("Monto diferencia", width="medium"),
        "atribuible": st.column_config.CheckboxColumn("¿Subpedido identificado?"),
        "cancelado": st.column_config.CheckboxColumn("Cancelado"),
    },
)

# ── Drill a subpedido ────────────────────────────────────────────────────
filas_sel = seleccion.selection.rows
if not filas_sel:
    st.info("Selecciona una fila para ver el detalle a nivel de subpedido.")
else:
    fila = tabla_pedidos.iloc[filas_sel[0]]
    st.divider()
    st.subheader(f"Detalle — pedido {fila['id_pedido']}")

    detalle = vista[vista["id_pedido"] == fila["id_pedido"]][
        ["numero_subpedido", "grupo", "unidades_faltantes", "monto_diferencia", "atribuible"]
    ]
    if not fila["atribuible"]:
        st.caption(
            "Este pedido tiene varios subpedidos y la diferencia es puramente monetaria — "
            "el origen no identifica cuál la causó (DEC-117)."
        )
    st.dataframe(
        detalle,
        hide_index=True,
        width="stretch",
        column_config={
            "numero_subpedido": st.column_config.TextColumn("Subpedido", width="small"),
            "grupo": st.column_config.TextColumn("Grupo", width="small"),
            "unidades_faltantes": st.column_config.NumberColumn("Unid. faltantes", format="%.0f"),
            "monto_diferencia": st.column_config.TextColumn("Monto diferencia", width="medium"),
            "atribuible": st.column_config.CheckboxColumn("¿Subpedido identificado?"),
        },
    )
