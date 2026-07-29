"""
Inventario — bodega vs. sistema administrativo (DEC-048).

Consume las tres VIEWs que `inventario/persistencia.py` escribe en cada
corrida del scheduler (DEC-043). No lee Excel ni recalcula nada: el cruce
completo cuesta 14,19 s y acá la consulta son milisegundos.

La lectura de negocio (DEC-041, reencuadrada en DEC-051):

    inventario_teorico = disponible_venta + vendido_no_alistado
    diferencia         = bochica_total − inventario_teorico
    sobrante_altura    = bochica_altura − inventario_teorico

`sobrante_altura` positivo es el hallazgo accionable: si solo la altura ya
supera al teórico, hay sobrante físico sin necesidad de mirar picking.
Altura es la zona que el sistema de bodega sí registra bien, así que su
sesgo conocido no sirve de excusa. Sirve para validar que los movimientos
de montacarga se hayan registrado.
"""

import sqlite3
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import (
    get_cancelaciones,
    get_inventario_anomalias,
    get_inventario_comparacion,
    get_inventario_corrida,
    get_inventario_tendencia,
)
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

# Con menos días la variación es indistinguible del ruido: la tendencia se
# muestra como tabla hasta que haya historia suficiente.
DIAS_MINIMOS_TENDENCIA = 3

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("📦 Bodega vs. sistema administrativo")

try:
    corrida = get_inventario_corrida()
    df = get_inventario_comparacion()
    anomalias = get_inventario_anomalias()
except sqlite3.OperationalError as e:
    # AUD-M6: contención normal con el ETL/scraper escribiendo.
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if corrida is None or df.empty:
    st.info(
        "El cruce de inventario todavía no corrió. Se ejecuta en cada ciclo del "
        "scheduler, o a mano con `python -m inventario.persistencia`."
    )
    st.stop()


# ── Frescura de las fuentes ────────────────────────────────────────────────────
def _fmt(iso: object) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return str(iso)


st.caption(
    f"📅 Cruce ejecutado: {_fmt(corrida['ejecutado_en'])} · "
    f"Sistema administrativo: {_fmt(corrida['admin_actualizado_en'])} · "
    f"Bochica: {_fmt(corrida['bochica_actualizado_en'])}"
)

if corrida["datos_desactualizados"]:
    # Las líneas del .bat corren sin `&&`: una descarga caída deja el Excel
    # anterior en su sitio y el número parecería fresco (DEC-043).
    st.warning(
        f"⚠️ La fuente más antigua tiene {corrida['fuente_mas_vieja_h']:.1f} horas. "
        "Probablemente falló alguna descarga: las cifras de abajo mezclan fotos de "
        "momentos distintos y no deberían usarse para decidir."
    )

# ── Magnitudes ─────────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.metric("Inventario teórico", f"{df['inventario_teorico'].sum():,.0f}")
k2.metric(
    "Bochica — total",
    f"{df['bochica_total'].sum():,.0f}",
    help="Altura + picking, tal como lo reporta el sistema de bodega.",
)
k3.metric(
    "Diferencia",
    f"{df['diferencia'].sum():,.0f}",
    help="Bochica total − teórico. Positiva es sobrante, negativa faltante.",
)
k4.metric("Referencias", f"{len(df):,}")

d1, d2, d3 = st.columns(3)
d1.metric(
    "Altura (confiable)",
    f"{df['bochica_altura'].sum():,.0f}",
    help="Zona que el sistema de bodega registra bien.",
)
d2.metric(
    "Picking (inflado)",
    f"{df['bochica_picking'].sum():,.0f}",
    help="El sistema de bodega no descuenta los movimientos de picking, así que sobreestima.",
)
d3.metric(
    "Paso de montacarga",
    f"{df['bochica_paso'].sum():,.0f}",
    help="No es posición de almacenamiento: queda fuera de la fórmula.",
)

# ── Sobrante físico confirmado ─────────────────────────────────────────────────
sobrantes = df[df["sobrante_altura"] > 0]

