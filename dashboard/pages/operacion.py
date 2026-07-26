"""
Operación — tiempos de ciclo y productividad del almacén (DEC-054).

Sale de las marcas de tiempo que el scraper ya recolecta. **No hay registro
de horas trabajadas**, así que no se calculan unidades por hora-hombre:
lo que se mide es throughput y tiempos de ciclo, que sí son deducibles.
"""

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import get_operacion_ciclos
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

# Los tiempos tienen cola larga (p90 de ciclo en ~70 h contra una mediana
# de 22 h): la mediana describe el caso típico y el promedio lo distorsiona.
PERCENTILES = [0.5, 0.9]

st.markdown('<p class="dp-breadcrumb">Dashboard / Operación</p>', unsafe_allow_html=True)
st.title("⏱ Tiempos de ciclo y productividad")

try:
    df = get_operacion_ciclos()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay cálculo de operación. Se genera en cada ciclo del scheduler, "
        "o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

# ── Filtro de periodo ──────────────────────────────────────────────────────────
dias = sorted(df["dia"].dropna().unique())
col_f1, col_f2 = st.columns([2, 2])
with col_f1:
    rango = st.select_slider(
        "Periodo",
        options=dias,
        value=(dias[max(0, len(dias) - 30)], dias[-1]),
        help="Por defecto, los últimos 30 días con actividad.",
    )
with col_f2:
    inspectores = sorted(df["inspector"].dropna().unique())
    inspector_sel = st.multiselect("Inspector", inspectores, placeholder="Todos...")

vista = df[(df["dia"] >= rango[0]) & (df["dia"] <= rango[1])]
if inspector_sel:
    vista = vista[vista["inspector"].isin(inspector_sel)]

if vista.empty:
    st.info("Sin subpedidos para el periodo y filtros seleccionados.")
    st.stop()

# ── Tiempos de ciclo ───────────────────────────────────────────────────────────
st.subheader("Cuánto tarda un subpedido en atravesar el proceso")

k1, k2, k3, k4 = st.columns(4)
k1.metric("Subpedidos", f"{len(vista):,}")
k2.metric(
    "Cola + picking (mediana)",
    f"{vista['cola_y_picking_h'].median():.1f} h",
    help=(
        "Desde que el subpedido entra a la cola hasta que el alistamiento físico "
        "termina. NO es tiempo de picking: la espera en cola lo domina."
    ),
)
k3.metric(
    "Inspección (mediana)",
    f"{vista['inspeccion_h'].median():.1f} h",
    help="Desde que termina el alistamiento hasta que la inspección cierra.",
)
k4.metric(
    "Ciclo total (mediana)",
    f"{vista['ciclo_total_h'].median():.1f} h",
    help="Desde que entra a la cola hasta que la inspección cierra.",
)

st.caption(
    "Se usa la **mediana** y no el promedio: la distribución tiene cola larga y el "
    "promedio quedaría arrastrado por unos pocos subpedidos muy demorados."
)

etapas = [
    ("Cola + picking", "cola_y_picking_h"),
    ("Inspección", "inspeccion_h"),
    ("Ciclo total", "ciclo_total_h"),
]
resumen = pd.DataFrame(
    [
        {
            "Etapa": nombre,
            "Mediana (h)": vista[columna].median(),
            "p90 (h)": vista[columna].quantile(0.9),
            "Máximo (h)": vista[columna].max(),
        }
        for nombre, columna in etapas
    ]
)
st.dataframe(
    resumen,
    hide_index=True,
    column_config={
        "Mediana (h)": st.column_config.NumberColumn(format="%.1f"),
        "p90 (h)": st.column_config.NumberColumn(format="%.1f"),
        "Máximo (h)": st.column_config.NumberColumn(format="%.1f"),
    },
)

st.divider()

# ── Evolución diaria ───────────────────────────────────────────────────────────
st.subheader("Evolución diaria")

por_dia = (
    vista.groupby("dia", as_index=False)
    .agg(
        subpedidos=("id_pedido", "size"),
        lineas=("lineas", "sum"),
        unidades=("unidades", "sum"),
        ciclo_mediano=("ciclo_total_h", "median"),
    )
    .sort_values("dia")
)

