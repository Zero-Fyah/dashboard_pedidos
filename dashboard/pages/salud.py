"""
Salud de inventario — cobertura, movimiento y quiebres (DEC-049).

Responde tres preguntas: ¿alcanza el inventario?, ¿se está moviendo?,
¿hay algo detenido?

Lee `v_inventario_salud`, que el scheduler recalcula en cada corrida. No
agrega ninguna fuente de datos: sale del inventario disponible del sistema
administrativo y de los ~7 meses de demanda que ya tiene el scraper.

**Lo que esta vista NO muestra, a propósito:** rotación financiera, días
de inventario (DOH) y GMROI necesitan el costo de la mercancía, y el
catálogo solo publica precio de venta. El "exceso sobre el máximo
objetivo" necesita un stock objetivo por referencia, que el negocio no
tiene definido. Mostrarlos con el precio de venta como sustituto del costo
daría cifras que parecen financieras y no lo son.
"""

import sqlite3

import plotly.graph_objects as go
import streamlit as st

from comun import (
    COBERTURA_ALTA_D,
    COBERTURA_RIESGO_D,
    VIGENCIA_ACTIVO,
    VIGENCIA_DESCONTINUADO,
)
from comun.reposicion import (
    ESTADO_OK,
    ESTADO_PEDIR_PRONTO,
    ESTADO_PEDIR_YA,
    calcular_reposicion,
)
from db import get_inventario_abc, get_inventario_corrida, get_inventario_salud
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🩺 Salud de inventario")

try:
    df = get_inventario_salud()
    corrida = get_inventario_corrida()
    # DEC-071: aporta la clase y el `cv` de la demanda para el punto de reorden.
    abc = get_inventario_abc()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay cálculo de salud de inventario. Se genera en cada ciclo del "
        "scheduler, o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

if corrida and corrida.get("datos_desactualizados"):
    st.warning(
        f"⚠️ La fuente más antigua tiene {corrida['fuente_mas_vieja_h']:.1f} horas. "
        "Probablemente falló una descarga: las cifras de abajo pueden estar desfasadas."
    )

# DEC-065: la salud se lee sobre el catálogo vigente. El descontinuado se
# reporta aparte, al final: no es un problema de abastecimiento y mezclarlo
# hacía que la categoría más grande de la página fuera 96% catálogo apagado.
descontinuado = df[df["vigencia"] == VIGENCIA_DESCONTINUADO]
df = df[df["vigencia"] == VIGENCIA_ACTIVO]

if df.empty:
    st.warning(
        f"Las {len(descontinuado):,} referencias del alcance están marcadas como "
        "descontinuadas en el sistema administrativo: no hay catálogo vigente que "
        "evaluar. Revisar la columna «Producto activo o inactivo» en el origen."
    )
    st.stop()

con_stock = df[df["disponible"] > 0]
quiebre = df[df["estado"] == "Quiebre"]
riesgo = df[df["estado"] == "Riesgo de quiebre"]
detenido = df[df["dias_sin_salida"] > 180]

# ── Lo accionable primero ──────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "En quiebre",
    f"{len(quiebre):,}",
    help="Sin stock y con demanda en los últimos 90 días. Son las que duelen.",
)
k2.metric(
    "En riesgo",
    f"{len(riesgo):,}",
    help="Menos de 15 días de cobertura al ritmo de venta de los últimos 90 días.",
)
k3.metric(
    "Con stock",
    f"{len(con_stock):,}",
    help=f"De {len(df):,} referencias vigentes del catálogo.",
)
k4.metric(
    "Detenidas +180 días",
    f"{len(detenido):,}",
    help="Sin una sola salida en más de 180 días.",
)

if len(quiebre):
    st.error(
        f"**{len(quiebre):,} referencias en quiebre** — sin stock disponible pero con "
        f"demanda en los últimos 90 días ({quiebre['demanda_90d'].sum():,.0f} unidades "
        "vendidas en ese lapso). Son las que tienen impacto comercial directo.",
        icon="🚨",
    )

st.divider()

# ── Composición ────────────────────────────────────────────────────────────────
st.subheader("Cómo se reparte el catálogo vigente")

orden = [
    "Quiebre",
    "Riesgo de quiebre",
    "Normal",
    "Cobertura alta",
    "Sin demanda",
    "Sin stock ni demanda",
]
conteo = df["estado"].value_counts().reindex(orden).fillna(0).astype(int)

