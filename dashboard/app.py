import streamlit as st

from dashboard.db import (
    _VIEW_CONSOLIDADO_EXISTS,
    get_consolidado,
    get_detalle_operacional,
    get_detalle_pedido,
    get_opciones_filtro,
    get_pedidos,
    get_pedidos_activos,
)

st.set_page_config(
    page_title="Dashboard · Pedidos",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("📦 Dashboard de Pedidos")

# ── Filtros ────────────────────────────────────────────────────────────────────
try:
    opciones_pedido, opciones_sub, opciones_almacen, opciones_tipo = get_opciones_filtro()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

col1, col2, col3, col4 = st.columns(4)
with col1:
    estado_pedido_sel = st.selectbox(
        "Estado del pedido",
        opciones_pedido,
        index=1,
        help="Clasifica según si el pedido tiene al menos un subpedido abierto.",
    )
with col2:
    estados_sub_sel = st.multiselect(
        "Estado del subpedido",
        opciones_sub,
        placeholder="Todos los estados...",
        help="Filtra los subpedidos por su estado específico.",
    )
with col3:
    almacenes_sel = st.multiselect(
        "Almacén",
        opciones_almacen,
        placeholder="Todos los almacenes...",
        help="Filtra por almacén de origen de las líneas de pedido.",
    )
with col4:
    tipos_sub_sel = st.multiselect(
        "Tipo de subpedido",
        opciones_tipo,
        placeholder="Todos los tipos...",
        help="Filtra por tipo de subpedido (Accesorios, Alimentos, Arenas).",
    )

# Conversión a tuplas ordenadas para caché
estados_sub_key = tuple(sorted(estados_sub_sel))
almacenes_key   = tuple(sorted(almacenes_sel))
tipos_sub_key   = tuple(sorted(tipos_sub_sel))

# ── Carga de datos ─────────────────────────────────────────────────────────────
with st.spinner("Cargando datos..."):
    df_consolidado    = get_consolidado(estados_sub_key, almacenes_key)
    df_pedidos        = get_pedidos(estado_pedido_sel, estados_sub_key, almacenes_key)
    df_pedidos_activos = get_pedidos_activos(estados_sub_key, almacenes_key, tipos_sub_key)

# ── KPIs ───────────────────────────────────────────────────────────────────────
st.divider()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Pedidos", f"{len(df_pedidos):,}")
k2.metric("SKUs únicos", f"{df_consolidado['Producto'].nunique():,}")
k3.metric("Comprometido", f"{int(df_consolidado['Comprometido'].sum()):,}")
k4.metric("Pendiente", f"{int(df_consolidado['Pendiente'].sum()):,}")

# ── Consolidado de mercancía ───────────────────────────────────────────────────
st.divider()
st.subheader("Consolidado de mercancía comprometida")

if df_consolidado.empty and not _VIEW_CONSOLIDADO_EXISTS:
    st.warning("La VIEW v_inventario_comprometido no existe. Ejecuta el ETL primero.")
elif df_consolidado.empty:
    st.info("Sin mercancía comprometida para el filtro seleccionado.")
else:
    st.caption(
        f"{len(df_consolidado):,} líneas · "
        f"{df_consolidado['Producto'].nunique():,} productos únicos · "
        "solo subpedidos activos"
    )
    st.dataframe(
        df_consolidado,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Comprometido":      st.column_config.NumberColumn(format="%g"),
            "Entregado":         st.column_config.NumberColumn(format="%g"),
            "Pendiente":         st.column_config.NumberColumn(format="%g"),
            "Pedidos con stock": st.column_config.NumberColumn(format="%d"),
        },
    )

# ── Vista operacional ──────────────────────────────────────────────────────────
st.divider()
st.subheader("Vista operacional — Pedidos activos")

if df_pedidos_activos.empty:
    st.info("Sin pedidos activos para el filtro seleccionado.")