if len(sobrantes):
    st.error(
        f"**Sobrante físico confirmado — {len(sobrantes):,} referencias, "
        f"{sobrantes['sobrante_altura'].sum():,.0f} unidades.**  \n"
        "En estas referencias el inventario de las ubicaciones de **altura** ya supera "
        "al teórico, **sin siquiera contar lo que haya en picking**. Como altura es la "
        "zona que el sistema de bodega sí registra bien, el sobrante es real y no un "
        "efecto del sesgo de picking.",
        icon="📦",
    )
    st.markdown(
        "**Qué revisar:** apunta a movimientos de montacarga ejecutados físicamente "
        "pero no registrados en el sistema — al almacenar mercancía que entra, o al "
        "reabastecer las ubicaciones de picking desde altura."
    )

st.divider()

# ── Tendencia del sobrante ─────────────────────────────────────────────────────
# Si el sobrante viene de movimientos sin registrar, la pregunta útil no es
# cuánto hay hoy sino si crece: creciendo, se siguen perdiendo movimientos;
# estable, el desfase es histórico y no se está agravando.
st.subheader("Cómo evoluciona el sobrante")

tendencia = get_inventario_tendencia()

if tendencia.empty:
    st.info("Todavía no hay corridas registradas para construir la tendencia.")
elif len(tendencia) < DIAS_MINIMOS_TENDENCIA:
    st.info(
        f"Hay **{len(tendencia)} día(s)** de historia. Se necesitan al menos "
        f"{DIAS_MINIMOS_TENDENCIA} para que una tendencia signifique algo — con menos, "
        "cualquier variación es indistinguible del ruido. La serie se va armando sola "
        "con cada corrida del scheduler."
    )
    st.dataframe(
        tendencia[["dia", "corridas", "sobrante_referencias", "sobrante_unidades"]],
        hide_index=True,
        column_config={
            "dia": st.column_config.TextColumn("Día", width="small"),
            "corridas": st.column_config.NumberColumn("Corridas", format="%d", width="small"),
            "sobrante_referencias": st.column_config.NumberColumn(
                "Referencias con sobrante", format="%d"
            ),
            "sobrante_unidades": st.column_config.NumberColumn(
                "Unidades de sobrante", format="%.0f"
            ),
        },
    )
else:
    ultimo = tendencia.iloc[-1]
    previo = tendencia.iloc[-2]
    delta_u = ultimo["sobrante_unidades"] - previo["sobrante_unidades"]
    delta_r = int(ultimo["sobrante_referencias"] - previo["sobrante_referencias"])

    t1, t2, t3 = st.columns(3)
    t1.metric(
        "Sobrante hoy",
        f"{ultimo['sobrante_unidades']:,.0f}",
        f"{delta_u:+,.0f} vs. día anterior",
        # Crecer es malo: se siguen perdiendo movimientos.
        delta_color="inverse",
    )
    t2.metric(
        "Referencias afectadas",
        f"{int(ultimo['sobrante_referencias']):,}",
        f"{delta_r:+d}",
        delta_color="inverse",
    )
    t3.metric("Días de historia", f"{len(tendencia):,}")

    # Una sola serie: sin leyenda, el título ya la nombra.
    fig_t = go.Figure()
    fig_t.add_scatter(
        x=tendencia["dia"],
        y=tendencia["sobrante_unidades"],
        mode="lines+markers",
        line=dict(color=GRAFICO_SERIES[0], width=2, shape="spline"),
        marker=dict(size=8, color=GRAFICO_SERIES[0], line=dict(color=BG_DEEP, width=2)),
        fill="tozeroy",
        fillcolor="rgba(29,158,117,0.12)",
        hovertemplate="<b>%{x}</b><br>%{y:,.0f} unidades de sobrante<extra></extra>",
    )
    fig_t.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=TEXT_SECONDARY, size=12),
        margin=dict(l=10, r=10, t=20, b=10),
        height=300,
        showlegend=False,
        hovermode="x unified",
    )
    fig_t.update_xaxes(
        showgrid=False,
        linecolor=GRAFICO_GRID,
        tickfont=dict(color=TEXT_PRIMARY),
        automargin=True,
    )
    fig_t.update_yaxes(
        gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, tickformat=",.0f", automargin=True
    )
    st.plotly_chart(fig_t, config={"displayModeBar": False})

    st.caption(
        "Un punto por día, tomando la última corrida de cada uno. **Si la línea sube, "
        "se siguen ejecutando movimientos sin registrar**; si se aplana, el desfase es "
        "histórico y dejó de agravarse."
    )