c1, c2 = st.columns([1, 1])
with c1:
    for estado in orden:
        st.markdown(f"**{conteo[estado]:,}** · {estado}")
    st.caption(
        "«Sin stock ni demanda» es catálogo por depurar, no un problema de "
        "abastecimiento: son referencias vigentes en cero que además nadie "
        "pidió. El producto ya descontinuado no entra acá — va en el bloque "
        "del final."
    )

with c2:
    # Barras horizontales: la comparación es entre categorías nombradas y
    # los nombres son largos. Una sola serie, así que no lleva leyenda —
    # el título ya la nombra.
    fig = go.Figure()
    fig.add_bar(
        y=orden[::-1],
        x=[conteo[e] for e in orden[::-1]],
        orientation="h",
        marker_color=GRAFICO_SERIES[0],
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        hovertemplate="<b>%{y}</b><br>%{x:,.0f} referencias<extra></extra>",
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=10, b=10),
        height=260,
        showlegend=False,
    )
    fig.update_xaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
    fig.update_yaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
    st.plotly_chart(fig, config={"displayModeBar": False})

st.divider()

# ── Antigüedad del movimiento ──────────────────────────────────────────────────
st.subheader("Cuánto hace que no sale cada referencia")

# El primer corte arranca sin piso y el último no tiene techo: así ninguna
# referencia se cae del histograma en silencio si una fecha quedara fuera
# del rango esperado (p. ej. un pedido con fecha futura por desfase de reloj).
cortes = [(-(10**6), 30), (31, 60), (61, 90), (91, 180), (181, 10**6)]
etiquetas = ["0-30 días", "31-60", "61-90", "91-180", "+180 días"]
valores = [
    int(((df["dias_sin_salida"] >= a) & (df["dias_sin_salida"] <= b)).sum()) for a, b in cortes
]
nunca = int(df["dias_sin_salida"].isna().sum())

fig2 = go.Figure()
fig2.add_bar(
    x=[*etiquetas, "Nunca salió"],
    y=[*valores, nunca],
    marker_color=GRAFICO_SERIES[0],
    marker_line=dict(color=BG_DEEP, width=2),
    marker_cornerradius=4,
    hovertemplate="<b>%{x}</b><br>%{y:,.0f} referencias<extra></extra>",
)
fig2.update_layout(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=TEXT_SECONDARY, size=12),
    margin=dict(l=10, r=10, t=10, b=10),
    height=300,
    showlegend=False,
    bargap=0.35,
)
fig2.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
fig2.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
st.plotly_chart(fig2, config={"displayModeBar": False})

if len(detenido):
    st.caption(
        f"Las {len(detenido):,} referencias detenidas por más de 180 días acumulan "
        f"**{detenido['valor_venta'].sum():,.0f}** a precio de venta. No es el costo "
        "inmovilizado — el catálogo no publica costo, así que esta cifra sobreestima "
        "la exposición financiera real."
    )

st.divider()

# ── Detalle ────────────────────────────────────────────────────────────────────
st.subheader("Detalle por referencia")

f1, f2, f3 = st.columns([1.5, 1.5, 1])
with f1:
    estados_sel = st.multiselect("Estado", orden, placeholder="Todos...")
with f2:
    familias = sorted(df["familia"].dropna().unique())
    familias_sel = st.multiselect("Familia", familias, placeholder="Todas...")
with f3:
    orden_sel = st.selectbox(
        "Ordenar por",
        ["Más urgente", "Mayor demanda", "Más días detenida", "Mayor cobertura"],
    )

vista = df.copy()
if estados_sel:
    vista = vista[vista["estado"].isin(estados_sel)]
if familias_sel:
    vista = vista[vista["familia"].isin(familias_sel)]

if orden_sel == "Más urgente":
    # Quiebre primero, después menor cobertura: el orden en que conviene atacar.
    prioridad = {e: i for i, e in enumerate(orden)}
    vista = (
        vista.assign(_p=vista["estado"].map(prioridad))
        .sort_values(["_p", "dias_cobertura"], na_position="last")
        .drop(columns="_p")
    )
elif orden_sel == "Mayor demanda":
    vista = vista.sort_values("demanda_90d", ascending=False)
elif orden_sel == "Más días detenida":
    vista = vista.sort_values("dias_sin_salida", ascending=False, na_position="first")
else:
    vista = vista.sort_values("dias_cobertura", ascending=False, na_position="last")

if vista.empty:
    st.info("Sin referencias para los filtros seleccionados.")