else:
    dias_abierto_vals = df_pedidos_activos["Días abierto"].dropna()
    n_con_dif    = int((df_pedidos_activos["⚠ Dif."] == "Sí").sum())
    # fillna(False) para que NaN (actualizado_en NULL) no se cuente como estancado
    n_estancados = int(
        (df_pedidos_activos["Días sin mov."].fillna(0) >= 2).sum()
    )

    op1, op2, op3, op4 = st.columns(4)
    op1.metric("Pedidos activos",    f"{len(df_pedidos_activos):,}")
    op2.metric("Con diferencia",     f"{n_con_dif:,}")
    op3.metric("Promedio días abierto",
               f"{dias_abierto_vals.mean():.1f}" if not dias_abierto_vals.empty else "—")
    op4.metric("Sin mov. ≥ 2 días",  f"{n_estancados:,}",
               help="Pedidos cuya última actualización fue hace 2 o más días.")

    st.caption(
        f"{len(df_pedidos_activos):,} pedidos activos · "
        "ordenados por antigüedad DESC · "
        "haz clic en una fila para ver el detalle"
    )

    evento_op = st.dataframe(
        df_pedidos_activos,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="tabla_operacional",
        column_config={
            "⚠ Dif.":       st.column_config.TextColumn(width="small"),
            "Días abierto": st.column_config.NumberColumn(format="%d días", width="small"),
            "Días sin mov.":st.column_config.NumberColumn(format="%d días", width="small"),
            "Subpedidos":   st.column_config.NumberColumn(format="%d",      width="small"),
            "Sub. abiertos":st.column_config.NumberColumn(format="%d",      width="small"),
        },
    )

    # ── Drill-down operacional ─────────────────────────────────────────────────
    filas_op = evento_op.selection.rows
    if filas_op:
        fila_op   = df_pedidos_activos.iloc[filas_op[0]]
        id_op     = str(fila_op["ID Pedido"])

        st.divider()
        izq_op, der_op = st.columns([3, 1])
        with izq_op:
            st.subheader(f"Detalle operacional — Pedido {id_op}")
        with der_op:
            if fila_op["⚠ Dif."] == "Sí":
                st.warning("⚠ Este pedido tiene diferencias.")

        m1, m2, m3, m4 = st.columns(4)
        m1.markdown(f"**Cliente:** {fila_op['Cliente']}")
        m2.markdown(f"**Vendedor:** {fila_op['Vendedor']}")
        m3.markdown(f"**Fecha:** {fila_op['Fecha creación']}")
        m4.markdown(f"**Forma de pago:** {fila_op['Forma de pago']}")

        t1, t2 = st.columns(2)
        dias_ab  = fila_op['Días abierto']
        dias_sin = fila_op['Días sin mov.']
        t1.markdown(f"**Días abierto:** {int(dias_ab) if dias_ab == dias_ab else '—'} días")
        t2.markdown(f"**Días sin movimiento:** {int(dias_sin) if dias_sin == dias_sin else '—'} días")

        df_det_op = get_detalle_operacional(
            id_op, estados_sub_key, almacenes_key, tipos_sub_key
        )

        if df_det_op.empty:
            st.info(
                "Sin líneas para este pedido con el filtro actual. "
                "Prueba ampliando los filtros."
            )
        else:
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Subpedidos visibles", df_det_op["Subpedido"].nunique())
            d2.metric("SKUs",                df_det_op["Producto"].nunique())
            d3.metric("Comprometido",        f"{int(df_det_op['Comprometido'].sum()):,}")
            d4.metric("Pendiente",           f"{int(df_det_op['Pendiente'].sum()):,}")

            st.dataframe(
                df_det_op,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Comprometido":  st.column_config.NumberColumn(format="%g"),
                    "Entregado":     st.column_config.NumberColumn(format="%g"),
                    "Pendiente":     st.column_config.NumberColumn(format="%g"),
                    "Monto a pagar": st.column_config.NumberColumn(format="$ {:,.0f}"),
                    "Monto final":   st.column_config.NumberColumn(format="$ {:,.0f}"),
                },
            )

# ── Tabla de pedidos ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Pedidos")
st.caption(f"{len(df_pedidos):,} pedidos · haz clic en una fila para ver el detalle.")

if df_pedidos.empty:
    st.info("Sin pedidos para el filtro seleccionado.")
    st.stop()

evento = st.dataframe(
    df_pedidos,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="tabla_pedidos",
    column_config={
        "⚠ Diferencia": st.column_config.TextColumn(width="small"),
        "Subpedidos":   st.column_config.NumberColumn(format="%d", width="small"),
    },
)

# ── Drill-down ─────────────────────────────────────────────────────────────────
filas_sel = evento.selection.rows
if not filas_sel:
    st.stop()

fila = df_pedidos.iloc[filas_sel[0]]
id_pedido = str(fila["ID Pedido"])

st.divider()
izq, der = st.columns([3, 1])
with izq:
    st.subheader(f"Detalle — Pedido {id_pedido}")
with der:
    if fila["⚠ Diferencia"] == "Sí":
        st.warning("⚠ Este pedido tiene diferencias.")

d1, d2, d3, d4 = st.columns(4)
d1.markdown(f"**Cliente:** {fila['Cliente']}")
d2.markdown(f"**Vendedor:** {fila['Vendedor']}")
d3.markdown(f"**Fecha:** {fila['Fecha']}")
d4.markdown(f"**Forma de pago:** {fila['Forma de pago']}")

df_detalle = get_detalle_pedido(id_pedido, estados_sub_key, almacenes_key)

if df_detalle.empty:
    st.info(
        "Sin líneas para este pedido con el filtro actual. "
        "Prueba ampliando los filtros de subpedido o almacén."
    )
    st.stop()

e1, e2, e3, e4 = st.columns(4)
e1.metric("Subpedidos visibles", df_detalle["Subpedido"].nunique())
e2.metric("SKUs", df_detalle["Producto"].nunique())
e3.metric("Comprometido", f"{int(df_detalle['Comprometido'].sum()):,}")
e4.metric("Pendiente", f"{int(df_detalle['Pendiente'].sum()):,}")

st.dataframe(
    df_detalle,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Comprometido":  st.column_config.NumberColumn(format="%g"),
        "Entregado":     st.column_config.NumberColumn(format="%g"),
        "Pendiente":     st.column_config.NumberColumn(format="%g"),
        "Monto a pagar": st.column_config.NumberColumn(format="$ {:,.0f}"),
        "Monto final":   st.column_config.NumberColumn(format="$ {:,.0f}"),
    },
)