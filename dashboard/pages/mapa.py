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

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import get_inventario_posiciones
from theme import (
    BG_DEEP,
    GRAFICO_GRID,
    GRAFICO_SECUENCIAL,
    GRAFICO_SERIES,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

# DEC-079: el plano físico solo dibuja alturas — es el alcance de la Fase 3.
TIPO_ALTURA_MAPA = "Altura"
NIVELES_ALTURA = (4, 5, 6, 7)
# Los dos bloques del plano; el orden de los racks es el físico (DEC-078).
BODEGAS_PLANO = (
    ("Bodega 4 — racks A a H", "ABCDEFGH"),
    ("Bodega 3 — racks I a P", "IJKLMNOP"),
)

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


# ─────────────────────────────────────────────
# Lectura rápida de la ocupación (DEC-080)
# ─────────────────────────────────────────────
# Tres preguntas que el heatmap responde pero obliga a leer celda por celda.
# Cada gráfico lleva **una sola serie** salvo el primero, donde las dos
# clases se separan por luminosidad (teal lleno / neutro vacío) y no por
# tono: así se leen igual en escala de grises y con cualquier daltonismo.
_VACIO_NEUTRO = "rgba(255,255,255,0.14)"

vista_g = vista.copy()
vista_g["bodega"] = (
    vista_g["rack"].astype(str).str[0].map(lambda r: "Bodega 4" if r <= "H" else "Bodega 3")
)

g1, g2 = st.columns([1.15, 1])

with g1:
    st.markdown("##### Cuánto espacio queda, por bodega y tipo")
    grupos = (
        vista_g.groupby(["bodega", "tipo"])
        .agg(total=("ocupada", "size"), ocupadas=("ocupada", "sum"))
        .reset_index()
    )
    grupos["vacias"] = grupos["total"] - grupos["ocupadas"]
    grupos["pct"] = grupos["ocupadas"] / grupos["total"] * 100
    grupos["fila"] = grupos["bodega"] + " · " + grupos["tipo"]
    # Plotly pone la primera categoría ABAJO, así que se ordena ascendente
    # por vacías para que la fila con más espacio libre quede arriba: es
    # la que responde la pregunta.
    grupos = grupos.sort_values("vacias")

    fig_b = go.Figure()
    fig_b.add_bar(
        y=grupos["fila"],
        x=grupos["ocupadas"],
        orientation="h",
        name="Ocupadas",
        marker_color=GRAFICO_SERIES[0],
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        customdata=grupos[["pct", "total"]],
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} ocupadas de %{customdata[1]:,.0f} "
        "(%{customdata[0]:.0f}%)<extra></extra>",
    )
    fig_b.add_bar(
        y=grupos["fila"],
        x=grupos["vacias"],
        orientation="h",
        name="Vacías",
        marker_color=_VACIO_NEUTRO,
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        text=[f"{v:,.0f} libres".replace(",", ".") for v in grupos["vacias"]],
        textposition="outside",
        cliponaxis=False,
        textfont=dict(color=TEXT_SECONDARY, size=11),
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} vacías<extra></extra>",
    )
    fig_b.update_layout(
        barmode="stack",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=120, t=10, b=10),
        height=260,
        bargap=0.35,
        legend=dict(orientation="h", y=-0.15, font=dict(color=TEXT_SECONDARY)),
    )
    fig_b.update_xaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    fig_b.update_yaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    st.plotly_chart(fig_b, config={"displayModeBar": False})

with g2:
    st.markdown("##### Ocupación por nivel")
    niveles = (
        vista_g.groupby("nivel")
        .agg(total=("ocupada", "size"), ocupadas=("ocupada", "sum"))
        .reset_index()
        .sort_values("nivel")
    )
    niveles["pct"] = niveles["ocupadas"] / niveles["total"] * 100

    fig_n = go.Figure()
    fig_n.add_bar(
        x=[f"Nivel {int(n)}" for n in niveles["nivel"]],
        y=niveles["pct"],
        marker_color=GRAFICO_SERIES[0],
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        # El valor va sobre la barra porque las diferencias son de pocos
        # puntos: a esta escala todas las barras se ven iguales y el eje
        # NO se trunca — una barra recortada miente sobre su magnitud.
        text=[f"{v:.0f}%" for v in niveles["pct"]],
        textposition="outside",
        cliponaxis=False,
        textfont=dict(color=TEXT_SECONDARY, size=11),
        customdata=niveles[["ocupadas", "total"]],
        hovertemplate="<b>%{x}</b><br>%{y:.0f}% · %{customdata[0]:,.0f} de "
        "%{customdata[1]:,.0f}<extra></extra>",
    )
    fig_n.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=28, b=10),
        height=260,
        bargap=0.35,
        showlegend=False,
    )
    fig_n.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    fig_n.update_yaxes(
        gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, ticksuffix="%", automargin=True
    )
    st.plotly_chart(fig_n, config={"displayModeBar": False})

