"""
Ventas y descuentos — la dimensión comercial que el dashboard no tenía (DEC-097).

`estadisticas_monto` trae **15 conceptos con cobertura del 100%** de los pedidos
y ninguna de las diez páginas mostraba una serie de facturación. Las dos VIEWs
que lo exponen —`v_estadisticas_monto_num` (DEC-036) y `v_descuentos_lineas`
(DEC-035)— se construyeron con la justificación explícita "para el dashboard" y
el ETL las venía reconstruyendo cada hora sin que nadie las leyera.

**La cifra que se publica es la neta de cancelaciones.** De los $84.093 M
brutos, **$11,6 mil millones son pedidos con todos sus subpedidos cancelados**.
Un dashboard que muestre el bruto como "ventas" sobreestima la facturación en
14% — y esa es exactamente la clase de error que nadie detecta después, porque
el número se ve razonable.
"""

from __future__ import annotations

import sqlite3

import plotly.graph_objects as go
import streamlit as st
from filtros import aplicar, aviso_alcance, barra_lateral

from db import (
    get_descuentos_por_tipo,
    get_opciones_comerciales,
    get_rango_fechas,
    get_ventas,
    get_ventas_por_linea,
)
from theme import (
    BG_DEEP,
    GRAFICO_GRID,
    GRAFICO_SERIES,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


def _mm(valor: float) -> str:
    """Formatea en millones con separador de miles a la colombiana."""
    return f"${valor / 1e6:,.0f} M".replace(",", ".")


def _pesos(valor: float) -> str:
    """Formatea en pesos enteros. Para cifras chicas, donde `_mm` pierde todo.

    Un ticket de $1.347.900 mostrado en millones queda en "$1 M", que no sirve
    para nada: la métrica más pequeña de la página es la que más precisión pide.
    """
    return f"${valor:,.0f}".replace(",", ".")


st.markdown('<p class="dp-breadcrumb">Dashboard / Comercial</p>', unsafe_allow_html=True)
st.title("💰 Ventas y descuentos")
# DEC-104: esta página vive en «Fuera del alcance», que NO quiere decir fuera
# del proyecto. El dashboard tiene dos propósitos y esta sirve al segundo:
# consulta y análisis para las demás áreas, y base para las propuestas de
# mejora al sistema administrativo.
st.info(
    "**Fuera del alcance del área de inventarios.** Esta página no es trabajo "
    "diario de bodega: existe para que las demás áreas consulten y analicen lo "
    "que el pipeline captura, y para sostener las propuestas de mejora al "
    "sistema administrativo. Las cifras son igual de válidas que las del resto "
    "del dashboard.",
    icon="🧭",
)

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
    df = aplicar(get_ventas(_f.desde, _f.hasta), _f)
    # El mix y los descuentos se agregan en SQL y no traen las dimensiones
    # comerciales, así que solo respetan la fecha. La página lo advierte en vez
    # de dar a entender que el recorte por vendedor también los alcanza.
    por_linea = get_ventas_por_linea(_f.desde, _f.hasta)
    descuentos = get_descuentos_por_tipo(_f.desde, _f.hasta)
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info("Sin facturación en el periodo seleccionado. ¿Corrió el ETL?")
    st.stop()

vivos = df[df["cancelado"] == 0]
anulados = df[df["cancelado"] == 1]

# ── Facturación ────────────────────────────────────────────────────────────────
st.subheader("Facturación del periodo")

bruto = float(df["total"].sum())
neto = float(vivos["total"].sum())
perdido = float(anulados["total"].sum())

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "Facturación neta",
    _mm(neto),
    help="Pedidos cuyos subpedidos NO están todos cancelados. Es la cifra a usar.",
)
k2.metric(
    "Perdido por cancelación",
    _mm(perdido),
    delta=f"{100 * perdido / bruto:.1f}% del bruto",
    delta_color="inverse",
    help=f"{len(anulados):,} pedidos con todos sus subpedidos cancelados.".replace(",", "."),
)
k3.metric("Antes de IVA", _mm(float(vivos["antes_iva"].sum())))
k4.metric(
    "Ticket mediano",
    _pesos(float(vivos["total"].median())),
    help="Mediana y no promedio: la distribución tiene cola larga, y el promedio "
    f"({_pesos(float(vivos['total'].mean()))}) la sobreestima.",
)

