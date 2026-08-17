"""
Arena — inventario por código de barras, modalidad y ciudad (DEC-118).

**Núcleo del módulo analítico de Arenas.** El código de barras identifica el
producto físico (aroma/peso), no la modalidad — un mismo barcode aparece
bajo varias referencias comerciales según dónde y cómo se vende, así que la
tabla principal cruza las dos cosas: código de barras × modalidad, con la
ciudad como filtro (no como columna).

Respaldo (reserva para averías) y el hub de Yumbo **no son modalidad de
venta** — van aparte para no mezclar conceptos distintos en la misma tabla.

El balance Unidades/Tonelada/Corporativo que se muestra es **medido sobre
ventas históricas reales**, no la hipótesis 30/70 del plan original — ver
`db.get_arena_balance()`.
"""

from __future__ import annotations

import sqlite3

import streamlit as st

from comun import ARENA_MODALIDADES_NUCLEO
from comun.arena import alertas_quiebre_arena, demanda_arena_por_ciudad
from comun.reposicion import ESTADO_PEDIR_PRONTO, ESTADO_PEDIR_YA
from db import get_arena_balance, get_arena_inventario, get_arena_movimiento

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("🏖️ Arena — inventario por código de barras")

try:
    inv = get_arena_inventario()
    balance = get_arena_balance()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if inv.empty:
    st.info(
        "Todavía no hay una corrida con inventario de Arena. Se genera en cada "
        "ciclo del scheduler, o se fuerza con `python -m inventario.persistencia`."
    )
    st.stop()

MODALIDADES_NUCLEO = list(ARENA_MODALIDADES_NUCLEO)
MODALIDADES_APARTE = ["Respaldo", "Yumbo (hub)"]

# ── Filtro de ciudad ─────────────────────────────────────────────────────
ciudades = sorted(inv["almacen"].dropna().unique().tolist())
ciudad_sel = st.multiselect("Ciudad", ciudades, default=ciudades, help="Filtra por `almacen`.")
vista = inv[inv["almacen"].isin(ciudad_sel)] if ciudad_sel else inv.iloc[0:0]

if vista.empty:
    st.info("Selecciona al menos una ciudad.")
    st.stop()

# ── Balance medido (no asumido) ─────────────────────────────────────────
st.subheader("Balance de bolsas vendidas, medido")
if balance.empty:
    st.caption("Sin ventas históricas de Arena para medir el balance todavía.")
else:
    total = balance["bolsas"].sum()
    cobertura = float(balance["cobertura"].iloc[0])
    cols = st.columns(len(balance))
    for col, (_, fila) in zip(
        cols, balance.sort_values("bolsas", ascending=False).iterrows(), strict=False
    ):
        col.metric(fila["modalidad"], f"{fila['bolsas'] / total * 100:.1f}%")
    total_fmt = f"{total:,.0f}".replace(",", ".")
    st.caption(
        f"Sobre {total_fmt} bolsas vendidas en todo el histórico "
        f"({cobertura * 100:.0f}% de las líneas de Arena reconocidas por el "
        "clasificador de modalidad). Es lo que muestran las ventas reales, no la "
        "hipótesis de 30/70 Unidades/Tonelada — la proporción puede variar por "
        "ciudad o periodo, no se investigó ese detalle todavía."
    )

st.divider()

# ── Tabla núcleo: código de barras × modalidad ──────────────────────────
st.subheader("Inventario por código de barras")

nucleo = vista[vista["modalidad"].isin(MODALIDADES_NUCLEO)]
if nucleo.empty:
    st.info("Sin inventario de Unidades/Tonelada/Corporativo en la ciudad seleccionada.")
