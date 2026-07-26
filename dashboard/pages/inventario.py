"""
Inventario — bodega vs. sistema administrativo (DEC-048).

Consume las tres VIEWs que `inventario/persistencia.py` escribe en cada
corrida del scheduler (DEC-043). No lee Excel ni recalcula nada: el cruce
completo cuesta 14,19 s y acá la consulta son milisegundos.

La lectura de negocio, cerrada en DEC-041:

    inventario_teorico = disponible_venta + vendido_no_alistado
    picking_estimado   = inventario_teorico − bochica_altura
    diferencia         = bochica_picking − picking_estimado

`altura` es la fuente confiable; `picking` es la incógnita, porque el
sistema de bodega nunca descuenta sus movimientos. Por eso el estimado se
calcula restando altura al teórico, en vez de creerle a picking.
"""

import sqlite3
from datetime import datetime

import plotly.graph_objects as go
import streamlit as st

from db import get_inventario_anomalias, get_inventario_comparacion, get_inventario_corrida
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

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
k2.metric("Bochica — altura", f"{df['bochica_altura'].sum():,.0f}", help="Fuente confiable.")
k3.metric(
    "Bochica — picking",
    f"{df['bochica_picking'].sum():,.0f}",
    help="Inflado por diseño: el sistema de bodega no descuenta los movimientos de picking.",
)
k4.metric("Referencias", f"{len(df):,}")

# ── El hallazgo, presentado como tal ───────────────────────────────────────────
negativos = df[df["picking_estimado"] < 0]
total_estimado = df["picking_estimado"].sum()

if len(negativos):
    st.error(
        f"**Hallazgo sin explicar — {len(negativos):,} referencias con picking estimado "
        f"negativo ({negativos['picking_estimado'].sum():,.0f} unidades).**  \n"
        "En estas referencias el inventario de altura, que es la fuente confiable, ya "
        "supera al teórico sin contar picking. El sesgo conocido de picking no lo "
        "explica. Hipótesis sin verificar: el sistema administrativo no publica como "
        "disponible mercancía que sí está en altura (reservas, tránsito, inactivos), o "
        "el conteo de altura arrastra error en las posiciones de estiba completa.",
        icon="🔍",
    )
    st.caption(
        f"Agregado del estimado: {total_estimado:,.0f} unidades. **No es una métrica "
        "cerrada** — es la pregunta que esta vista existe para investigar."
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
    "Donde la barra de altura supera a la del teórico, la referencia entra en el "
    "hallazgo de arriba. Los mismos datos están en la tabla siguiente."
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
        "Solo picking estimado negativo",
        help="Las referencias del hallazgo: altura supera al teórico.",
    )

vista = df.copy()
if familias_sel:
    vista = vista[vista["familia"].isin(familias_sel)]
if averias_sel == "Solo averías":
    vista = vista[vista["es_averia"] == 1]
elif averias_sel == "Sin averías":
    vista = vista[vista["es_averia"] != 1]
if solo_neg:
    vista = vista[vista["picking_estimado"] < 0]

if vista.empty:
    st.info("Sin referencias para los filtros seleccionados.")
else:
    st.caption(f"{len(vista):,} referencias · ordenadas por picking estimado ascendente.")
    st.dataframe(
        vista.sort_values("picking_estimado")[
            [
                "referencia",
                "familia",
                "es_averia",
                "disponible_venta",
                "vendido_no_alistado",
                "inventario_teorico",
                "bochica_altura",
                "bochica_picking",
                "picking_estimado",
                "diferencia",
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
            "picking_estimado": st.column_config.NumberColumn("Picking estimado", format="%.0f"),
            "diferencia": st.column_config.NumberColumn("Diferencia", format="%.0f"),
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

st.caption(
    "Alcance: solo ubicaciones del layout de bodega. La mercancía recibida por peso "
    "(buckets Q/R1/YU/Z, prefijos PU*, otras sedes) queda fuera del cruce por decisión "
    "de negocio: se incorpora cuando el flujo de unidades esté controlado."
)
