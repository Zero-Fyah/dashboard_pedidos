"""
Clasificación ABC-XYZ (DEC-050).

ABC ordena por cuánto ingreso genera cada referencia; XYZ, por qué tan
predecible es su demanda. La matriz cruza ambas y sugiere una política por
celda.

Lee `v_inventario_abc`, que el scheduler recalcula en cada corrida. No
agrega ninguna fuente: usa el ingreso real ya registrado en
`lineas_pedido`.
"""

import sqlite3

import plotly.graph_objects as go
import streamlit as st

from db import get_inventario_abc
from theme import (
    BG_DEEP,
    GRAFICO_GRID,
    GRAFICO_SECUENCIAL,
    GRAFICO_SERIES,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🗂 Clasificación ABC-XYZ")

try:
    df = get_inventario_abc()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay clasificación. Se genera en cada ciclo del scheduler, o a "
        "mano con `python -m inventario.persistencia`."
    )
    st.stop()

con_consumo = df[df["abc"] != "Sin consumo"]
sin_consumo = df[df["abc"] == "Sin consumo"]

if con_consumo.empty:
    st.info("Ninguna referencia registró consumo en la ventana analizada.")
    st.stop()

# ── Resumen ────────────────────────────────────────────────────────────────────
resumen = con_consumo.groupby("abc").agg(
    refs=("referencia", "size"), valor=("valor_consumo", "sum")
)
total_valor = resumen["valor"].sum()

k1, k2, k3, k4 = st.columns(4)
for col, clase in zip((k1, k2, k3), ("A", "B", "C"), strict=True):
    if clase in resumen.index:
        refs = int(resumen.loc[clase, "refs"])
        pct = resumen.loc[clase, "valor"] / total_valor * 100
        col.metric(
            f"Clase {clase}",
            f"{refs:,} refs",
            f"{pct:.1f}% del ingreso",
            delta_color="off",
        )
k4.metric(
    "Sin consumo",
    f"{len(sin_consumo):,}",
    help="Referencias del catálogo sin una sola venta en la ventana analizada.",
)

st.caption(
    "Ventana: los 6 meses calendario completos anteriores al mes en curso. El mes "
    "en curso se excluye porque, a medio transcurrir, subestimaría el consumo y "
    "metería una caída artificial en la variabilidad. Alcance: catálogo sin arenas "
    "y almacén Bogotá, el mismo de las otras vistas de inventario."
)

st.divider()

# ── Pareto ─────────────────────────────────────────────────────────────────────
st.subheader("Concentración del ingreso")

curva = con_consumo.sort_values("valor_consumo", ascending=False).reset_index(drop=True)
curva["rank"] = curva.index + 1

fig = go.Figure()
fig.add_scatter(
    x=curva["rank"],
    y=curva["pct_acumulado"],
    mode="lines",
    line=dict(color=GRAFICO_SERIES[0], width=2, shape="spline"),
    fill="tozeroy",
    fillcolor="rgba(29,158,117,0.12)",
    hovertemplate="Referencia n.º %{x}<br>%{y:.1f}% del ingreso acumulado<extra></extra>",
)
# Los cortes de Pareto, como referencia recesiva.
for corte, etiqueta in ((80, "80% — corte A/B"), (95, "95% — corte B/C")):
    fig.add_hline(
        y=corte,
        line=dict(color=GRAFICO_GRID, width=1, dash="dot"),
        annotation_text=etiqueta,
        annotation_position="right",
        annotation_font=dict(color=TEXT_SECONDARY, size=11),
    )
fig.update_layout(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=TEXT_SECONDARY, size=12),
    margin=dict(l=10, r=10, t=20, b=10),
    height=320,
    showlegend=False,
    hovermode="x unified",
)
fig.update_xaxes(
    title="Referencias ordenadas por ingreso",
    showgrid=False,
    linecolor=GRAFICO_GRID,
    tickfont=dict(color=TEXT_PRIMARY),
    automargin=True,
)
fig.update_yaxes(
    title="% acumulado",
    gridcolor=GRAFICO_GRID,
    zerolinecolor=GRAFICO_GRID,
    range=[0, 100],
    automargin=True,
)
st.plotly_chart(fig, config={"displayModeBar": False})

st.divider()

# ── Matriz ─────────────────────────────────────────────────────────────────────
st.subheader("Matriz ABC-XYZ")

filas, columnas = ["A", "B", "C"], ["X", "Y", "Z"]
matriz = [
    [int(((con_consumo["abc"] == a) & (con_consumo["xyz"] == x)).sum()) for x in columnas]
    for a in filas
]