st.markdown("##### Dónde está el espacio libre")
libres = (
    vista_g[vista_g["ocupada"] == 0]
    .groupby("rack")
    .size()
    .reset_index(name="vacias")
    .sort_values("vacias", ascending=False)
)
if libres.empty:
    st.success("No hay posiciones vacías con este filtro.", icon="✅")
else:
    fig_l = go.Figure()
    fig_l.add_bar(
        x=libres["rack"],
        y=libres["vacias"],
        marker_color=GRAFICO_SERIES[0],
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        hovertemplate="<b>Rack %{x}</b><br>%{y:,.0f} posiciones vacías<extra></extra>",
    )
    fig_l.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=10, b=10),
        height=240,
        bargap=0.3,
        showlegend=False,
    )
    fig_l.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    fig_l.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    st.plotly_chart(fig_l, config={"displayModeBar": False})
    st.caption(
        "Ordenado de más a menos espacio libre: es la respuesta a «dónde pongo "
        "lo que llega». Los racks **A a H son Bodega 4** y **I a P, Bodega 3** — "
        "por eso los primeros ocho del ranking son casi todos de Bodega 4. "
        "El detalle celda a celda está más abajo."
    )


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

# ─────────────────────────────────────────────
# Plano físico de alturas (DEC-079)
# ─────────────────────────────────────────────
st.divider()
st.subheader("Plano de alturas — qué posición está ocupada y cuál no")

st.markdown(
    "Reproduce la hoja **«Plano Layout»** del layout: cada columna es un nivel de "
    "altura de un rack y cada fila una posición. **Negro = ocupada. Sin relleno = "
    "vacía.** El gris es una posición que el layout no declara."
)

# El fondo es claro a propósito. En el tema oscuro del dashboard, "negro" y
# "sin relleno" serían el mismo color; sobre papel claro los dos son
# literales y el contraste es máximo, que es lo que un plano necesita.
_PAPEL = "#E9ECEF"  # posición inexistente — el hueco de la grilla
_VACIA = "#FFFFFF"
_OCUPADA = "#111111"
_TINTA = "#31424E"

alturas = df[df["tipo"] == TIPO_ALTURA_MAPA].copy()
if alturas.empty:
    st.info("El layout no declara posiciones de altura.")
