"""
Scorecard ejecutivo — el estado del área en una pantalla (DEC-059, H.1).

Resume los seis módulos sin obligar a entrar en ninguno. Está pensada para
la reunión mensual del Comité de Inventarios: qué está bien, qué no, y
hacia dónde va.

**El semáforo solo aparece donde hay una meta real.** El IRA por clase y la
frescura de las fuentes tienen umbral definido —el primero en la sección 12
del plan de inventario, el segundo en el propio pipeline—, así que se puede
decir si cumplen. Los demás indicadores se muestran con su valor y su
tendencia, **sin veredicto**: inventarles una meta para poder pintarlos de
verde o rojo daría una precisión que no existe, y la primera decisión que
alguien tomara mirando ese color sería sobre un número que nadie acordó.

Cuando el negocio defina metas para quiebres, cobertura o productividad,
entran al semáforo sin cambiar nada más.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from tz import a_hora_colombia

from comun import META_IRA_POR_CLASE, VIGENCIA_ACTIVO
from db import (
    get_alertas,
    get_inventario_corrida,
    get_inventario_corridas,
    get_inventario_salud,
    get_inventario_ubicaciones,
    get_ira,
    get_scorecard,
)
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Principal</p>', unsafe_allow_html=True)
st.title("📊 Estado del área")

try:
    alertas = get_alertas()
    ira = get_ira()
    salud = get_inventario_salud()
    ubicaciones = get_inventario_ubicaciones()
    corrida = get_inventario_corrida()
    corridas = get_inventario_corridas()
    # DEC-102: las cifras de pedidos, servicio y caja. Consulta propia por
    # rendimiento; su consistencia con las páginas de detalle está testeada.
    tarjeta = get_scorecard()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()


def _valor(fuente, campo, defecto=None):
    """Lee un campo de la corrida, venga como Series, DataFrame o dict."""
    if fuente is None:
        return defecto
    if isinstance(fuente, pd.DataFrame):
        if fuente.empty or campo not in fuente.columns:
            return defecto
        return fuente.iloc[0][campo]
    if isinstance(fuente, pd.Series):
        return fuente.get(campo, defecto)
    if isinstance(fuente, dict):
        return fuente.get(campo, defecto)
    return defecto


if _valor(corrida, "ejecutado_en") is None:
    st.info(
        "Todavía no hay ninguna corrida registrada. El scheduler la genera cada hora, "
        "o se fuerza con `python -m inventario.persistencia`."
    )
    st.stop()


# ─────────────────────────────────────────────
# Con meta definida: acá el semáforo significa algo
# ─────────────────────────────────────────────
st.subheader("Contra meta")

criticas = (
    int(((alertas["activa"] == 1) & (alertas["severidad"] == "Crítica")).sum())
    if not alertas.empty
    else 0
)
horas_fuente = _valor(corrida, "fuente_mas_vieja_h")
desactualizado = bool(_valor(corrida, "datos_desactualizados", 0))
# DEC-109: la hora de la fuente más vieja (admin o Bochica) en vez de las
# horas transcurridas — mismo dato que fuente_mas_vieja_h, mostrado como
# reloj. min() porque "más vieja" es la fecha más temprana de las dos.
_fuentes_ts = [
    t
    for t in (_valor(corrida, "admin_actualizado_en"), _valor(corrida, "bochica_actualizado_en"))
    if t
]
fuente_vieja_ts = min(_fuentes_ts) if _fuentes_ts else None
ira_global = None
if not ira.empty and "Global" in set(ira["clase"]):
    ira_global = float(ira[ira["clase"] == "Global"].iloc[0]["ira"])

c1, c2, c3 = st.columns(3)
c1.metric(
    "🔴 Alertas críticas",
    criticas,
    delta="Todo en orden" if criticas == 0 else "Requieren atención hoy",
    delta_color="normal" if criticas == 0 else "inverse",
    help="Meta: 0. Quiebres de clase A, discrepancias de conteo en clase A y fuentes vencidas.",
)
c2.metric(
    "📅 Última actualización",
    a_hora_colombia(fuente_vieja_ts) if fuente_vieja_ts else "—",
    delta="Al día" if not desactualizado else "Vencida",
    delta_color="normal" if not desactualizado else "inverse",
    help=(
        "Hora de la fuente más antigua entre admin y Bochica, en hora Colombia (UTC-5). "
        f"Meta: menos de 3 horas de antigüedad ({horas_fuente:.1f} h ahora)."
        if horas_fuente is not None
        else "Hora de la fuente más antigua entre admin y Bochica, en hora Colombia (UTC-5). "
        "Meta: menos de 3 horas de antigüedad."
    ),
)
if ira_global is not None:
    c3.metric(
        "🎯 IRA global",
        f"{ira_global:.1f}%",
        help="Meta por clase: A >95%, B >90%, C >85% (sección 12 del plan).",
    )
    incumple = [
        f"{r.clase} ({r.ira:.0f}%)"
        for r in ira[ira["clase"] != "Global"].itertuples(index=False)
        if r.clase in META_IRA_POR_CLASE and r.ira < META_IRA_POR_CLASE[r.clase]
    ]
    if incumple:
        c3.caption("Bajo meta: " + ", ".join(incumple))
else:
    c3.metric("🎯 IRA global", "—")
    c3.caption("Aparece con el primer conteo físico ingerido")


# ─────────────────────────────────────────────
# Sin meta definida: valor y tendencia, sin veredicto
# ─────────────────────────────────────────────
st.divider()
st.subheader("Sin meta definida")
st.caption(
    "Se muestran **sin semáforo a propósito**: el negocio todavía no fijó un umbral "
    "para ellos, y pintarlos de verde o rojo contra una meta inventada llevaría a "
    "decidir sobre un número que nadie acordó."
)

# DEC-067: sobre el catálogo vigente, igual que la página de Salud. Sin este
# filtro el scorecard contaba 155 quiebres contra los 81 de la página de
# detalle: 74 eran producto descontinuado, casi todo averías. Un quiebre de
# stock de una avería no existe — nadie repone mercancía dañada.
vigente = salud[salud["vigencia"] == VIGENCIA_ACTIVO] if not salud.empty else salud
quiebres = int((vigente["estado"] == "Quiebre").sum()) if not vigente.empty else 0
riesgo = int((vigente["estado"] == "Riesgo de quiebre").sum()) if not vigente.empty else 0
sobrante_ref = int(_valor(corrida, "sobrante_referencias", 0) or 0)
sobrante_uni = int(_valor(corrida, "sobrante_unidades", 0) or 0)

altura = ubicaciones[ubicaciones["tipo"] == "Altura"] if not ubicaciones.empty else pd.DataFrame()
posiciones = altura["ubicacion"].nunique() if not altura.empty else 0
ab_pendientes = 0
if not alertas.empty:
    cob = alertas[alertas["clave"] == "cobertura:altura_ab"]
    ab_pendientes = int(cob.iloc[0]["valor"]) if len(cob) else 0

d1, d2, d3, d4 = st.columns(4)
quiebres_alertados = int((alertas["tipo"] == "Quiebre de stock").sum()) if not alertas.empty else 0
d1.metric(
    "Referencias en quiebre",
    f"{quiebres:,}".replace(",", "."),
    help=f"{quiebres_alertados} son de clase A o B y están en el centro de alertas; "
    "las demás son clase C o sin rotación.",
)
d2.metric("En riesgo de quiebre", f"{riesgo:,}".replace(",", "."))
d3.metric(
    "Sobrante físico (unidades)",
    f"{sobrante_uni:,}".replace(",", "."),
    help=f"Por encima del teórico, en {sobrante_ref} referencias.",
)
d4.metric(
    "Posiciones A/B sin contar",
    f"{ab_pendientes:,}".replace(",", "."),
    help=f"De {posiciones:,} posiciones de altura ocupadas.".replace(",", "."),
)


# ─────────────────────────────────────────────
# Pedidos y servicio (DEC-102)
# ─────────────────────────────────────────────
# El scorecard tenía siete métricas y las siete eran de inventario, ignorando
# 29.931 pedidos, $84.088 M facturados y 227.666 eventos operacionales. Para el
# Comité —que también decide sobre servicio y caja— eso es media foto.
st.divider()
st.subheader("Pedidos y servicio")
st.caption(
    "Sale de una consulta propia por rendimiento: reusar las de las páginas de "
    "detalle costaba 6,6 s en la portada. Cada cifra usa **el mismo predicado** que "
    "su página, y hay tests que comparan las dos vías — es la lección de DEC-067, "
    "donde dos definiciones del mismo indicador divergieron en silencio."
)

if tarjeta is None:
    st.info("No se pudieron leer las cifras de pedidos.")
else:
    p1, p2, p3, p4 = st.columns(4)
    p1.metric(
        "Pedidos abiertos",
        f"{int(tarjeta['pedidos_abiertos']):,}".replace(",", "."),
        help="Con al menos un subpedido sin cerrar. Detalle en Pedidos → Ciclo de vida.",
    )
    p2.metric(
        "Valor retenido",
        f"${tarjeta['valor_retenido'] / 1e6:,.0f} M".replace(",", "."),
        help="Total a pagar de los pedidos que siguen abiertos.",
    )
    p3.metric(
        "Fill rate por pedido",
        f"{tarjeta['fill_rate']:.1f}%" if tarjeta["fill_rate"] is not None else "—",
        help="Pedidos que salieron completos. La escala más exigente de DEC-069: "
        "una sola línea corta arruina el pedido entero.",
    )
    _ot = tarjeta["on_time"]
    p4.metric(
        "Entregas a tiempo",
        f"{_ot:.1f}%" if _ot is not None else "—",
        help=f"Sobre {int(tarjeta['entregas_medibles']):,} pedidos con compromiso fechado "
        "y entrega registrada. Escala por día.".replace(",", "."),
    )
    st.caption(
        "⚠️ **Las entregas a tiempo dependen de una definición que el negocio no cerró.** "
        "Si «Hora de entrega» es un compromiso con el cliente, esta cifra es el indicador "
        "de servicio del área; si es una franja de programación interna, mide otra cosa "
        "(DEC-094). El detalle y la advertencia completa están en **Operación → "
        "Cumplimiento de entrega**."
    )


# ─────────────────────────────────────────────
# Tendencia
# ─────────────────────────────────────────────
if not corridas.empty and len(corridas) > 2:
    st.divider()
    st.subheader("Cómo viene evolucionando")

    hist = corridas.sort_values("ejecutado_en").copy()
    hist["ejecutado_en"] = pd.to_datetime(hist["ejecutado_en"], errors="coerce", utc=True)
    hist = hist.dropna(subset=["ejecutado_en"])
    # DEC-109: hora Colombia en el hover — antes se veía UTC crudo sin
    # ninguna etiqueta de zona horaria (más engañoso que un "UTC" explícito).
    hist["ejecutado_en"] = hist["ejecutado_en"].dt.tz_convert("Etc/GMT+5")

    fig = go.Figure()
    for campo, etiqueta, color in (
        ("sobrante_unidades", "Sobrante físico", GRAFICO_SERIES[0]),
        ("anomalias_unidades", "Anomalías de ubicación", GRAFICO_SERIES[1]),
    ):
        if campo in hist.columns:
            fig.add_trace(
                go.Scatter(
                    x=hist["ejecutado_en"],
                    y=hist[campo],
                    mode="lines",
                    name=etiqueta,
                    line={"color": color, "width": 2},
                    hovertemplate="%{x|%d %b %H:%M} (hora Colombia)<br>%{y:,.0f} unidades<extra>"
                    + etiqueta
                    + "</extra>",
                )
            )
    fig.update_layout(
        height=300,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        paper_bgcolor=BG_DEEP,
        plot_bgcolor=BG_DEEP,
        font={"color": TEXT_PRIMARY},
        legend={"orientation": "h", "y": -0.25, "font": {"color": TEXT_SECONDARY}},
        xaxis={"gridcolor": GRAFICO_GRID, "automargin": True},
        yaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "title": "Unidades"},
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        f"Sobre {len(hist)} corridas registradas. Una línea plana en el sobrante significa "
        "que la causa —movimiento físico sin registrar— sigue activa."
    )


# ─────────────────────────────────────────────
# A dónde ir
# ─────────────────────────────────────────────
st.divider()
if not alertas.empty:
    activas = int((alertas["activa"] == 1).sum())
    if activas:
        st.markdown(
            f"**{activas} alertas activas** esperando revisión — el detalle está en "
            "**Principal → Centro de alertas**."
        )