m1, m2 = st.columns([1.1, 1])
with m1:
    heat = go.Figure(
        go.Heatmap(
            z=matriz,
            x=columnas,
            y=filas,
            # Rampa secuencial de un solo tono: la magnitud es el conteo.
            colorscale=[
                [i / (len(GRAFICO_SECUENCIAL) - 1), c] for i, c in enumerate(GRAFICO_SECUENCIAL)
            ],
            hovertemplate="<b>%{y}%{x}</b><br>%{z:,} referencias<extra></extra>",
            showscale=False,
            xgap=2,
            ygap=2,
        )
    )

    # El número va como anotación y no con `texttemplate` porque el color
    # tiene que cambiar por celda: en blanco sobre el extremo claro de la
    # rampa el contraste es ilegible. Sobre las celdas claras se usa el
    # navy del fondo, que ahí sí contrasta.
    maximo = max(max(fila) for fila in matriz) or 1
    for i, clase in enumerate(filas):
        for j, variabilidad in enumerate(columnas):
            valor = matriz[i][j]
            claro = valor / maximo > 0.62  # a partir de ahí la celda ya es clara
            heat.add_annotation(
                x=variabilidad,
                y=clase,
                text=f"{valor:,}",
                showarrow=False,
                font=dict(size=16, color=BG_DEEP if claro else TEXT_PRIMARY),
            )
    heat.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=13),
        margin=dict(l=10, r=10, t=10, b=10),
        height=300,
    )
    heat.update_xaxes(
        title="XYZ — predecibilidad de la demanda",
        side="top",
        tickfont=dict(color=TEXT_PRIMARY, size=14),
        automargin=True,
    )
    heat.update_yaxes(
        title="ABC — aporte al ingreso",
        autorange="reversed",
        tickfont=dict(color=TEXT_PRIMARY, size=14),
        automargin=True,
    )
    st.plotly_chart(heat, config={"displayModeBar": False})

with m2:
    st.markdown(
        "**X** demanda estable (CV ≤ 0,5) · **Y** variable (≤ 1,0) · **Z** errática (> 1,0)"
    )
    celda_sel = st.selectbox(
        "Ver política de la celda",
        [f"{a}{x}" for a in filas for x in columnas],
    )
    politica = con_consumo[con_consumo["celda"] == celda_sel]["politica"].dropna()
    if not politica.empty:
        st.info(politica.iloc[0], icon="💡")
    n_celda = int((con_consumo["celda"] == celda_sel).sum())
    st.caption(f"{n_celda:,} referencias en {celda_sel}.")

z_nuevas = int(((con_consumo["xyz"] == "Z") & (con_consumo["meses_con_venta"] <= 2)).sum())
if z_nuevas:
    st.warning(
        f"⚠️ De las referencias clasificadas **Z**, {z_nuevas:,} vendieron en solo "
        "1 o 2 de los 6 meses. Con una ventana corta, un producto **nuevo** también "
        "sale con variabilidad alta: ahí el CV no mide volatilidad sino que antes no "
        "existía. Mirá la columna «Meses con venta» antes de decidir sobre ellas."
    )

st.divider()

# ── Detalle ────────────────────────────────────────────────────────────────────
st.subheader("Detalle por referencia")

f1, f2, f3 = st.columns(3)
with f1:
    abc_sel = st.multiselect("Clase ABC", ["A", "B", "C", "Sin consumo"], placeholder="Todas...")
with f2:
    xyz_sel = st.multiselect("Clase XYZ", ["X", "Y", "Z"], placeholder="Todas...")
with f3:
    familias = sorted(df["familia"].dropna().unique())
    fam_sel = st.multiselect("Familia", familias, placeholder="Todas...")

vista = df.copy()
if abc_sel:
    vista = vista[vista["abc"].isin(abc_sel)]
if xyz_sel:
    vista = vista[vista["xyz"].isin(xyz_sel)]
if fam_sel:
    vista = vista[vista["familia"].isin(fam_sel)]

if vista.empty:
    st.info("Sin referencias para los filtros seleccionados.")
else:
    st.caption(f"{len(vista):,} referencias · ordenadas por ingreso descendente.")
    st.dataframe(
        vista[
            [
                "referencia",
                "familia",
                "abc",
                "xyz",
                "celda",
                "valor_consumo",
                "pct_valor",
                "pct_acumulado",
                "unidades",
                "cv",
                "meses_con_venta",
                "politica",
            ]
        ],
        hide_index=True,
        column_config={
            "referencia": st.column_config.TextColumn("Referencia", width="medium"),
            "familia": st.column_config.TextColumn("Familia", width="small"),
            "abc": st.column_config.TextColumn("ABC", width="small"),
            "xyz": st.column_config.TextColumn("XYZ", width="small"),
            "celda": st.column_config.TextColumn("Celda", width="small"),
            "valor_consumo": st.column_config.NumberColumn("Ingreso 6 meses", format="%.0f"),
            "pct_valor": st.column_config.NumberColumn("% del total", format="%.2f"),
            "pct_acumulado": st.column_config.NumberColumn("% acumulado", format="%.1f"),
            "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
            "cv": st.column_config.NumberColumn("CV", format="%.2f"),
            "meses_con_venta": st.column_config.NumberColumn("Meses con venta", format="%.0f"),
            "politica": st.column_config.TextColumn("Política sugerida", width="large"),
        },
    )
    st.download_button(
        "⬇️ Descargar (CSV)",
        vista.to_csv(index=False).encode("utf-8-sig"),
        file_name="clasificacion_abc_xyz.csv",
        mime="text/csv",
    )

st.caption(
    "El ABC usa el **ingreso real** cobrado por línea, no un estimado de "
    "demanda × precio de catálogo. Los subpedidos cancelados quedan fuera. Las "
    "políticas por celda son guía estándar de gestión de inventarios, no una regla "
    "que el sistema aplique."
)