st.divider()

# ── Comparación por familia ────────────────────────────────────────────────────
st.subheader("Teórico y reportado, por familia")

por_familia = (
    df.assign(familia=df["familia"].fillna("Sin familia"))
    .groupby("familia", as_index=False)[["inventario_teorico", "bochica_altura", "bochica_picking"]]
    .sum()
    .sort_values("inventario_teorico", ascending=False)
)

# Barras agrupadas: tres medidas en la misma unidad, un solo eje. Los colores
# se asignan en orden fijo desde la paleta validada del tema — nunca cíclico.
series = [
    ("Inventario teórico", "inventario_teorico"),
    ("Bochica — altura", "bochica_altura"),
    ("Bochica — picking", "bochica_picking"),
]
fig = go.Figure()
for (nombre, columna), color in zip(series, GRAFICO_SERIES, strict=True):
    fig.add_bar(
        x=por_familia["familia"],
        y=por_familia[columna],
        name=nombre,
        marker_color=color,
        # 2px de superficie entre barras adyacentes y extremo redondeado
        # anclado a la línea base.
        marker_line=dict(color=BG_DEEP, width=2),
        marker_cornerradius=4,
        hovertemplate=f"<b>%{{x}}</b><br>{nombre}: %{{y:,.0f}} unidades<extra></extra>",
    )

fig.update_layout(
    barmode="group",
    bargap=0.28,
    bargroupgap=0.06,
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    # El texto lleva tokens de texto, nunca el color de la serie.
    font=dict(color=TEXT_SECONDARY, size=12),
    # Con 3 series la leyenda es obligatoria: la identidad nunca queda solo
    # en el color.
    legend=dict(orientation="h", y=1.14, x=0, font=dict(color=TEXT_SECONDARY)),
    # Márgenes con aire: con l/b en 0 las etiquetas de ambos ejes salían
    # cortadas (las familias mutiladas abajo, los miles como "00" a la
    # izquierda). `automargin` los ajusta al contenido real en vez de
    # confiar en un valor fijo que se rompe al cambiar la escala.
    margin=dict(l=10, r=10, t=60, b=10),
    height=400,
    hovermode="x unified",
)
fig.update_xaxes(
    showgrid=False, linecolor=GRAFICO_GRID, tickfont=dict(color=TEXT_PRIMARY), automargin=True
)
fig.update_yaxes(
    gridcolor=GRAFICO_GRID,
    zerolinecolor=GRAFICO_GRID,
    tickformat=",.0f",
    title=None,
    automargin=True,
)
st.plotly_chart(fig, config={"displayModeBar": False})

st.caption(
    "Donde la barra de altura supera a la del teórico, la familia acumula sobrante "
    "físico confirmado. Los mismos datos están en la tabla siguiente."
)

st.divider()

# ── Detalle por referencia ─────────────────────────────────────────────────────
st.subheader("Detalle por referencia")

f1, f2, f3 = st.columns([2, 1, 1])
with f1:
    familias = sorted(df["familia"].dropna().unique())
    familias_sel = st.multiselect("Familia", familias, placeholder="Todas...")
with f2:
    averias_sel = st.selectbox("Averías", ["Todas", "Solo averías", "Sin averías"])
