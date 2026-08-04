"""
Cumplimiento de entrega — la mitad *on time* de OTIF (DEC-095).

`CLAUDE.md` daba esta mitad de E.1 por bloqueada "sin fecha comprometida en el
origen". El compromiso estaba en `pedidos.hora_entrega` desde el primer día
(DEC-093) y la entrega real en el evento `Entrega`; lo que faltaba era
cruzarlos. Son **19.858 pedidos** con las dos puntas.

**Dos escalas, las dos correctas** — mismo criterio que DEC-069 con el fill
rate. Por día (¿llegó el día pactado?) es el estándar del sector y da 9,1%; por
ventana (¿llegó dentro de la franja?) es la lectura estricta y da 0,6%.
Publicar solo la segunda haría ver un colapso donde puede haber una convención
de la operación.

**Lo que esta página NO decide.** Si «Hora de entrega» es un compromiso con el
cliente o una franja de programación interna es una pregunta de negocio, no de
datos, y sigue abierta (DEC-094). La página lo dice arriba de todo en vez de
elegir por su cuenta: con la primera lectura el 9,1% es un indicador de
servicio; con la segunda es una métrica de planeación interna.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from filtros import aplicar, aviso_alcance, barra_lateral

from comun.entregas import (
    A_TIEMPO,
    EN_VENTANA,
    clasificar_por_dia,
    clasificar_por_ventana,
    dias_de_desvio,
    esta_vencido,
    horas_de_atraso,
    parsear_compromiso,
)
from db import get_entregas, get_opciones_comerciales, get_rango_fechas
from theme import (
    BG_DEEP,
    GRAFICO_GRID,
    GRAFICO_SERIES,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

st.markdown('<p class="dp-breadcrumb">Dashboard / Operación</p>', unsafe_allow_html=True)
st.title("🚚 Cumplimiento de entrega")

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
    df = aplicar(get_entregas(_f.desde, _f.hasta), _f)
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info("Sin pedidos en el periodo seleccionado.")
    st.stop()

# ── Clasificación ──────────────────────────────────────────────────────────────
# Se hace acá y no en SQL porque las reglas viven en `comun.entregas`, que es
# puro y testeable. Son ~30.000 filas: el costo es despreciable.
_ahora = pd.Timestamp.now().to_pydatetime()
_compromisos = df["hora_entrega"].map(parsear_compromiso)
# `strict=True` a propósito: las dos series salen del mismo DataFrame, así que
# un desalineamiento sería un bug y no algo que convenga tolerar en silencio.
_pares = list(zip(_compromisos, df["entregado_en"], strict=True))

df["tipo_compromiso"] = [c.tipo if c else "Sin compromiso" for c in _compromisos]
df["por_dia"] = [clasificar_por_dia(c, m) for c, m in _pares]
df["por_ventana"] = [clasificar_por_ventana(c, m) for c, m in _pares]
df["dias_desvio"] = [dias_de_desvio(c, m) for c, m in _pares]
df["horas_atraso"] = [horas_de_atraso(c, m) for c, m in _pares]
df["vencido"] = [esta_vencido(c, m, _ahora) for c, m in _pares]

medibles = df[df["por_dia"].notna()]

st.warning(
    "**Antes de leer estas cifras:** que «Hora de entrega» sea un compromiso con "
    "el cliente o una franja de programación interna sigue sin resolverse — es una "
    "decisión de negocio, no de datos (DEC-094). Si es lo primero, esto es el "
    "indicador de servicio del área. Si es lo segundo, mide la calidad de la "
    "planeación, que igual importa pero no se le promete a nadie."
)

if medibles.empty:
    st.info("Ningún pedido del periodo tiene compromiso fechado y entrega registrada.")
    st.stop()

# ── Los dos indicadores ────────────────────────────────────────────────────────
st.subheader("A tiempo, en dos escalas")

a_tiempo = int((medibles["por_dia"] == A_TIEMPO).sum())
en_ventana = int((medibles["por_ventana"] == EN_VENTANA).sum())
total = len(medibles)
vencidos = df[df["vencido"]]

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "A tiempo — por día",
    f"{100 * a_tiempo / total:.1f}%",
    help=f"{a_tiempo:,} de {total:,} pedidos llegaron el día pactado o antes. "
    "Es la escala estándar del sector.".replace(",", "."),
)
k2.metric(
    "A tiempo — por ventana",
    f"{100 * en_ventana / total:.1f}%",
    help=f"{en_ventana:,} de {total:,} llegaron dentro de la franja horaria. "
    "Lectura estricta.".replace(",", "."),
)
k3.metric(
    "Desvío mediano",
    f"{medibles['dias_desvio'].median():.0f} días",
    help="Días entre la fecha pactada y la de entrega. Positivo = después.",
)
k4.metric(
    "⚠️ Promesas vencidas hoy",
    f"{len(vencidos):,}".replace(",", "."),
    delta="Sin entrega registrada" if len(vencidos) else "Ninguna",
    delta_color="inverse" if len(vencidos) else "normal",
    help="La ventana ya cerró y no hay evento de entrega. Es la cola de hoy.",
)

# ── Evolución ──────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Cómo viene evolucionando")

medibles = medibles.copy()
medibles["mes"] = medibles["fecha"].str.slice(0, 7)
por_mes = (
    medibles.groupby("mes")
    .agg(pedidos=("id_pedido", "count"), a_tiempo=("por_dia", lambda s: (s == A_TIEMPO).sum()))
    .reset_index()
)
por_mes["pct"] = 100 * por_mes["a_tiempo"] / por_mes["pedidos"]

fig = go.Figure()
fig.add_bar(
    x=por_mes["mes"],
    y=por_mes["pct"],
    marker_color=GRAFICO_SERIES[0],
    marker_line={"color": BG_DEEP, "width": 1},
    marker_cornerradius=3,
    hovertemplate="<b>%{x}</b><br>%{y:.1f}% a tiempo<extra></extra>",
)
fig.update_layout(
    title={
        "text": "A tiempo por día, por mes del pedido",
        "font": {"color": TEXT_SECONDARY, "size": 13},
    },
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font={"color": TEXT_SECONDARY, "size": 12},
    margin={"l": 10, "r": 10, "t": 40, "b": 10},
    height=300,
    showlegend=False,
    bargap=0.25,
)
fig.update_xaxes(showgrid=False, tickfont={"color": TEXT_PRIMARY}, automargin=True)
fig.update_yaxes(gridcolor=GRAFICO_GRID, ticksuffix="%", automargin=True)
st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

_primer, _ultimo = por_mes.iloc[0], por_mes.iloc[-1]
st.caption(
    f"De **{_primer['pct']:.1f}%** en {_primer['mes']} a **{_ultimo['pct']:.1f}%** en "
    f"{_ultimo['mes']}. El salto de abril a mayo coincide con el cambio de formato del "
    "campo en el origen (de un instante a una franja horaria), así que parte de la "
    "mejora puede ser que la promesa se volviera más realista y no que la operación "
    "acelerara. Separar las dos cosas necesita una definición de negocio."
)

# ── Por canal: donde está concentrado ──────────────────────────────────────────
st.divider()
st.subheader("Dónde se incumple")

por_canal = (
    medibles.groupby("metodo_entrega")
    .agg(
        pedidos=("id_pedido", "count"),
        a_tiempo=("por_dia", lambda s: (s == A_TIEMPO).sum()),
        desvio_mediano=("dias_desvio", "median"),
        valor=("valor", "sum"),
    )
    .reset_index()
    .sort_values("pedidos", ascending=False)
)
por_canal["pct"] = 100 * por_canal["a_tiempo"] / por_canal["pedidos"]

c1, c2 = st.columns([1, 1])
with c1:
    st.dataframe(
        por_canal[["metodo_entrega", "pedidos", "pct", "desvio_mediano", "valor"]],
        hide_index=True,
        width="stretch",
        column_config={
            "metodo_entrega": st.column_config.TextColumn("Canal"),
            "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
            "pct": st.column_config.NumberColumn("A tiempo", format="%.1f%%"),
            "desvio_mediano": st.column_config.NumberColumn("Desvío mediano (d)", format="%.0f"),
            "valor": st.column_config.NumberColumn("Valor", format="$%.0f"),
        },
    )
with c2:
    peor = por_canal.sort_values("pct").iloc[0]
    st.markdown(
        f"**{peor['metodo_entrega']} cumple el {peor['pct']:.1f}%** de las fechas que "
        f"promete, sobre {int(peor['pedidos']):,} pedidos. ".replace(",", ".")
        + "No es ruido estadístico ni una cola de casos raros: es el comportamiento "
        "normal de ese canal.\n\n"
        "La lectura operativa es que **la fecha se está pactando con el criterio de "
        "la flota propia y aplicándola también a los envíos de tercero**, que tienen "
        "otro tiempo de tránsito. Es un problema de cómo se promete, no "
        "necesariamente de cómo se despacha."
    )

# ── Reprogramaciones ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Reprogramaciones de la fecha pactada")

con_repro = df[df["reprogramaciones"] > 0]
multi = df[df["reprogramaciones"] >= 2]
r1, r2, r3 = st.columns(3)
r1.metric("Pedidos reprogramados", f"{len(con_repro):,}".replace(",", "."))
r2.metric(
    "Reprogramados 2+ veces",
    f"{len(multi):,}".replace(",", "."),
    help="Cada reprogramación es una promesa que se le cambió al cliente.",
)
r3.metric(
    "Máximo en un pedido",
    int(df["reprogramaciones"].max()),
)

if not multi.empty and multi["por_dia"].notna().any():
    _m = multi[multi["por_dia"].notna()]
    _u = medibles[medibles["reprogramaciones"] <= 1]
    if not _m.empty and not _u.empty:
        st.caption(
            f"Los pedidos reprogramados dos o más veces llegan a tiempo el "
            f"**{100 * (_m['por_dia'] == A_TIEMPO).mean():.1f}%** de las veces, contra el "
            f"**{100 * (_u['por_dia'] == A_TIEMPO).mean():.1f}%** de los demás. "
            "Reprogramar mueve la meta, así que un cumplimiento **más alto** en este "
            "grupo no significa mejor servicio: significa que la promesa se ajustó "
            "hasta poder cumplirla."
        )

# ── Lo accionable: promesas vencidas ───────────────────────────────────────────
st.divider()
st.subheader("⚠️ Promesas vencidas sin entrega registrada")

if vencidos.empty:
    st.success("No hay promesas de entrega vencidas sin registrar.")
else:
    st.markdown(
        f"**{len(vencidos)} pedidos** tienen la ventana de entrega cerrada y ningún "
        "evento de entrega. Es la única sección de esta página que mira adelante: "
        "todo lo demás es historia."
    )
    vista = vencidos.copy()
    vista["dias_vencida"] = (
        pd.Timestamp.now().normalize()
        - pd.to_datetime(vista["hora_entrega"].str.slice(0, 10), errors="coerce")
    ).dt.days
    vista = vista.sort_values("dias_vencida", ascending=False)

    st.dataframe(
        vista[
            [
                "id_pedido",
                "fecha",
                "nombre_empresa",
                "metodo_entrega",
                "hora_entrega",
                "dias_vencida",
                "reprogramaciones",
                "valor",
            ]
        ],
        hide_index=True,
        width="stretch",
        height=380,
        column_config={
            "id_pedido": st.column_config.TextColumn("Pedido"),
            "fecha": st.column_config.TextColumn("Fecha pedido"),
            "nombre_empresa": st.column_config.TextColumn("Cliente", width="medium"),
            "metodo_entrega": st.column_config.TextColumn("Canal"),
            "hora_entrega": st.column_config.TextColumn("Comprometido"),
            "dias_vencida": st.column_config.NumberColumn("Días vencida", format="%.0f"),
            "reprogramaciones": st.column_config.NumberColumn("Reprog.", format="%d"),
            "valor": st.column_config.NumberColumn("Valor", format="$%.0f"),
        },
    )
    st.download_button(
        "⬇️ Descargar promesas vencidas (CSV)",
        vista.to_csv(index=False).encode("utf-8-sig"),
        file_name="entregas_vencidas.csv",
        mime="text/csv",
    )
    st.caption(
        f"Ninguna supera los 30 días: la más vieja lleva "
        f"{int(vista['dias_vencida'].max())}. Eso descarta que sean entregas hechas y "
        "nunca registradas —esas se acumularían sin techo— y confirma que son pedidos "
        "genuinamente abiertos."
    )

# ── Cobertura del dato ─────────────────────────────────────────────────────────
st.divider()
with st.expander("Sobre qué se está midiendo — cobertura y formatos"):
    cob = (
        df.groupby("tipo_compromiso")
        .size()
        .rename("pedidos")
        .reset_index()
        .sort_values("pedidos", ascending=False)
    )
    st.dataframe(cob, hide_index=True, width="stretch")
    st.markdown(
        "El campo tiene **tres formatos** y el origen los fue cambiando:\n\n"
        "- `punto` — un instante (`2026-03-05 14:00`), de febrero a mayo.\n"
        "- `franja` — una ventana (`2026-08-01 08:00 ~ 09:00`), de mayo en adelante.\n"
        "- `Cualquier hora` — el origen dice explícitamente que **no hay hora "
        "comprometida**. Queda fuera de las dos escalas: contarlo como incumplido "
        "inventaría una promesa que nadie hizo.\n\n"
        "Que el desvío contra la entrega real sea prácticamente el mismo antes y "
        "después del cambio de formato es lo que descarta que estas cifras sean un "
        "artefacto de parseo: el origen cambió cómo lo escribe y la distancia no se "
        "movió."
    )
    sin_dato = int((df["tipo_compromiso"] == "Sin compromiso").sum())
    st.caption(
        f"{sin_dato:,} pedidos del periodo no tienen compromiso registrado. ".replace(",", ".")
        + "La cobertura del campo cae en los pedidos viejos porque el origen deja de "
        "renderizar la tarjeta de entrega pasados ~6 meses (DEC-091), no porque no se "
        "haya capturado."
    )
