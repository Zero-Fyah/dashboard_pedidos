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

from db import get_arena_balance, get_arena_inventario

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

MODALIDADES_NUCLEO = ["Unidades", "Tonelada", "Corporativo"]
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