else:
    tabla = nucleo.pivot_table(
        index=["codigo_barras", "especificacion"],
        columns="modalidad",
        values="inventario",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    for m in MODALIDADES_NUCLEO:
        if m not in tabla.columns:
            tabla[m] = 0
    tabla = tabla[["codigo_barras", "especificacion", *MODALIDADES_NUCLEO]]
    tabla.columns = [
        "Código de barras",
        "Especificación",
        "Cant. Unidad",
        "Cant. Tonelada",
        "Cant. Corporativo",
    ]

    busqueda = st.text_input("Buscar por código de barras o especificación", "")
    if busqueda:
        mascara = tabla["Código de barras"].astype(str).str.contains(
            busqueda, case=False, na=False
        ) | tabla["Especificación"].astype(str).str.contains(busqueda, case=False, na=False)
        tabla = tabla[mascara]

    st.dataframe(
        tabla,
        hide_index=True,
        width="stretch",
        height=460,
        column_config={
            "Cant. Unidad": st.column_config.NumberColumn(format="%.0f"),
            "Cant. Tonelada": st.column_config.NumberColumn(format="%.0f"),
            "Cant. Corporativo": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.caption(f"{len(tabla):,} código(s) de barras.".replace(",", "."))
    st.download_button(
        "⬇️ Descargar (CSV)",
        tabla.to_csv(index=False).encode("utf-8-sig"),
        file_name="arena_inventario.csv",
        mime="text/csv",
    )

# ── Alertas de quiebre — cuándo se agota cada código de barras por ciudad ──
st.divider()
st.subheader("Alertas de quiebre — cuándo se agota cada código de barras por ciudad")
st.markdown(
    "La tabla de arriba dice **cuánto hay hoy**. Esto dice **cuándo se agota** "
    "en cada ciudad, al ritmo de venta de los últimos 90 días."
)

p1, p2 = st.columns(2)
lead_time_arena = p1.number_input(
    "Plazo de reposición/traslado (días)",
    min_value=1,
    max_value=365,
    value=60,
    step=5,
    help="Desde que se pide hasta que llega a la ciudad. **No sale de los "
    "datos** — Arena se recibe por peso y no hay tabla de compras ni de "
    "traslados en el sistema (mismo límite que DEC-071 documenta para el "
    "catálogo general). Poné el plazo real.",
)
objetivo_arena = p2.number_input(
    "Cobertura objetivo (días)",
    min_value=0,
    max_value=365,
    value=30,
    step=5,
    help="Días de venta que se quiere tener encima al recibir el traslado.",
)

movimiento = get_arena_movimiento()
demanda_arena = demanda_arena_por_ciudad(movimiento)
alertas = alertas_quiebre_arena(
    inv,
    demanda_arena,
    lead_time_dias=int(lead_time_arena),
    dias_cobertura_objetivo=int(objetivo_arena),
)
alertas_vista = alertas[alertas["almacen"].isin(ciudad_sel)] if not alertas.empty else alertas

if alertas_vista.empty:
    st.info(
        "Sin demanda medida en los últimos 90 días para la ciudad "
        "seleccionada, o todavía no hay ventas de Arena en la base."
    )
else:
    ya = alertas_vista[alertas_vista["estado_reposicion"] == ESTADO_PEDIR_YA]
    pronto = alertas_vista[alertas_vista["estado_reposicion"] == ESTADO_PEDIR_PRONTO]

    a1, a2, a3 = st.columns(3)
    a1.metric(
        "Pedir ya",
        f"{len(ya):,}",
        help="Sin stock y con demanda: ya se está perdiendo venta en esa ciudad.",
    )
    a2.metric(
        "Bajo el punto de reorden",
        f"{len(pronto):,}",
        help="Todavía hay stock, pero no alcanza para cubrir el plazo de traslado.",
    )
    a3.metric(
        "Códigos con demanda",
        f"{len(alertas_vista):,}",
        help="Combinaciones código de barras × ciudad con venta en los últimos 90 días.",
    )

    st.dataframe(
        alertas_vista[
            [
                "almacen",
                "codigo_barras",
                "especificacion",
                "demanda_diaria",
                "disponible",
                "cobertura_dias",
                "fecha_quiebre_proyectada",
                "punto_reorden",
                "estado_reposicion",
            ]
        ],
        hide_index=True,
        width="stretch",
        height=380,
        column_config={
            "almacen": st.column_config.TextColumn("Ciudad", width="small"),
            "codigo_barras": st.column_config.TextColumn("Código de barras", width="medium"),
            "especificacion": st.column_config.TextColumn("Especificación", width="medium"),
            "demanda_diaria": st.column_config.NumberColumn("Demanda/día", format="%.1f"),
            "disponible": st.column_config.NumberColumn("Disponible", format="%.0f"),
            "cobertura_dias": st.column_config.NumberColumn("Días cobertura", format="%.0f"),
            "fecha_quiebre_proyectada": st.column_config.DateColumn(
                "Fecha de quiebre proyectada", format="YYYY-MM-DD"
            ),
            "punto_reorden": st.column_config.NumberColumn(
                "Punto de reorden",
                format="%.0f",
                help="Cuando el disponible baja de acá, hay que pedir el traslado.",
            ),
            "estado_reposicion": st.column_config.TextColumn("Estado", width="medium"),
        },
    )
    st.download_button(
        "⬇️ Descargar alertas (CSV)",
        alertas_vista.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"arena_quiebre_lt{int(lead_time_arena)}d.csv",
        mime="text/csv",
    )

    st.caption(
        f"⚠️ **Todo este bloque depende del plazo de {int(lead_time_arena)} días "
        "que pusiste arriba.** Es el único número acá que no sale de los datos: "
        "la demanda diaria sale del histórico real de venta (90 días), la fecha "
        "de quiebre es proyección lineal sobre ese ritmo — no anticipa "
        "estacionalidad ni promociones. El disponible es la foto de la corrida "
        "más reciente, no un histórico — `arena_inventario` guarda una sola "
        "corrida a la vez (DEC-119)."
    )

# ── Respaldo y hub de Yumbo — aparte, no son modalidad de venta ────────
aparte = vista[vista["modalidad"].isin(MODALIDADES_APARTE)]
if not aparte.empty:
    st.divider()
    st.subheader("Respaldo y hub de Yumbo")
    st.caption(
        "No son modalidad de venta: Respaldo es reserva para averías y "
        "situaciones extraordinarias; el hub de Yumbo es acopio/tránsito "
        "nacional, no un SKU que un cliente compre directo."
    )
    tabla_aparte = (
        aparte[["codigo_barras", "especificacion", "modalidad", "almacen", "inventario"]]
        .rename(
            columns={
                "codigo_barras": "Código de barras",
                "especificacion": "Especificación",
                "modalidad": "Grupo",
                "almacen": "Ciudad",
                "inventario": "Inventario",
            }
        )
        .sort_values(["Grupo", "Ciudad"])
    )
    st.dataframe(
        tabla_aparte,
        hide_index=True,
        width="stretch",
        column_config={"Inventario": st.column_config.NumberColumn(format="%.0f")},
    )