# Dos medidas de escala distinta → dos gráficos, nunca dos ejes en uno.
g1, g2 = st.columns(2)
with g1:
    fig = go.Figure()
    fig.add_bar(
        x=por_dia["dia"],
        y=por_dia["subpedidos"],
        marker_color=GRAFICO_SERIES[0],
        marker_line=dict(color=BG_DEEP, width=1),
        marker_cornerradius=3,
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} subpedidos<extra></extra>",
    )
    fig.update_layout(
        title=dict(text="Subpedidos completados", font=dict(color=TEXT_SECONDARY, size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=40, b=10),
        height=280,
        showlegend=False,
        bargap=0.2,
    )
    fig.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    fig.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    st.plotly_chart(fig, config={"displayModeBar": False})

with g2:
    fig2 = go.Figure()
    fig2.add_scatter(
        x=por_dia["dia"],
        y=por_dia["ciclo_mediano"],
        mode="lines",
        line=dict(color=GRAFICO_SERIES[1], width=2, shape="spline"),
        hovertemplate="<b>%{x}</b><br>%{y:.1f} h de ciclo mediano<extra></extra>",
    )
    fig2.update_layout(
        title=dict(text="Ciclo total mediano (horas)", font=dict(color=TEXT_SECONDARY, size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=40, b=10),
        height=280,
        showlegend=False,
    )
    fig2.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    fig2.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    st.plotly_chart(fig2, config={"displayModeBar": False})

st.divider()

# ── Productividad por alistador ────────────────────────────────────────────────
st.subheader("Carga de trabajo por alistador")

compartidos = int((vista["n_alistadores"] > 1).sum())
st.info(
    f"**{compartidos:,} de {len(vista):,} subpedidos ({compartidos / len(vista) * 100:.0f}%) "
    "tuvieron más de un alistador.** Por eso se separan dos cifras: "
    "*participaciones* cuenta todos los subpedidos en que intervino cada persona "
    "(mide carga), y *exclusivos* solo aquellos donde fue el único alistador — es lo "
    "único que se le puede atribuir sin inventar un reparto.",
    icon="👥",
)

con_alistador = vista[vista["alistador"].notna()].copy()
if con_alistador.empty:
    st.info("Sin alistador registrado en los subpedidos del periodo.")
else:
    expandido = con_alistador.assign(persona=con_alistador["alistador"].str.split(",")).explode(
        "persona"
    )
    expandido["persona"] = expandido["persona"].str.strip()
    expandido = expandido[expandido["persona"] != ""]

    participaciones = expandido.groupby("persona").size().rename("participaciones")

    solos = con_alistador[con_alistador["n_alistadores"] == 1].copy()
    solos["persona"] = solos["alistador"].str.strip()
    exclusivos = solos.groupby("persona").agg(
        exclusivos=("id_pedido", "size"),
        lineas=("lineas", "sum"),
        unidades=("unidades", "sum"),
        ciclo_mediano_h=("ciclo_total_h", "median"),
    )

    tabla = (
        participaciones.to_frame()
        .join(exclusivos, how="left")
        .fillna({"exclusivos": 0, "lineas": 0, "unidades": 0})
        .sort_values("participaciones", ascending=False)
        .reset_index()
        .rename(columns={"persona": "Alistador"})
    )

    p1, p2, p3 = st.columns(3)
    p1.metric("Alistadores activos", f"{len(tabla):,}")
    p2.metric("Con atribución exclusiva", f"{int((tabla['exclusivos'] > 0).sum()):,}")
    p3.metric("Líneas atribuibles", f"{tabla['lineas'].sum():,.0f}")

    st.dataframe(
        tabla,
        hide_index=True,
        column_config={
            "Alistador": st.column_config.TextColumn(width="large"),
            "participaciones": st.column_config.NumberColumn("Participaciones", format="%d"),
            "exclusivos": st.column_config.NumberColumn("Subpedidos exclusivos", format="%d"),
            "lineas": st.column_config.NumberColumn("Líneas (exclusivas)", format="%.0f"),
            "unidades": st.column_config.NumberColumn("Unidades (exclusivas)", format="%.0f"),
            "ciclo_mediano_h": st.column_config.NumberColumn(
                "Ciclo mediano (h)",
                format="%.1f",
                help="Solo sobre sus subpedidos exclusivos, para que sea comparable.",
            ),
        },
    )
    st.download_button(
        "⬇️ Descargar (CSV)",
        tabla.to_csv(index=False).encode("utf-8-sig"),
        file_name="productividad_alistadores.csv",
        mime="text/csv",
    )

st.caption(
    "**Lo que esta vista no mide, por falta de dato:** unidades por hora-hombre "
    "(no hay registro de horas trabajadas), tiempo *dock-to-stock* (no hay datos de "
    "recepción) y exactitud de *put-away* (no se registra la ubicación asignada contra "
    "la real). El campo `alistamiento_completado` tampoco se usa: se verificó contra "
    "el registro de operaciones que marca el fin de la **inspección**, no del picking."
)