with f3:
    st.write("")
    solo_neg = st.checkbox(
        "Solo con sobrante en altura",
        help="Referencias donde la altura ya supera al teórico: sobrante físico confirmado.",
    )

vista = df.copy()
if familias_sel:
    vista = vista[vista["familia"].isin(familias_sel)]
if averias_sel == "Solo averías":
    vista = vista[vista["es_averia"] == 1]
elif averias_sel == "Sin averías":
    vista = vista[vista["es_averia"] != 1]
if solo_neg:
    vista = vista[vista["sobrante_altura"] > 0]

if vista.empty:
    st.info("Sin referencias para los filtros seleccionados.")
else:
    st.caption(f"{len(vista):,} referencias · ordenadas por sobrante en altura descendente.")
    st.dataframe(
        vista.sort_values("sobrante_altura", ascending=False)[
            [
                "referencia",
                "familia",
                "es_averia",
                "disponible_venta",
                "vendido_no_alistado",
                "inventario_teorico",
                "bochica_altura",
                "bochica_picking",
                "bochica_total",
                "diferencia",
                "sobrante_altura",
            ]
        ],
        hide_index=True,
        column_config={
            "referencia": st.column_config.TextColumn("Referencia", width="medium"),
            "familia": st.column_config.TextColumn("Familia", width="small"),
            "es_averia": st.column_config.CheckboxColumn("Avería", width="small"),
            "disponible_venta": st.column_config.NumberColumn("Disponible venta", format="%.0f"),
            "vendido_no_alistado": st.column_config.NumberColumn(
                "Vendido sin alistar", format="%.0f"
            ),
            "inventario_teorico": st.column_config.NumberColumn("Teórico", format="%.0f"),
            "bochica_altura": st.column_config.NumberColumn("Altura", format="%.0f"),
            "bochica_picking": st.column_config.NumberColumn("Picking", format="%.0f"),
            "bochica_total": st.column_config.NumberColumn("Bochica total", format="%.0f"),
            "diferencia": st.column_config.NumberColumn("Diferencia", format="%.0f"),
            "sobrante_altura": st.column_config.NumberColumn("Sobrante en altura", format="%.0f"),
        },
    )
    st.download_button(
        "⬇️ Descargar la comparación (CSV)",
        vista.to_csv(index=False).encode("utf-8-sig"),
        file_name="inventario_comparacion.csv",
        mime="text/csv",
    )

st.divider()

# ── Anomalías de ubicación ─────────────────────────────────────────────────────
st.subheader("Stock donde el layout dice que no debería haber")

if anomalias.empty:
    st.success("✅ Sin stock en ubicaciones que el layout marca como no disponibles.")
else:
    resumen = (
        anomalias.groupby("motivo", as_index=False)
        .agg(ubicaciones=("ubicacion", "nunique"), unidades=("cantidad", "sum"))
        .sort_values("unidades", ascending=False)
    )
    _MOTIVOS = {
        "paso_montacarga": "Paso de montacarga — el túnel transversal del rack, no es posición de almacenamiento",
        "estiba_nivel_superior": "Nivel superior de estiba completa — el stock debería estar todo en la altura 1",
        "posicion_no_habilitada": "Posición no habilitada — marcada NO en las tres alturas de picking",
    }
    a1, a2 = st.columns(2)
    a1.metric("Ubicaciones con stock inesperado", f"{anomalias['ubicacion'].nunique():,}")
    a2.metric("Unidades involucradas", f"{anomalias['cantidad'].sum():,.0f}")

    for _, fila in resumen.iterrows():
        st.markdown(
            f"**{fila['unidades']:,.0f} unidades** en {fila['ubicaciones']:,} ubicaciones — "
            f"{_MOTIVOS.get(fila['motivo'], fila['motivo'])}"
        )

    with st.expander("Ver el detalle por ubicación"):
        st.dataframe(
            anomalias,
            hide_index=True,
            column_config={
                "motivo": st.column_config.TextColumn("Motivo", width="medium"),
                "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
                "id_especificacion": st.column_config.TextColumn("ID especificación"),
                "cantidad": st.column_config.NumberColumn("Unidades", format="%.0f"),
            },
        )