st.caption(
    f"El bruto del periodo es **{_mm(bruto)}**, pero **{_mm(perdido)}** corresponde a "
    "pedidos totalmente cancelados. Todo lo que sigue usa la cifra neta. "
    "Hay además pedidos **parcialmente** cancelados, que no se descuentan: no son "
    "ni una venta perdida ni una venta completa, y separarlos exigiría prorratear "
    "con un criterio que nadie definió."
)

# ── Evolución ──────────────────────────────────────────────────────────────────
vivos = vivos.copy()
vivos["mes"] = vivos["fecha"].str.slice(0, 7)
por_mes = (
    vivos.groupby("mes")
    .agg(facturado=("total", "sum"), pedidos=("id_pedido", "count"))
    .reset_index()
)
por_mes["ticket"] = por_mes["facturado"] / por_mes["pedidos"]

g1, g2 = st.columns(2)
with g1:
    fig = go.Figure()
    fig.add_bar(
        x=por_mes["mes"],
        y=por_mes["facturado"] / 1e6,
        marker_color=GRAFICO_SERIES[0],
        marker_line={"color": BG_DEEP, "width": 1},
        marker_cornerradius=3,
        hovertemplate="<b>%{x}</b><br>$%{y:,.0f} M<extra></extra>",
    )
    fig.update_layout(
        title={"text": "Facturación neta por mes", "font": {"color": TEXT_SECONDARY, "size": 13}},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": TEXT_SECONDARY, "size": 12},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=280,
        showlegend=False,
        bargap=0.25,
    )
    fig.update_xaxes(showgrid=False, tickfont={"color": TEXT_PRIMARY}, automargin=True)
    fig.update_yaxes(gridcolor=GRAFICO_GRID, ticksuffix=" M", automargin=True)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

with g2:
    fig2 = go.Figure()
    fig2.add_scatter(
        x=por_mes["mes"],
        y=por_mes["ticket"] / 1e6,
        mode="lines+markers",
        line={"color": GRAFICO_SERIES[1], "width": 2, "shape": "spline"},
        hovertemplate="<b>%{x}</b><br>$%{y:,.2f} M por pedido<extra></extra>",
    )
    fig2.update_layout(
        title={"text": "Ticket promedio por mes", "font": {"color": TEXT_SECONDARY, "size": 13}},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": TEXT_SECONDARY, "size": 12},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=280,
        showlegend=False,
    )
    fig2.update_xaxes(showgrid=False, tickfont={"color": TEXT_PRIMARY}, automargin=True)
    fig2.update_yaxes(gridcolor=GRAFICO_GRID, ticksuffix=" M", automargin=True, rangemode="tozero")
    st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})

# ── Mix por línea de producto ──────────────────────────────────────────────────
st.divider()
st.subheader("Mix por línea de producto")

if por_linea.empty:
    st.info("Sin líneas de pedido en el periodo.")
else:
    mix = (
        por_linea.groupby("linea")
        .agg(lineas=("lineas", "sum"), unidades=("unidades", "sum"), valor=("valor", "sum"))
        .reset_index()
        .sort_values("valor", ascending=False)
    )
    total_mix = mix["valor"].sum()
    mix["pct_valor"] = 100 * mix["valor"] / total_mix
    mix["pct_lineas"] = 100 * mix["lineas"] / mix["lineas"].sum()

    m1, m2 = st.columns([1, 1.2])
    with m1:
        st.dataframe(
            mix[["linea", "valor", "pct_valor", "lineas", "pct_lineas"]],
            hide_index=True,
            width="stretch",
            column_config={
                "linea": st.column_config.TextColumn("Línea"),
                "valor": st.column_config.NumberColumn("Valor", format="$%.0f"),
                "pct_valor": st.column_config.NumberColumn("% valor", format="%.1f%%"),
                "lineas": st.column_config.NumberColumn("Líneas", format="%d"),
                "pct_lineas": st.column_config.NumberColumn("% líneas", format="%.1f%%"),
            },
        )
    with m2:
        top = mix.iloc[0]
        st.markdown(
            f"**{top['linea']} concentra el {top['pct_valor']:.0f}% del valor con solo el "
            f"{top['pct_lineas']:.0f}% de las líneas.** Es el patrón inverso al de "
            "Accesorios, que aporta la mayoría de las líneas y una fracción del dinero.\n\n"
            "La consecuencia operativa es directa: **el esfuerzo de bodega y el valor "
            "facturado no viven en la misma línea de producto.** Un plan de conteo o de "
            "slotting optimizado por número de líneas optimiza para el producto que menos "
            "factura."
        )
        st.caption(
            "El mix sale de `lineas_pedido.tipo`, que trae las tres categorías al 100%. "
            "**No se derivó del IVA**, que parecía la vía natural: los conceptos `IVA "
            "arena para gatos` / `accesorios` / `alimentos` solo cubren el 75,4% de la "
            "base gravable — el resto es producto sin IVA que no queda atribuido."
        )

