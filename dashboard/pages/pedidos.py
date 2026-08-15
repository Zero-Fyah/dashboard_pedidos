"""
Consolidado de pedidos — una fila por línea de pedido (DEC-045).

Reemplaza la vista operacional anterior (KPIs, tabla de activos y
drill-downs), que queda recuperable en el historial de git.

La columna "ID de especificación" viene de `catalogo_productos`, el puente
`(referencia, código de barras)` → ID que arma `inventario/persistencia.py`
en cada corrida del scheduler: `lineas_pedido` no guarda ningún ID de
producto. DEC-111: reemplaza a "ID del producto" — `id_especificacion` es
el identificador único de cada producto (variantes de color/lote incluidas),
`id_producto` es más agregado. Tiene más líneas vacías que antes porque es
un dato más fino y por lo tanto más ambiguo por par: medido 2026-08-12,
9,29% de los pares del admin no resuelven a un único `id_especificacion`
contra 0,66% que no resolvían a un único `id_producto`.
"""

import sqlite3

import pandas as pd
import streamlit as st
from filtros import aviso_alcance, barra_lateral, recortar
from tz import a_hora_colombia

from db import (
    get_comprometido,
    get_opciones_comerciales,
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
# Tope de días que esta página consulta, por encima del filtro global (DEC-101).
# Medido: 7 días = 4,4 s · histórico completo = 42,7 s sobre 875.059 líneas.
# 31 días es el punto donde sigue siendo usable sin volverse un exportador lento.
DIAS_MAXIMOS = 31
# DEC-112: ventana inicial de esta página — antes arrancaba con todo el
# histórico (dias_por_defecto=None), la más lenta de las dos consultas de
# arriba. Solo aplica "la primera vez de la sesión" (docstring de
# barra_lateral): si el usuario visita otra página con filtro de fecha antes
# de entrar acá, el rango compartido ya quedó sembrado con el default de esa
# otra página y este valor no vuelve a aplicarse.
DIAS_INICIALES = 15

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
    st.caption(f"📅 Datos al: {a_hora_colombia(_ultima)} (UTC-5)")

# ── Filtros ────────────────────────────────────────────────────────────────────
# DEC-101: el rango sale del filtro global; los controles propios de esta página
# son los que ninguna otra usa (almacén y el subfiltro de picking).
_f = barra_lateral(
    _min_fecha, _max_fecha, get_opciones_comerciales(), dias_por_defecto=DIAS_INICIALES
)
# Esta es la única página que consulta a nivel de línea sobre las 875.059 de
# `lineas_pedido`: medido, 7 días cuestan 4,4 s y el histórico completo 42,7 s.
# El recorte es local — el filtro global del sidebar queda intacto para el resto.
_f_local, _recortado = recortar(_f, DIAS_MAXIMOS)
aviso_alcance(_f_local, aplica_dimensiones=False)
fecha_desde, fecha_hasta = _f_local.desde, _f_local.hasta

if _recortado:
    st.warning(
        f"El filtro global cubre **{_f.desde} → {_f.hasta}**, pero esta página consulta "
        f"línea por línea sobre 875.000 registros y se acota a los últimos "
        f"**{DIAS_MAXIMOS} días** ({fecha_desde} → {fecha_hasta}). El histórico completo "
        "tarda 42 s contra 4 s de una semana. **El resto de las páginas sigue usando el "
        "rango que elegiste** — el recorte es solo acá."
    )

col2, col3, col4 = st.columns(3)
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
            "Deja únicamente los subpedidos comprometidos y aún sin alistar: "
            "Pendiente de pago (pago inmediato), Pendiente de recolección o "
            "Aprobación de pagos, con alistamiento físico sin terminar (DEC-109)."
        ),
    )

st.info(
    "El consolidado agrega en SQL sobre el conjunto completo, así que **los filtros "
    "de vendedor, cliente y canal no lo alcanzan**: aplicarlos en pandas dejaría los "
    "totales de arriba hablando de un universo y la tabla de otro. Para cortar por "
    "esas dimensiones están las páginas de Ventas, Ciclo de vida y Entregas.",
    icon="ℹ️",
)

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
        "de especificación: no están en el catálogo administrativo vigente, o su par "
        "referencia/código de barras apunta a más de un ID de especificación (DEC-111)."
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
        "ID de especificación": st.column_config.TextColumn(width="medium"),
    },
)

