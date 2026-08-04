"""
Ciclo de vida del pedido — dónde están y dónde se traban (DEC-096).

`timeline_pedido` tenía **172.954 filas y cero consumidores**. Es la sección 1
de las ocho que captura el scraper y la fuente de la "Vista de ciclos
operacionales" que `integral.md` pide desde el principio; lo que existía en su
lugar medía solo alistamiento e inspección, que es **un tramo del ciclo, no el
ciclo**.

La propiedad que hace útil esta página: el timeline solo registra pasos
**completados**, así que el último paso de un pedido abierto es su estado
actual, y la antigüedad de ese paso es cuánto lleva parado ahí. Eso responde
"cuáles están bloqueados", la primera pregunta de `integral.md`, que hasta hoy
no tenía pantalla.

Las cuatro secciones contestan preguntas distintas y en este orden a propósito:
**dónde está el trabajo ahora** (lo accionable), **cuánto se pierde en el
camino**, **cuánto tarda punta a punta** y **en qué etapa se va el tiempo**.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from filtros import aplicar, aviso_alcance, barra_lateral

from db import (
    get_ciclo_pedidos,
    get_ciclo_transiciones,
    get_opciones_comerciales,
    get_rango_fechas,
)
from theme import (
    BG_DEEP,
    GRAFICO_GRID,
    GRAFICO_SERIES,
    STATUS_CRITICAL,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

# Un pedido abierto que lleva más de esto en la misma etapa dejó de estar "en
# proceso". No es un umbral de negocio —nadie lo definió— sino un corte de
# lectura, y la página lo dice para que nadie lo tome por una meta.
DIAS_ESTANCADO = 15

st.markdown('<p class="dp-breadcrumb">Dashboard / Pedidos</p>', unsafe_allow_html=True)
st.title("🔄 Ciclo de vida del pedido")

try:
    _min_fecha, _max_fecha = get_rango_fechas()
except (FileNotFoundError, sqlite3.OperationalError) as e:
    st.error(f"No se pudo leer la base ({e}).")
    st.stop()

if not _min_fecha:
    st.info("Todavía no hay pedidos en la base de datos.")
    st.stop()

_f = barra_lateral(_min_fecha, _max_fecha, get_opciones_comerciales())
aviso_alcance(_f)

try:
    df = aplicar(get_ciclo_pedidos(_f.desde, _f.hasta), _f)
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info("Sin pedidos con timeline en el periodo seleccionado.")
    st.stop()

_ahora = pd.Timestamp.now()
df["ultimo_evento_dt"] = pd.to_datetime(df["ultimo_evento"], errors="coerce")
df["dias_en_estado"] = (_ahora - df["ultimo_evento_dt"]).dt.total_seconds() / 86400
df["lead_time_d"] = (
    pd.to_datetime(df["entregado_en"], errors="coerce")
    - pd.to_datetime(df["inicio"], errors="coerce")
).dt.total_seconds() / 86400

abiertos = df[df["abierto"] == 1]

# ── 1. Dónde está el trabajo ahora ─────────────────────────────────────────────
st.subheader("Dónde está el trabajo abierto, ahora mismo")

estancados = abiertos[abiertos["dias_en_estado"] > DIAS_ESTANCADO]

k1, k2, k3, k4 = st.columns(4)
k1.metric("Pedidos abiertos", f"{len(abiertos):,}".replace(",", "."))
k2.metric(
    f"Parados más de {DIAS_ESTANCADO} días",
    f"{len(estancados):,}".replace(",", "."),
    delta="Requieren revisión" if len(estancados) else "Ninguno",
    delta_color="inverse" if len(estancados) else "normal",
)
k3.metric(
    "Valor retenido",
    f"${abiertos['valor'].sum() / 1e6:,.0f} M".replace(",", "."),
    help="Suma del total a pagar de los pedidos que siguen abiertos.",
)
k4.metric(
    "Antigüedad mediana",
    f"{abiertos['dias_en_estado'].median():.0f} d",
    help="Días que lleva el pedido abierto en su etapa actual.",
)

if abiertos.empty:
    st.success("No hay pedidos abiertos en el periodo.")
else:
    wip = (
        abiertos.groupby("estado_actual")
        .agg(
            pedidos=("id_pedido", "count"),
            dias_medianos=("dias_en_estado", "median"),
            dias_max=("dias_en_estado", "max"),
            valor=("valor", "sum"),
            estancados=("dias_en_estado", lambda s: int((s > DIAS_ESTANCADO).sum())),
        )
        .reset_index()
        .sort_values("pedidos", ascending=False)
    )

    c1, c2 = st.columns([1.1, 1])
    with c1:
        fig = go.Figure()
        fig.add_bar(
            x=wip["pedidos"],
            y=wip["estado_actual"],
            orientation="h",
            marker_color=GRAFICO_SERIES[0],
            marker_line={"color": BG_DEEP, "width": 1},
            marker_cornerradius=3,
            hovertemplate="<b>%{y}</b><br>%{x} pedidos abiertos<extra></extra>",
        )
        fig.update_layout(
            title={
                "text": "Pedidos abiertos por etapa",
                "font": {"color": TEXT_SECONDARY, "size": 13},
            },
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"color": TEXT_SECONDARY, "size": 12},
            margin={"l": 10, "r": 10, "t": 40, "b": 10},
            height=320,
            showlegend=False,
        )
        fig.update_xaxes(gridcolor=GRAFICO_GRID, automargin=True)
        fig.update_yaxes(
            showgrid=False, tickfont={"color": TEXT_PRIMARY}, automargin=True, autorange="reversed"
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    with c2:
        st.dataframe(
            wip[["estado_actual", "pedidos", "dias_medianos", "dias_max", "estancados"]],
            hide_index=True,
            width="stretch",
            height=320,
            column_config={
                "estado_actual": st.column_config.TextColumn("Etapa", width="medium"),
                "pedidos": st.column_config.NumberColumn("Abiertos", format="%d"),
                "dias_medianos": st.column_config.NumberColumn("Días (mediana)", format="%.0f"),
                "dias_max": st.column_config.NumberColumn("Días (máx)", format="%.0f"),
                "estancados": st.column_config.NumberColumn(f">{DIAS_ESTANCADO} d", format="%d"),
            },
        )

    peor = wip.sort_values("dias_medianos", ascending=False).iloc[0]
    st.caption(
        f"La etapa **«{peor['estado_actual']}»** concentra {int(peor['pedidos'])} pedidos "
        f"abiertos con una antigüedad mediana de **{peor['dias_medianos']:.0f} días**. "
        f"El umbral de {DIAS_ESTANCADO} días no es una meta de negocio: nadie la definió. "
        "Es un corte de lectura para separar lo que está en proceso de lo que dejó de "
        "moverse."
    )

    if not estancados.empty:
        with st.expander(f"Ver los {len(estancados)} pedidos parados", expanded=False):
            vista = estancados.sort_values("dias_en_estado", ascending=False)
            st.dataframe(
                vista[
                    [
                        "id_pedido",
                        "fecha",
                        "nombre_empresa",
                        "metodo_entrega",
                        "estado_actual",
                        "dias_en_estado",
                        "valor",
                    ]
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "id_pedido": st.column_config.TextColumn("Pedido"),
                    "fecha": st.column_config.TextColumn("Fecha"),
                    "nombre_empresa": st.column_config.TextColumn("Cliente", width="medium"),
                    "metodo_entrega": st.column_config.TextColumn("Canal"),
                    "estado_actual": st.column_config.TextColumn("Etapa", width="medium"),
                    "dias_en_estado": st.column_config.NumberColumn("Días parado", format="%.0f"),
                    "valor": st.column_config.NumberColumn("Valor", format="$%.0f"),
                },
            )
            st.download_button(
                "⬇️ Descargar pedidos parados (CSV)",
                vista.to_csv(index=False).encode("utf-8-sig"),
                file_name="pedidos_parados.csv",
                mime="text/csv",
            )

# ── 2. Embudo ──────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Cuánto se pierde en el camino")

st.markdown(
    "Cada barra son los pedidos que **alguna vez** llegaron a esa etapa. No es un "
    "embudo estricto: los tres canales recorren los mismos estados pero no todos "
    "los pedidos pasan por todos —un pedido de almacén no se despacha—. Se verificó "
    "que la ruta **no depende del canal**: Transportadora, Ruta y Almacén tocan cada "
    "estado a tasas parecidas."
)

try:
    trans = get_ciclo_transiciones(_f.desde, _f.hasta)
except sqlite3.OperationalError:
    trans = pd.DataFrame()

# Se cuenta dónde **quedó** cada pedido, no por dónde pasó: son cifras que suman
# el universo completo y no se solapan. Contar "alguna vez llegó a" daría
# porcentajes que suman más de 100 y que nadie puede interpretar de un vistazo.
embudo = (
    df.groupby("estado_actual")
    .size()
    .rename("terminaron_aqui")
    .reset_index()
    .rename(columns={"estado_actual": "estado"})
    .sort_values("terminaron_aqui", ascending=False)
)

total = len(df)
entregados = int(df["entregado_en"].notna().sum())
cancelados = int((df["estado_actual"] == "pedido cancelado").sum())

e1, e2, e3 = st.columns(3)
e1.metric("Pedidos en el periodo", f"{total:,}".replace(",", "."))
e2.metric(
    "Llegaron a entregarse",
    f"{100 * entregados / total:.1f}%",
    help=f"{entregados:,} pedidos con el paso «Recibido y recibido».".replace(",", "."),
)
e3.metric(
    "Terminaron cancelados",
    f"{100 * cancelados / total:.1f}%",
    delta=f"{cancelados:,} pedidos".replace(",", "."),
    delta_color="off",
)

st.dataframe(
    embudo,
    hide_index=True,
    width="stretch",
    column_config={
        "estado": st.column_config.TextColumn("Etapa donde quedó el pedido", width="medium"),
        "terminaron_aqui": st.column_config.NumberColumn("Pedidos", format="%d"),
    },
)

# ── 3. Lead time punta a punta ─────────────────────────────────────────────────
st.divider()
st.subheader("Cuánto tarda un pedido de punta a punta")

con_lead = df[df["lead_time_d"].notna()]
if con_lead.empty:
    st.info("Ningún pedido del periodo llegó a entregarse.")
else:
    l1, l2, l3, l4 = st.columns(4)
    l1.metric("Mediana", f"{con_lead['lead_time_d'].median():.1f} d")
    l2.metric("p25", f"{con_lead['lead_time_d'].quantile(0.25):.1f} d")
    l3.metric("p75", f"{con_lead['lead_time_d'].quantile(0.75):.1f} d")
    l4.metric("p95", f"{con_lead['lead_time_d'].quantile(0.95):.1f} d")

    por_mes = (
        con_lead.assign(mes=con_lead["fecha"].str.slice(0, 7))
        .groupby("mes")["lead_time_d"]
        .median()
        .reset_index()
    )
    fig2 = go.Figure()
    fig2.add_scatter(
        x=por_mes["mes"],
        y=por_mes["lead_time_d"],
        mode="lines+markers",
        line={"color": GRAFICO_SERIES[1], "width": 2, "shape": "spline"},
        hovertemplate="<b>%{x}</b><br>%{y:.1f} días<extra></extra>",
    )
    fig2.update_layout(
        title={
            "text": "Lead time mediano por mes del pedido",
            "font": {"color": TEXT_SECONDARY, "size": 13},
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": TEXT_SECONDARY, "size": 12},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=280,
        showlegend=False,
    )
    fig2.update_xaxes(showgrid=False, tickfont={"color": TEXT_PRIMARY}, automargin=True)
    fig2.update_yaxes(gridcolor=GRAFICO_GRID, ticksuffix=" d", automargin=True, rangemode="tozero")
    st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})

    st.caption(
        f"Del primer paso del timeline al «Recibido y recibido», sobre "
        f"{len(con_lead):,} pedidos entregados. ".replace(",", ".")
        + "Coincide de forma independiente con lo que mide la página de **Cumplimiento "
        "de entrega** por otro camino (evento `Entrega` contra fecha del pedido), lo que "
        "es una validación cruzada y no una repetición: son dos fuentes distintas del "
        "origen dando el mismo número."
    )

# ── 4. Dónde se va el tiempo ───────────────────────────────────────────────────
st.divider()
st.subheader("En qué etapa se va el tiempo")

if trans.empty:
    st.info("Sin transiciones registradas en el periodo.")
else:
    resumen = (
        trans.groupby(["origen", "destino"])["horas"]
        .agg(saltos="count", mediana="median", p90=lambda s: s.quantile(0.90))
        .reset_index()
    )
    # Menos de 300 observaciones no sostiene una mediana estable a esta escala.
    resumen = resumen[resumen["saltos"] >= 300].sort_values("mediana", ascending=False)

    if resumen.empty:
        st.info("Ninguna transición del periodo tiene volumen suficiente para una mediana.")
    else:
        resumen["mediana_d"] = resumen["mediana"] / 24
        st.dataframe(
            resumen[["origen", "destino", "saltos", "mediana", "p90"]].head(15),
            hide_index=True,
            width="stretch",
            column_config={
                "origen": st.column_config.TextColumn("Desde", width="medium"),
                "destino": st.column_config.TextColumn("Hasta", width="medium"),
                "saltos": st.column_config.NumberColumn("Observaciones", format="%d"),
                "mediana": st.column_config.NumberColumn("Mediana (h)", format="%.1f"),
                "p90": st.column_config.NumberColumn("p90 (h)", format="%.1f"),
            },
        )

        # La cola más accionable: el pedido ya está listo y espera.
        espera = resumen[
            (resumen["origen"] == "Listo para enviar") & (resumen["destino"] == "pdt despachar")
        ]
        if not espera.empty:
            fila = espera.iloc[0]
            st.markdown(
                f"<div style='border-left:3px solid {STATUS_CRITICAL};padding-left:12px'>"
                f"<b>El salto más accionable no es el más lento.</b> "
                f"«Listo para enviar → pdt despachar» tarda una mediana de "
                f"<b>{fila['mediana']:.0f} horas</b> sobre {int(fila['saltos']):,} "
                "observaciones. En esa etapa el pedido ya está alistado, inspeccionado y "
                "empacado: <b>no falta hacer nada, solo falta que salga</b>. Es cola pura, "
                "y es donde una hora ganada no cuesta trabajo adicional."
                "</div>".replace(",", "."),
                unsafe_allow_html=True,
            )

        st.caption(
            "Se miden saltos entre pasos **consecutivos** del timeline. Un mismo par "
            "puede repetirse dentro de un pedido: los de varios subpedidos recorren el "
            "timeline más de una vez, y cada repetición cuenta como una observación — "
            "que es lo correcto para medir duración de etapa, no de pedido. Se ocultan "
            "las transiciones con menos de 300 observaciones: a esta escala no sostienen "
            "una mediana estable."
        )