st.divider()
st.subheader("🚚 Mercancía alistada que terminó cancelada")

try:
    cancelaciones = get_cancelaciones()
except sqlite3.OperationalError:
    cancelaciones = pd.DataFrame()

if cancelaciones.empty:
    st.caption("Sin subpedidos alistados y cancelados en el alcance del plan.")
else:
    rezagadas = cancelaciones[cancelaciones["dias_hasta_cancelacion"] > 30]
    c1, c2, c3 = st.columns(3)
    c1.metric("Subpedidos", f"{len(cancelaciones):,}".replace(",", "."))
    c2.metric("Unidades que salieron", f"{cancelaciones['unidades'].sum():,.0f}".replace(",", "."))
    c3.metric(
        "Con más de un mes de rezago",
        f"{len(rezagadas) / len(cancelaciones) * 100:.0f}%",
        help="Días entre el cierre del alistamiento y el registro de la cancelación.",
    )

    st.markdown(
        "Mercancía que **salió de su posición y nunca se despachó**. La lectura "
        "intuitiva —«se alistó y enseguida se canceló, está en el piso»— **no es la "
        "que dicen los datos**: el rezago mediano entre cerrar el alistamiento y "
        f"registrar la cancelación es de **{cancelaciones['dias_hasta_cancelacion'].median():.0f} "
        "días**. No es una devolución pendiente de hoy, es mercancía que estuvo en "
        "estado indeterminado durante meses — y por eso es una explicación candidata "
        "de las diferencias que el conteo va a encontrar."
    )

    mensual = (
        cancelaciones.groupby("mes", as_index=False)
        .agg(subpedidos=("id_pedido", "size"), unidades=("unidades", "sum"))
        .sort_values("mes")
    )
    fig_c = go.Figure(
        go.Bar(
            x=mensual["mes"],
            y=mensual["unidades"],
            marker={"color": GRAFICO_SERIES[2]},
            hovertemplate="%{x}<br>%{y:,.0f} unidades<extra></extra>",
        )
    )
    fig_c.update_layout(
        height=260,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        paper_bgcolor=BG_DEEP,
        plot_bgcolor=BG_DEEP,
        font={"color": TEXT_PRIMARY},
        xaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "type": "category"},
        yaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "title": "Unidades"},
    )
    st.plotly_chart(fig_c, width="stretch")

    with st.expander(f"Ver los {len(cancelaciones)} subpedidos"):
        st.dataframe(
            cancelaciones.sort_values("unidades", ascending=False),
            hide_index=True,
            width="stretch",
            column_config={
                "id_pedido": st.column_config.TextColumn("Pedido", width="small"),
                "numero_subpedido": st.column_config.TextColumn("Subpedido", width="small"),
                "mes": st.column_config.TextColumn("Mes", width="small"),
                "alistador": st.column_config.TextColumn("Alistó", width="medium"),
                "cierre_alistamiento": st.column_config.TextColumn("Cerró alistamiento"),
                "cancelado_en": st.column_config.TextColumn("Se canceló"),
                "dias_hasta_cancelacion": st.column_config.NumberColumn(
                    "Días de rezago", format="%.0f"
                ),
                "lineas": st.column_config.NumberColumn("Líneas", format="%d"),
                "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
                "valor": st.column_config.NumberColumn("Valor", format="$%d"),
            },
        )
    st.caption(
        "Alcance del plan: excluye arena por tonelada y otros almacenes. Sin ese "
        "filtro serían 147.435 unidades, pero el 79% es arena y tapa la señal real."
    )


st.caption(
    "Alcance: solo ubicaciones del layout de bodega. La mercancía recibida por peso "
    "(buckets Q/R1/YU/Z, prefijos PU*, otras sedes) queda fuera del cruce por decisión "
    "de negocio: se incorpora cuando el flujo de unidades esté controlado."
)