# ── Inventario comprometido ────────────────────────────────────────────────────
# DEC-098: la "Vista de inventario comprometido" que `integral.md` pide desde el
# primer día. No depende del rango de fechas de arriba: es el estado vivo de hoy,
# igual que los impagos de DEC-085.
st.divider()
st.subheader("Mercancía comprometida en pedidos abiertos")

try:
    comp = get_comprometido()
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}).")
    comp = pd.DataFrame()

if comp.empty:
    st.info("Sin pedidos abiertos con mercancía comprometida.")
else:
    con_stock = comp[comp["disponible"].notna()]
    sin_cubrir = con_stock[con_stock["faltante"] > 0]
    cubierto = con_stock["comprometido"].sum() - con_stock["faltante"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Unidades comprometidas",
        f"{comp['comprometido'].sum():,.0f}".replace(",", "."),
        help="Cantidad comprada en subpedidos que siguen abiertos.",
    )
    c2.metric("Referencias", f"{len(comp):,}".replace(",", "."))
    c3.metric(
        "Valor comprometido",
        f"${comp['valor'].sum() / 1e6:,.0f} M".replace(",", "."),
    )
    c4.metric(
        "Cobertura con stock",
        f"{100 * cubierto / con_stock['comprometido'].sum():.1f}%"
        if con_stock["comprometido"].sum()
        else "—",
        delta=f"{len(sin_cubrir)} refs sin cubrir" if len(sin_cubrir) else "Todo cubierto",
        delta_color="inverse" if len(sin_cubrir) else "normal",
    )

    st.warning(
        "**Esta cifra no sale de `v_inventario_comprometido`, y es a propósito.** Esa "
        "VIEW resta `cantidad_entregada` a `cantidad_comprada`, pero el origen puebla "
        "las dos iguales mientras el subpedido está abierto —99,3% de las líneas, con "
        "`cantidades_definitivas = 0` en 2.081 de 2.085 subpedidos activos—. La resta da "
        "**251 unidades** cuando lo realmente comprometido son **497.929**. Acá se usa "
        "`cantidad_comprada`, que es lo que la bodega tiene reservado."
    )

    if not sin_cubrir.empty:
        st.markdown(
            f"**{len(sin_cubrir)} referencias no alcanzan a cubrir lo comprometido**: "
            f"faltan {sin_cubrir['faltante'].sum():,.0f} unidades y hay "
            f"${sin_cubrir['valor'].sum() / 1e6:,.0f} M en pedidos que dependen de ellas. "
            "Es la lista de reposición con urgencia real: no es demanda proyectada, es "
            "mercancía ya vendida.".replace(",", ".")
        )
        vista = sin_cubrir.sort_values("faltante", ascending=False)
        st.dataframe(
            vista[
                [
                    "referencia",
                    "producto",
                    "pedidos",
                    "comprometido",
                    "disponible",
                    "faltante",
                    "estado_salud",
                    "valor",
                ]
            ],
            hide_index=True,
            width="stretch",
            height=340,
            column_config={
                "referencia": st.column_config.TextColumn("Referencia"),
                "producto": st.column_config.TextColumn("Producto", width="medium"),
                "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                "comprometido": st.column_config.NumberColumn("Comprometido", format="%.0f"),
                "disponible": st.column_config.NumberColumn("Disponible", format="%.0f"),
                "faltante": st.column_config.NumberColumn("Faltante", format="%.0f"),
                "estado_salud": st.column_config.TextColumn("Salud"),
                "valor": st.column_config.NumberColumn("Valor en juego", format="$%.0f"),
            },
        )
        st.download_button(
            "⬇️ Descargar referencias sin cubrir (CSV)",
            vista.to_csv(index=False).encode("utf-8-sig"),
            file_name="comprometido_sin_cubrir.csv",
            mime="text/csv",
        )
    else:
        st.success("Todas las referencias comprometidas tienen stock suficiente.")

    _sin_dato = int(comp["disponible"].isna().sum())
    if _sin_dato:
        st.caption(
            f"{_sin_dato} de {len(comp)} referencias comprometidas no tienen dato de stock "
            "en el cruce de inventario, así que no entran en la cobertura. No es lo mismo "
            "que tener cero: es no saber."
        )