else:
    st.caption(f"{len(vista):,} referencias.")
    st.dataframe(
        vista[
            [
                "referencia",
                "familia",
                "estado",
                "disponible",
                "demanda_30d",
                "demanda_90d",
                "dias_cobertura",
                "ultima_salida",
                "dias_sin_salida",
                "valor_venta",
            ]
        ],
        hide_index=True,
        column_config={
            "referencia": st.column_config.TextColumn("Referencia", width="medium"),
            "familia": st.column_config.TextColumn("Familia", width="small"),
            "estado": st.column_config.TextColumn("Estado", width="medium"),
            "disponible": st.column_config.NumberColumn("Disponible", format="%.0f"),
            "demanda_30d": st.column_config.NumberColumn("Demanda 30d", format="%.0f"),
            "demanda_90d": st.column_config.NumberColumn("Demanda 90d", format="%.0f"),
            "dias_cobertura": st.column_config.NumberColumn("Días cobertura", format="%.0f"),
            "ultima_salida": st.column_config.TextColumn("Última salida", width="small"),
            "dias_sin_salida": st.column_config.NumberColumn("Días sin salir", format="%.0f"),
            "valor_venta": st.column_config.NumberColumn("Valor (precio venta)", format="%.0f"),
        },
    )
    st.download_button(
        "⬇️ Descargar (CSV)",
        vista.to_csv(index=False).encode("utf-8-sig"),
        file_name="salud_inventario.csv",
        mime="text/csv",
    )

# Los umbrales se interpolan desde `comun/`, no se escriben en la prosa: un
# número fijo acá quedaría contradiciendo la clasificación el día que cambie
# (ya pasó en la página de Operación). La ventana de 90 días sí va literal —
# está horneada en los nombres de columna (`demanda_90d`), no es un knob.
st.caption(
    "La cobertura se calcula con la demanda de los últimos 90 días, excluyendo "
    f"subpedidos cancelados. Umbrales de inventario (DEC-066): riesgo bajo "
    f"{COBERTURA_RIESGO_D} días, cobertura alta sobre {COBERTURA_ALTA_D}. "
    "Alcance: catálogo sin arenas y almacén Bogotá, el mismo del cruce de "
    "inventario. Solo referencias vigentes (DEC-065)."
)

# ── Reposición ─────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Cuándo pedir y cuánto")

st.markdown(
    "Lo de arriba dice **qué falta**. Esto dice **cuándo pedir**, que es lo que "
    "permite reponer antes del quiebre en vez de reaccionar después."
)

# El plazo de reposición NO está en los datos: no hay tabla de compras ni de
# proveedores (C.1/C.2 del plan siguen bloqueados). Se pide explícito en vez
# de esconder un default: un punto de reorden con un plazo inventado es peor
# que no tenerlo, porque parece un dato.
p1, p2 = st.columns(2)
lead_time = p1.number_input(
    "Plazo de reposición (días)",
    min_value=1,
    max_value=365,
    value=60,
    step=5,
    help="Desde que se emite la orden hasta que la mercancía está disponible "
    "en bodega. **No sale de los datos** — el sistema administrativo no expone "
    "compras. Poné el plazo real de tu proveedor principal.",
)
objetivo = p2.number_input(
    "Cobertura objetivo (días)",
    min_value=0,
    max_value=365,
    value=30,
    step=5,
    help="Cuántos días de venta querés tener encima al recibir el pedido. "
    "Define la cantidad sugerida, no el punto de reorden.",
)

repo = calcular_reposicion(
    df, abc, lead_time_dias=int(lead_time), dias_cobertura_objetivo=int(objetivo)
)

if repo.empty:
    st.info("No hay referencias vigentes con demanda en los últimos 90 días.")