# ── Descuentos ─────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Descuento concedido")

lista = float(vivos["precio_lista"].sum())
dto = float(vivos["descuento"].sum())
tasa = abs(100 * dto / lista) if lista else 0

d1, d2, d3 = st.columns(3)
d1.metric("Descuento total", _mm(abs(dto)))
d2.metric(
    "Tasa efectiva",
    f"{tasa:.1f}%",
    help="Descuento sobre el precio de lista, en pedidos no cancelados.",
)
d3.metric("Precio de lista", _mm(lista))

if not descuentos.empty:
    vista = descuentos.copy()
    vista["monto"] = vista["monto"].abs()
    vista = vista.sort_values("monto", ascending=False).head(12)
    st.dataframe(
        vista[["descuento_tipo", "pedidos", "lineas", "monto"]],
        hide_index=True,
        width="stretch",
        column_config={
            "descuento_tipo": st.column_config.TextColumn("Tipo de descuento", width="medium"),
            "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
            "lineas": st.column_config.NumberColumn("Líneas", format="%d"),
            "monto": st.column_config.NumberColumn("Monto concedido", format="$%.0f"),
        },
    )
    st.download_button(
        "⬇️ Descargar descuentos por tipo (CSV)",
        descuentos.to_csv(index=False).encode("utf-8-sig"),
        file_name="descuentos_por_tipo.csv",
        mime="text/csv",
    )
    st.caption(
        "El `descuento_tipo` es texto compuesto del origen: una misma línea puede "
        "acumular varios (`Almacen9% | Tipo de cambio3%`). Se respeta la combinación tal "
        "como viene — partirla repartiría el monto entre categorías con un criterio que "
        "el origen no da."
    )

# ── Concentración comercial ────────────────────────────────────────────────────
st.divider()
st.subheader("Dónde está concentrada la facturación")

c1, c2 = st.columns(2)


def _concentracion(col: str, etiqueta: str, columna_st) -> None:
    """Top 10 por facturación, con el peso del primero sobre el total."""
    agg = (
        vivos.groupby(col)
        .agg(facturado=("total", "sum"), pedidos=("id_pedido", "count"))
        .reset_index()
        .sort_values("facturado", ascending=False)
    )
    if agg.empty:
        return
    total = agg["facturado"].sum()
    agg["pct"] = 100 * agg["facturado"] / total
    columna_st.markdown(f"**Top 10 {etiqueta}** — de {len(agg):,} en total".replace(",", "."))
    columna_st.dataframe(
        agg.head(10)[[col, "pedidos", "facturado", "pct"]],
        hide_index=True,
        width="stretch",
        column_config={
            col: st.column_config.TextColumn(etiqueta.capitalize(), width="medium"),
            "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
            "facturado": st.column_config.NumberColumn("Facturado", format="$%.0f"),
            "pct": st.column_config.NumberColumn("% del total", format="%.1f%%"),
        },
    )
    top10 = agg.head(10)["pct"].sum()
    columna_st.caption(f"Los 10 primeros concentran el **{top10:.1f}%** de la facturación.")


_concentracion("vendedor", "vendedores", c1)
_concentracion("nombre_empresa", "clientes", c2)

st.caption(
    "`integral.md` pide filtros por vendedor, forma de pago y destino desde el primer "
    "día. Esta página es el primer lugar del dashboard donde esas dimensiones se usan "
    "para algo; los filtros globales que las cruzan con el resto llegan en su propia "
    "rebanada."
)