else:
    alturas["rack"] = alturas["rack"].astype(str).str.strip().str.upper()
    alturas["nivel"] = pd.to_numeric(alturas["nivel"], errors="coerce").astype("Int64")
    alturas["posicion"] = pd.to_numeric(alturas["posicion"], errors="coerce").astype("Int64")

    vacias_alt = alturas[alturas["ocupada"] == 0]
    p1, p2, p3 = st.columns(3)
    p1.metric("Posiciones de altura", f"{len(alturas):,}".replace(",", "."))
    p2.metric(
        "Ocupadas",
        f"{int(alturas['ocupada'].sum()):,}".replace(",", "."),
        delta=f"{alturas['ocupada'].mean() * 100:.0f}%",
    )
    p3.metric(
        "Vacías",
        f"{len(vacias_alt):,}".replace(",", "."),
        help="Son las que hay que mirar cuando falta mercancía: una posición que el "
        "sistema da por vacía es exactamente donde puede aparecer lo que no se "
        "encontró donde debía estar.",
    )

    for etiqueta, racks_bodega in BODEGAS_PLANO:
        bloque = alturas[alturas["rack"].isin(list(racks_bodega))]
        if bloque.empty:
            continue

        # Una columna por (rack, nivel); las filas son las posiciones.
        bloque = bloque.assign(_col=bloque["rack"] + "·" + bloque["nivel"].astype(str))
        # Una columna en blanco entre racks: sin ella los 32 niveles corren
        # juntos y es imposible ver dónde termina un rack. El plano original
        # hace lo mismo con sus columnas de pasillo.
        columnas: list[str] = []
        for i, r in enumerate(racks_bodega):
            if i:
                columnas.append(f"_sep{i}")
            columnas.extend(f"{r}·{n}" for n in NIVELES_ALTURA)
        posiciones = sorted(bloque["posicion"].dropna().unique())

        z = bloque.pivot_table(
            index="posicion", columns="_col", values="ocupada", aggfunc="max"
        ).reindex(index=posiciones, columns=columnas)
        etiquetas = (
            bloque.pivot_table(
                index="posicion", columns="_col", values="ubicacion", aggfunc="first"
            )
            .reindex(index=posiciones, columns=columnas)
            .fillna("—")
        )
        unidades = (
            bloque.pivot_table(index="posicion", columns="_col", values="unidades", aggfunc="sum")
            .reindex(index=posiciones, columns=columnas)
            .fillna(0)
        )
        estado = z.map(lambda v: "vacía" if v == 0 else ("ocupada" if v == 1 else "no existe"))

        fig = go.Figure(
            go.Heatmap(
                z=z.to_numpy(dtype=float),
                x=columnas,
                y=[str(p) for p in posiciones],
                colorscale=[[0.0, _VACIA], [1.0, _OCUPADA]],
                zmin=0,
                zmax=1,
                showscale=False,
                # Separación de 1px entre celdas: el hueco muestra el papel y
                # hace de rejilla sin dibujar bordes sobre las marcas.
                xgap=1,
                ygap=1,
                customdata=np.dstack(
                    (etiquetas.to_numpy(), estado.to_numpy(), unidades.to_numpy())
                ),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>%{customdata[1]}<br>"
                    "%{customdata[2]:,.0f} unidades<extra></extra>"
                ),
            )
        )
        # Una etiqueta por rack, centrada sobre sus cuatro niveles.
        centros = [
            columnas[i * (len(NIVELES_ALTURA) + 1) + len(NIVELES_ALTURA) // 2]
            for i in range(len(racks_bodega))
        ]
        fig.update_layout(
            title=dict(text=etiqueta, font=dict(color=TEXT_PRIMARY, size=14), y=0.99),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor=_PAPEL,
            font=dict(color=_TINTA, size=11),
            # Margen superior amplio: el título y las etiquetas de rack
            # comparten la banda de arriba y se pisaban.
            margin=dict(l=54, r=10, t=76, b=10),
            height=max(420, 13 * len(posiciones)),
        )
        fig.update_xaxes(
            side="top",
            tickvals=centros,
            ticktext=list(racks_bodega),
            tickfont=dict(color=TEXT_PRIMARY, size=13),
            showgrid=False,
            zeroline=False,
        )
        fig.update_yaxes(
            autorange="reversed",
            title=dict(text="Posición", font=dict(color=TEXT_SECONDARY, size=11)),
            tickfont=dict(color=TEXT_SECONDARY, size=9),
            dtick=5,
            showgrid=False,
            zeroline=False,
        )
        st.plotly_chart(fig, config={"displayModeBar": False})

    # La lista de vacías es el producto accionable de este plano.
    if len(vacias_alt):
        st.download_button(
            f"⬇️ Descargar las {len(vacias_alt)} posiciones de altura vacías (CSV)",
            vacias_alt[["ubicacion", "rack", "posicion", "nivel", "clase_posicion"]]
            .sort_values(["rack", "posicion", "nivel"])
            .to_csv(index=False)
            .encode("utf-8-sig"),
            file_name="posiciones_altura_vacias.csv",
            mime="text/csv",
        )

    st.caption(
        "**Solo alturas** (niveles 4 a 7): es el alcance de la Fase 3 del plan. "
        "«Vacía» es lo que dice el sistema, no lo que hay — y esa diferencia es "
        "justamente el motivo de mirar el plano: cuando falta mercancía en una "
        "posición que figura ocupada, suele aparecer en una que figura vacía."
    )