else:
    ya = repo[repo["estado_reposicion"] == ESTADO_PEDIR_YA]
    pronto = repo[repo["estado_reposicion"] == ESTADO_PEDIR_PRONTO]

    r1, r2, r3 = st.columns(3)
    r1.metric(
        "Pedir ya",
        f"{len(ya):,}",
        help="Sin stock y con demanda: ya se está perdiendo venta.",
    )
    r2.metric(
        "Bajo el punto de reorden",
        f"{len(pronto):,}",
        help="Todavía hay stock, pero no alcanza para cubrir el plazo de "
        "reposición. Es la lista que evita el quiebre.",
    )
    r3.metric(
        "Referencias con demanda",
        f"{len(repo):,}",
        help="Sobre las que tiene sentido calcular un punto de reorden.",
    )

    st.caption(
        f"⚠️ **Todo este bloque depende del plazo de {int(lead_time)} días que "
        "pusiste arriba.** Es el único número acá que no sale de los datos "
        "medidos: la demanda diaria y la variabilidad (`cv`) salen del "
        "histórico real. Cambiá el plazo y mirá cómo se mueve la lista."
    )

    urgentes = repo[repo["estado_reposicion"] != ESTADO_OK]
    if len(urgentes):
        st.dataframe(
            urgentes[
                [
                    "referencia",
                    "clase",
                    "demanda_diaria",
                    "disponible",
                    "cobertura_dias",
                    "punto_reorden",
                    "stock_seguridad",
                    "sugerido_pedir",
                    "estado_reposicion",
                ]
            ],
            hide_index=True,
            height=380,
            column_config={
                "referencia": st.column_config.TextColumn("Referencia", width="small"),
                "clase": st.column_config.TextColumn("Clase", width="small"),
                "demanda_diaria": st.column_config.NumberColumn("Demanda/día", format="%.1f"),
                "disponible": st.column_config.NumberColumn("Stock", format="%.0f"),
                "cobertura_dias": st.column_config.NumberColumn("Días cobertura", format="%.0f"),
                "punto_reorden": st.column_config.NumberColumn(
                    "Punto de reorden",
                    format="%.0f",
                    help="Cuando el stock baja de acá, hay que emitir la orden.",
                ),
                "stock_seguridad": st.column_config.NumberColumn(
                    "Stock seguridad",
                    format="%.0f",
                    help="Colchón por variabilidad de la demanda. Sale del `cv` "
                    "que ya calcula la clasificación XYZ, no de un supuesto.",
                ),
                "sugerido_pedir": st.column_config.NumberColumn("Sugerido pedir", format="%.0f"),
                "estado_reposicion": st.column_config.TextColumn("Estado", width="medium"),
            },
        )
        st.download_button(
            "⬇️ Descargar sugerencia de compra (CSV)",
            urgentes.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"reposicion_lt{int(lead_time)}d.csv",
            mime="text/csv",
        )
    else:
        st.success("Ninguna referencia está por debajo de su punto de reorden.", icon="✅")

    st.caption(
        "**Lo que este cálculo no incluye, por falta de dato:** cantidad mínima "
        "de pedido, múltiplos de empaque, restricciones de contenedor y costo "
        "de ordenar. Sin eso la cantidad sugerida es un punto de partida para "
        "negociar con el proveedor, no una orden de compra."
    )


# ── Catálogo descontinuado ─────────────────────────────────────────────────────
if len(descontinuado):
    st.divider()
    st.subheader("Catálogo descontinuado")

    con_fisico = descontinuado[descontinuado["disponible"] > 0]
    st.markdown(
        f"**{len(descontinuado):,} referencias** están marcadas como inactivas en el "
        "sistema administrativo. No entran en los indicadores de arriba: no tener "
        "stock de algo que ya no se vende no es un quiebre."
    )
    d1, d2 = st.columns(2)
    d1.metric(
        "Descontinuadas con stock",
        f"{len(con_fisico):,}",
        help="Producto que ya no se vende pero sigue ocupando inventario. "
        "Son las accionables: liquidar, devolver o dar de baja.",
    )
    d2.metric(
        "Unidades involucradas",
        f"{con_fisico['disponible'].sum():,.0f}",
        help="Según el inventario del sistema administrativo.",
    )

    if len(con_fisico):
        st.dataframe(
            con_fisico.sort_values("disponible", ascending=False)[
                ["referencia", "familia", "disponible", "valor_venta", "ultima_salida"]
            ],
            hide_index=True,
            column_config={
                "referencia": st.column_config.TextColumn("Referencia", width="medium"),
                "familia": st.column_config.TextColumn("Familia", width="small"),
                "disponible": st.column_config.NumberColumn("Disponible", format="%.0f"),
                "valor_venta": st.column_config.NumberColumn("Valor (precio venta)", format="%.0f"),
                "ultima_salida": st.column_config.TextColumn("Última salida", width="small"),
            },
        )

    st.caption(
        "La vigencia viene de la columna «Producto activo o inactivo» del catálogo "
        "del admin. Qué significa cada uno de sus dos valores se estableció "
        "midiendo su correlación con inventario y demanda, no leyendo la etiqueta "
        "— ver DEC-065."
    )
