"""
Consolidado de pedidos — una fila por línea de pedido (DEC-045).

Reemplaza la vista operacional anterior (KPIs, tabla de activos y
drill-downs), que queda recuperable en el historial de git.

La columna "ID del producto" viene de `catalogo_productos`, el puente
`(referencia, código de barras)` → ID que arma `inventario/persistencia.py`
en cada corrida del scheduler: `lineas_pedido` no guarda ningún ID de
producto.
"""

import sqlite3
from datetime import date, datetime, timedelta

import streamlit as st

from db import (
    get_opciones_filtro,
    get_pedidos_consolidado,
    get_rango_fechas,
    get_ultima_actualizacion,
)

# Tope de filas traídas a la tabla. El universo completo son ~840.000
# líneas: volcarlas todas colgaría el navegador sin aportar nada, porque
# nadie lee 840.000 filas — se exploran filtrando. El total real siempre
# se informa, así que el recorte nunca es silencioso.
LIMITE_FILAS = 50_000
# 7 días ≈ 34.000 líneas: entra cómodo bajo el tope, así la primera carga
# no arranca ya recortada. 30 días serían ~148.000.
DIAS_POR_DEFECTO = 7

st.markdown('<p class="dp-breadcrumb">Dashboard / Pedidos</p>', unsafe_allow_html=True)
st.title("📦 Consolidado de pedidos")

# ── Frescura y rango disponible ────────────────────────────────────────────────
try:
    _ultima = get_ultima_actualizacion()
    _min_fecha, _max_fecha = get_rango_fechas()
    _, _, opciones_almacen, _ = get_opciones_filtro()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()
except sqlite3.OperationalError as e:
    # AUD-M6 (defensa secundaria): contención normal con el ETL/scraper
    # escribiendo, con mensaje claro en vez de un traceback sin contexto.
    st.error(
        f"La base de datos está ocupada momentáneamente ({e}). Recarga la página en unos segundos."
    )
    st.stop()

if not _min_fecha:
    st.info("Todavía no hay pedidos en la base de datos.")
    st.stop()

if _ultima:
    try:
        _ultima_fmt = datetime.fromisoformat(_ultima).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        _ultima_fmt = _ultima
    st.caption(f"📅 Datos al: {_ultima_fmt}")

_min_d = date.fromisoformat(_min_fecha)
_max_d = date.fromisoformat(_max_fecha)
# Default acotado a los últimos 30 días: abrir con el histórico completo
# haría que la primera carga trajera cientos de miles de filas.
_ini_d = max(_min_d, _max_d - timedelta(days=DIAS_POR_DEFECTO))

# ── Filtros ────────────────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

with col1:
    rango = st.date_input(
        "Rango de fechas",
        value=(_ini_d, _max_d),
        min_value=_min_d,
        max_value=_max_d,
        format="YYYY-MM-DD",
        help="Filtra por la fecha del pedido padre.",
    )
with col2:
    estado_pedido_sel = st.selectbox(
        "Estado del pedido",
        ["Todos", "Abiertos", "Cerrados"],
        index=0,
        help="Un pedido está abierto si al menos uno de sus subpedidos sigue activo.",
    )
with col3:
    almacenes_sel = st.multiselect(
        "Almacén",
        opciones_almacen,
        placeholder="Todos...",
        help="Almacén de origen de la línea de pedido.",
    )
with col4:
    st.write("")  # alinea el checkbox con la base de los otros controles
    solo_previos = st.checkbox(
        "Solo previos a picking",
        value=False,
        help=(
            "Deja únicamente los subpedidos cuyo alistamiento físico todavía no "
            "terminó. Misma definición que usa el cruce de inventario."
        ),
    )

# st.date_input devuelve una tupla incompleta mientras el usuario está
# eligiendo el segundo extremo del rango.
if not isinstance(rango, (tuple, list)) or len(rango) != 2:
    st.info("Elegí la fecha final del rango para ver los resultados.")
    st.stop()

fecha_desde, fecha_hasta = (d.isoformat() for d in rango)
almacenes_key = tuple(sorted(almacenes_sel))

# ── Datos ──────────────────────────────────────────────────────────────────────
try:
    with st.spinner("Cargando consolidado..."):
        df, agg = get_pedidos_consolidado(
            fecha_desde,
            fecha_hasta,
            estado_pedido_sel,
            almacenes_key,
            solo_previos,
            LIMITE_FILAS,
        )
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()
except sqlite3.OperationalError as e:
    st.error(
        f"La base de datos está ocupada momentáneamente ({e}). Recarga la página en unos segundos."
    )
    st.stop()

if df.empty:
    st.info("Sin líneas de pedido para los filtros seleccionados.")
    st.stop()

# ── Resumen ────────────────────────────────────────────────────────────────────
# Todas las métricas salen de los agregados calculados en SQL sobre el
# conjunto completo, no del DataFrame recortado: si no, dirían "de las
# primeras 50.000 filas" mientras parecen del rango elegido.
k1, k2, k3, k4 = st.columns(4)
k1.metric("Líneas", f"{agg['lineas']:,}")
k2.metric("Pedidos", f"{agg['pedidos']:,}")
k3.metric("Referencias", f"{agg['referencias']:,}")
k4.metric("Cantidad comprada", f"{agg['cantidad']:,.0f}")

if agg["sin_id"]:
    # No es un fallo: el catálogo no cubre códigos legados ni los pares
    # ambiguos que se excluyen a propósito para no duplicar líneas.
    pct = agg["sin_id"] / agg["lineas"] * 100
    st.caption(
        f"⚠️ {agg['sin_id']:,} de {agg['lineas']:,} líneas ({pct:.1f}%) no tienen ID "
        "de producto: no están en el catálogo administrativo vigente, o su par "
        "referencia/código de barras apunta a más de un ID."
    )

if agg["lineas"] > len(df):
    st.warning(
        f"Mostrando las primeras {len(df):,} de {agg['lineas']:,} líneas. "
        "Acotá el rango de fechas o filtrá por almacén para ver el resto. "
        "Los totales de arriba sí corresponden al filtro completo."
    )

st.dataframe(
    df,
    hide_index=True,
    column_config={
        "Cantidad comprada": st.column_config.NumberColumn(format="%g"),
        "Pedido padre": st.column_config.TextColumn(width="medium"),
        "ID del producto": st.column_config.TextColumn(width="medium"),
    },
)
