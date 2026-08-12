"""
Clasificación ABC-XYZ jerárquica: familia → referencia → ID (DEC-053).

El análisis va de lo macro a lo micro. En cada nivel el Pareto se corre
**dentro del padre**: una referencia se compara contra las de su propia
familia, y un ID contra los de su propia referencia.

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

FILAS_ABC = ["A", "B", "C"]
COLUMNAS_XYZ = ["X", "Y", "Z"]

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🗂 Clasificación ABC-XYZ")

try:
    df = get_inventario_abc()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty or "nivel" not in df.columns:
    st.info(
        "Todavía no hay clasificación. Se genera en cada ciclo del scheduler, o a "
        "mano con `python -m inventario.persistencia`."
    )
    st.stop()

familias = df[df["nivel"] == "familia"]
referencias = df[df["nivel"] == "referencia"]
ids = df[df["nivel"] == "id"]


def _con_consumo(datos):
    return datos[datos["abc"] != "Sin consumo"]


def _matriz(datos) -> list[list[int]]:
    return [
        [int(((datos["abc"] == a) & (datos["xyz"] == x)).sum()) for x in COLUMNAS_XYZ]
        for a in FILAS_ABC
    ]


def _dibujar_matriz(datos, titulo: str) -> None:
    """Heatmap 3×3 con el conteo por celda."""
    matriz = _matriz(datos)
    heat = go.Figure(
        go.Heatmap(
            z=matriz,
            x=COLUMNAS_XYZ,
            y=FILAS_ABC,
            # Rampa secuencial de un solo tono: la magnitud es el conteo.
            colorscale=[
                [i / (len(GRAFICO_SECUENCIAL) - 1), c] for i, c in enumerate(GRAFICO_SECUENCIAL)
            ],
            hovertemplate="<b>%{y}%{x}</b><br>%{z:,} unidades<extra></extra>",
            showscale=False,
            xgap=2,
            ygap=2,
        )
    )
    # El número va como anotación y no con `texttemplate` porque el color
    # tiene que cambiar por celda: en blanco sobre el extremo claro de la
    # rampa el contraste es ilegible.
    maximo = max(max(fila) for fila in matriz) or 1
    for i, clase in enumerate(FILAS_ABC):
        for j, variabilidad in enumerate(COLUMNAS_XYZ):
            valor = matriz[i][j]
            claro = valor / maximo > 0.62
            heat.add_annotation(
                x=variabilidad,
                y=clase,
                text=f"{valor:,}",
                showarrow=False,
                font=dict(size=15, color=BG_DEEP if claro else TEXT_PRIMARY),
            )
    heat.update_layout(
        title=dict(text=titulo, font=dict(color=TEXT_SECONDARY, size=13)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=40, b=10),
        height=260,
    )
    heat.update_xaxes(side="top", tickfont=dict(color=TEXT_PRIMARY, size=13), automargin=True)
    heat.update_yaxes(
        autorange="reversed", tickfont=dict(color=TEXT_PRIMARY, size=13), automargin=True
    )
    st.plotly_chart(heat, config={"displayModeBar": False})


_COLUMNAS_TABLA = [
    "clave",
    "etiqueta",
    "abc",
    "xyz",
    "celda",
    "valor_consumo",
    "pct_valor",
    "unidades",
    "cv",
    "meses_con_venta",
    "politica",
]


def _tabla(datos, etiqueta_clave: str, *, mostrar_etiqueta: bool = False) -> None:
    columnas = [c for c in _COLUMNAS_TABLA if mostrar_etiqueta or c != "etiqueta"]
    st.dataframe(
        datos.sort_values("valor_consumo", ascending=False)[columnas],
        hide_index=True,
        column_config={
            "clave": st.column_config.TextColumn(etiqueta_clave, width="medium"),
            "etiqueta": st.column_config.TextColumn("Especificación", width="large"),
            "abc": st.column_config.TextColumn("ABC", width="small"),
            "xyz": st.column_config.TextColumn("XYZ", width="small"),
            "celda": st.column_config.TextColumn("Celda", width="small"),
            "valor_consumo": st.column_config.NumberColumn("Ingreso 6 meses", format="%.0f"),
            "pct_valor": st.column_config.NumberColumn("% de su grupo", format="%.2f"),
            "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
            "cv": st.column_config.NumberColumn("CV", format="%.2f"),
            "meses_con_venta": st.column_config.NumberColumn("Meses con venta", format="%.0f"),
            "politica": st.column_config.TextColumn("Política sugerida", width="large"),
        },
    )


st.caption(
    "El análisis va de lo macro a lo micro. **En cada nivel el Pareto se corre dentro "
    "del padre**: una referencia se compara contra las de su propia familia, no contra "
    "todo el catálogo. Ventana: 6 meses calendario completos, sin el mes en curso. "
    "Alcance: catálogo sin arenas y almacén Bogotá."
)

nivel_1, nivel_2, nivel_3 = st.tabs(
    ["1 · Familias", "2 · Referencias por familia", "3 · ID por referencia"],
    on_change="rerun",
)

# ── Nivel 1 ────────────────────────────────────────────────────────────────────
# on_change="rerun" (H1/H2, auditoría de rendimiento 2026-08-10/12): sin esto,
# Streamlit computa las 3 pestañas en cada rerun aunque solo una esté visible.
if nivel_1.open:
    with nivel_1:
        vivas = _con_consumo(familias)
        st.subheader("ABC-XYZ de las familias completas")

        c1, c2 = st.columns([1.3, 1])
        with c1:
            orden = vivas.sort_values("valor_consumo", ascending=False)
            fig = go.Figure()
            fig.add_bar(
                x=orden["clave"],
                y=orden["valor_consumo"],
                marker_color=GRAFICO_SERIES[0],
                marker_line=dict(color=BG_DEEP, width=2),
                marker_cornerradius=4,
                hovertemplate="<b>%{x}</b><br>%{y:,.0f} de ingreso<extra></extra>",
            )
            fig.update_layout(
                title=dict(text="Ingreso por familia", font=dict(color=TEXT_SECONDARY, size=13)),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_SECONDARY, size=12),
                margin=dict(l=10, r=10, t=40, b=10),
                height=260,
                showlegend=False,
                bargap=0.35,
            )
            fig.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
            fig.update_yaxes(
                gridcolor=GRAFICO_GRID,
                zerolinecolor=GRAFICO_GRID,
                tickformat=",.0f",
                automargin=True,
            )
            st.plotly_chart(fig, config={"displayModeBar": False})
        with c2:
            _dibujar_matriz(vivas, "Familias por celda")

        _tabla(familias, "Familia")

# ── Nivel 2 ────────────────────────────────────────────────────────────────────
if nivel_2.open:
    with nivel_2:
        st.subheader("ABC-XYZ de las referencias, dentro de su familia")
        disponibles = sorted(referencias["padre"].dropna().unique())
        familia_sel = st.selectbox("Familia", disponibles, key="fam_n2")

        del_grupo = referencias[referencias["padre"] == familia_sel]
        vivas = _con_consumo(del_grupo)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Referencias", f"{len(del_grupo):,}")
        k2.metric("Con consumo", f"{len(vivas):,}")
        k3.metric("Clase A", f"{int((del_grupo['abc'] == 'A').sum()):,}")
        k4.metric("Ingreso de la familia", f"{del_grupo['valor_consumo'].sum():,.0f}")

        if vivas.empty:
            st.info("Ninguna referencia de esta familia registró consumo en la ventana.")
        else:
            _dibujar_matriz(vivas, f"Referencias de {familia_sel} por celda")
            st.caption(
                "Los porcentajes son sobre el ingreso **de esta familia**: una referencia "
                "puede ser A acá sin serlo en el total del catálogo."
            )
        _tabla(del_grupo, "Referencia")

# ── Nivel 3 ────────────────────────────────────────────────────────────────────
if nivel_3.open:
    with nivel_3:
        st.subheader("ABC-XYZ de los ID, dentro de su referencia")

        con_ids = _con_consumo(ids)
        por_referencia = con_ids.groupby("padre").agg(
            ids=("clave", "size"), clases=("abc", "nunique"), celdas=("celda", "nunique")
        )
        mixtas = por_referencia[(por_referencia["ids"] > 1) & (por_referencia["clases"] > 1)]

        if len(mixtas):
            st.warning(
                f"**{len(mixtas):,} referencias contienen ID de clases ABC distintas** "
                f"(de {int((por_referencia['ids'] > 1).sum()):,} con más de un ID vendido). "
                "Como el slotting se hace por referencia, hoy esas variantes comparten una "
                "misma ubicación lógica aunque roten de forma muy distinta — es exactamente "
                "el recorrido de picking que se puede acortar bajando el slotting a nivel ID.",
                icon="🎯",
            )

        solo_mixtas = st.checkbox(
            "Ver solo referencias con ID de clases distintas",
            value=bool(len(mixtas)),
            help="Las que más ganarían con un slotting por ID.",
        )
        candidatas = sorted(mixtas.index if solo_mixtas else por_referencia.index)

        if not candidatas:
            st.info("No hay referencias con ID clasificables para este filtro.")
        else:
            ref_sel = st.selectbox("Referencia", candidatas, key="ref_n3")
            del_grupo = ids[ids["padre"] == ref_sel]
            vivos = _con_consumo(del_grupo)

            k1, k2, k3 = st.columns(3)
            k1.metric("ID de la referencia", f"{len(del_grupo):,}")
            k2.metric("Clases ABC distintas", f"{del_grupo['abc'].nunique():,}")
            k3.metric("Ingreso de la referencia", f"{del_grupo['valor_consumo'].sum():,.0f}")

            if not vivos.empty:
                _dibujar_matriz(vivos, f"ID de {ref_sel} por celda")
                st.caption(
                    "Los porcentajes son sobre el ingreso **de esta referencia**. Si los ID "
                    "caen en celdas distintas, conviene asignarles ubicaciones distintas."
                )
            _tabla(del_grupo, "ID de especificación", mostrar_etiqueta=True)

        st.caption(
            f"Se clasificaron {len(ids):,} ID. La atribución de una venta a un ID usa el par "
            "referencia/código de barras: **el 80,8% de las líneas resuelve a un ID único**, "
            "el resto queda sin atribuir porque su par apunta a más de una especificación. "
            "Esa ambigüedad es una de las tareas de calidad de datos pendientes."
        )
