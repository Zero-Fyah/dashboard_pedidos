"""
Mapa de bodega — ocupación y valor por zona (DEC-061, D.2 + A.1).

Responde dónde está lo que hay: qué racks están llenos, dónde se concentra
el valor y qué posiciones están vacías. Es la base de la utilización de
espacio (D.2 del catálogo) y, en cuanto entren conteos, del mapa de
exactitud por zona que pide A.1.

**Incluye las posiciones vacías.** Un mapa de ocupación construido solo
sobre lo que tiene inventario no puede decir qué tan llena está la bodega:
le falta el denominador. Por eso el universo lo pone el layout —5.189
posiciones activas— y el contenido se le suma encima.

La vista general es **rack × nivel**: 16 racks por 7 niveles son 112 celdas,
legibles de un vistazo. El detalle por posición (50 por rack) solo tiene
sentido mirando un rack a la vez, y así se muestra.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import get_inventario_posiciones
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SECUENCIAL, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🗺️ Mapa de bodega")

try:
    df = get_inventario_posiciones()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay mapa de posiciones. Se genera en cada ciclo del scheduler, "
        "o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

METRICAS = {
    "Ocupación (%)": ("ocupada", "mean", ".0%"),
    "Posiciones ocupadas": ("ocupada", "sum", ",.0f"),
    "Unidades": ("unidades", "sum", ",.0f"),
    "Valor": ("valor", "sum", ",.0f"),
    "Líneas SKU-posición": ("lineas", "sum", ",.0f"),
}

f1, f2 = st.columns([1, 1])
tipos = sorted(df["tipo"].dropna().unique())
tipo_sel = f1.multiselect("Tipo de ubicación", tipos, default=tipos)
metrica = f2.selectbox("Qué mostrar", list(METRICAS))

vista = df[df["tipo"].isin(tipo_sel)]
if vista.empty:
    st.warning("No hay posiciones con ese filtro.")
    st.stop()

campo, operacion, formato = METRICAS[metrica]


# ─────────────────────────────────────────────
# Resumen
# ─────────────────────────────────────────────
total = len(vista)
ocupadas = int(vista["ocupada"].sum())
c1, c2, c3, c4 = st.columns(4)
c1.metric("Posiciones activas", f"{total:,}".replace(",", "."))
c2.metric("Ocupadas", f"{ocupadas:,}".replace(",", "."), delta=f"{ocupadas / total * 100:.1f}%")
c3.metric("Vacías", f"{total - ocupadas:,}".replace(",", "."))
c4.metric("Valor almacenado", f"${vista['valor'].sum():,.0f}".replace(",", "."))


# Máximo de columnas que admite una etiqueta por celda. Por encima, los
# números se pisan entre sí: con 50 posiciones el detalle por rack salía
# como "100%100%100%..." ilegible. Ahí manda el color y el valor va en el
# hover.
_MAX_COLUMNAS_CON_ETIQUETA = 20


def _heatmap(tabla: pd.DataFrame, titulo_x: str, titulo_y: str, altura_px: int) -> go.Figure:
    """Heatmap con anotación por celda y color de texto adaptativo.

    El número va dentro de la celda porque un mapa sin cifras obliga a
    volver a la leyenda por cada celda — salvo que la grilla sea tan densa
    que las etiquetas se solapen, y entonces sobran (ver
    `_MAX_COLUMNAS_CON_ETIQUETA`).
    """
    valores = tabla.to_numpy(dtype=float)
    finitos = valores[~pd.isna(valores)]
    maximo = finitos.max() if finitos.size else 0

    figura = go.Figure(
        go.Heatmap(
            z=valores,
            x=[str(c) for c in tabla.columns],
            y=[str(i) for i in tabla.index],
            colorscale=[
                [i / (len(GRAFICO_SECUENCIAL) - 1), c] for i, c in enumerate(GRAFICO_SECUENCIAL)
            ],
            hovertemplate=f"{titulo_y} %{{y}} · {titulo_x} %{{x}}<br>{metrica}: %{{z:{formato}}}"
            "<extra></extra>",
            colorbar={
                "title": {"text": metrica, "font": {"color": TEXT_SECONDARY}},
                "tickfont": {"color": TEXT_SECONDARY},
                "tickformat": formato,
            },
        )
    )
    anotaciones = []
    for i, _fila in enumerate(
        tabla.index if len(tabla.columns) <= _MAX_COLUMNAS_CON_ETIQUETA else []
    ):
        for j, _columna in enumerate(tabla.columns):
            valor = valores[i][j]
            vacia = pd.isna(valor)
            if vacia:
                # Una celda sin dato queda del color del fondo, que en el
                # extremo oscuro de la rampa se confunde con "ocupación
                # muy baja". El guion la desambigua de un vistazo: no es
                # poco, es que esa combinación no existe en el layout.
                texto, color = "—", TEXT_SECONDARY
            else:
                texto = f"{valor:{formato}}".replace(",", ".")
                # El texto se decide contra el fondo de SU celda. Ojo con el
                # sentido de la rampa: acá el valor BAJO es la celda oscura
                # (`GRAFICO_SECUENCIAL[0]` es el paso más oscuro), así que
                # los bajos llevan texto claro y los altos, oscuro. Al revés
                # —que es como salió la primera versión— los extremos quedan
                # oscuro-sobre-oscuro y blanco-sobre-casi-blanco.
                color = "#FFFFFF" if valor < maximo * 0.6 else "#0B1220"
            anotaciones.append(
                {
                    # Índices, no nombres: en un eje de categorías Plotly lee
                    # un `x` que parece número como el ÍNDICE de la categoría,
                    # así que con niveles "1".."7" las etiquetas salían
                    # corridas una columna y la última caía fuera del gráfico.
                    "x": j,
                    "y": i,
                    "text": texto,
                    "showarrow": False,
                    "font": {"size": 10, "color": color},
                }
            )
    figura.update_layout(
        height=altura_px,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        paper_bgcolor=BG_DEEP,
        plot_bgcolor=BG_DEEP,
        font={"color": TEXT_PRIMARY},
        annotations=anotaciones,
        xaxis={
            "title": titulo_x,
            "gridcolor": GRAFICO_GRID,
            "automargin": True,
            "type": "category",
        },
        yaxis={
            "title": titulo_y,
            "gridcolor": GRAFICO_GRID,
            "automargin": True,
            "type": "category",
            "autorange": "reversed",
        },
    )
    return figura


# ─────────────────────────────────────────────
# Vista general: rack × nivel
# ─────────────────────────────────────────────
st.divider()
st.subheader(f"{metrica} por rack y nivel")

general = (
    vista.groupby(["rack", "nivel"])[campo]
    .agg(operacion)
    .reset_index()
    .pivot(index="rack", columns="nivel", values=campo)
)
st.plotly_chart(_heatmap(general, "Nivel", "Rack", 30 * len(general) + 120), width="stretch")
st.caption(
    "Los niveles 1 a 3 son picking (gestión de Bodega) y del 4 al 7, altura "
    "(gestión de Inventarios). El corte lo confirma el propio layout."
)


# ─────────────────────────────────────────────
# Detalle de un rack
# ─────────────────────────────────────────────
st.divider()
racks = sorted(vista["rack"].dropna().astype(str).unique())
rack_sel = st.selectbox("Ver un rack en detalle", racks)
st.subheader(f"Rack {rack_sel} — {metrica.lower()} por posición")

detalle = vista[vista["rack"].astype(str) == rack_sel]
tabla = (
    detalle.groupby(["nivel", "posicion"])[campo]
    .agg(operacion)
    .reset_index()
    .pivot(index="nivel", columns="posicion", values=campo)
)
st.plotly_chart(_heatmap(tabla, "Posición", "Nivel", 40 * len(tabla) + 140), width="stretch")
st.caption(
    "Con 50 posiciones por rack las cifras no caben en la celda, así que acá manda el "
    "color y el valor sale al pasar el mouse. En **Ocupación** cada posición solo puede "
    "estar llena o vacía, así que el mapa se lee como un plano de huecos libres."
)

vacias = detalle[detalle["ocupada"] == 0]
if len(vacias):
    with st.expander(f"Posiciones vacías del rack {rack_sel} ({len(vacias)})"):
        st.dataframe(
            vacias[["ubicacion", "tipo", "posicion", "nivel"]].sort_values(["nivel", "posicion"]),
            hide_index=True,
            width="stretch",
            column_config={
                "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
                "tipo": st.column_config.TextColumn("Tipo", width="small"),
                "posicion": st.column_config.NumberColumn("Posición", format="%d"),
                "nivel": st.column_config.NumberColumn("Nivel", format="%d"),
            },
        )

st.caption(
    "El valor está a precio de venta del catálogo, no a costo: sirve para comparar "
    "zonas entre sí, no como cifra financiera."
)
