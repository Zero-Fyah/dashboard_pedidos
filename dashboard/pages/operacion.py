"""
Operación — capacidad y tiempos de ciclo del almacén (DEC-054/055).

**Enfoque de equipo, por decisión explícita.** El log de operaciones
registra un solo usuario por cierre, pero el 52,3% de los subpedidos tuvo
varios alistadores (el alistamiento se reparte por familia): el 83,8% de
las líneas se le acredita a quien cerró, no a quien picó. Sumando el equipo
el error se cancela — cada subpedido se cuenta una vez —, pero comparar
personas con esta base sería incorrecto.

Por eso la vista muestra **capacidad y throughput del equipo**, y a nivel
de persona solo **carga de trabajo**, nunca ritmo. El enfoque individual
queda para cuando se registre qué familia tomó cada alistador.

**Cumplimiento de despacho (DEC-065/069):** la cuarta pestaña mide el *fill
rate* —qué proporción de pedidos salió con todo lo pedido—. Es la mitad *in
full* de E.1; la mitad *on time* la construyó DEC-095 y vive en
**Operación → Cumplimiento de entrega**.

**Alcance (DEC-100).** Esta página tenía seis pestañas y 1.447 líneas, y
cuatro de ellas no hablaban de bodega sino de plata: auditoría de pago,
comprobantes, saldo a favor, crédito y cancelaciones por impago. Todo eso se
mudó a **Comercial → Cobranza y cartera**. Un supervisor de almacén y un
analista de cartera no tienen por qué compartir pantalla, y la página cargaba
**14 consultas antes de dibujar la primera pestaña** — la mitad para datos
que ese supervisor nunca mira.
"""

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import (
    get_despacho_diario,
    get_despacho_faltantes,
    get_despacho_lineas_diario,
    get_inventario_abc,
    get_inventario_salud,
    get_operacion_ciclos,
    get_operacion_ventanas,
)
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Operación</p>', unsafe_allow_html=True)
st.title("⏱ Capacidad y tiempos de ciclo")

try:
    ciclos = get_operacion_ciclos()
    ventanas = get_operacion_ventanas()
    despacho = get_despacho_diario()
    # DEC-069: el detalle trae `referencia`, así que se cruza con la clase ABC
    # y con el stock actual.
    _abc = get_inventario_abc()
    _salud = get_inventario_salud()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if ciclos.empty and ventanas.empty:
    st.info(
        "Todavía no hay cálculo de operación. Se genera en cada ciclo del scheduler, "
        "o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

# ── Periodo ────────────────────────────────────────────────────────────────────
dias = sorted(set(ciclos["dia"].dropna()) | set(ventanas["dia"].dropna()))
rango = st.select_slider(
    "Periodo",
    options=dias,
    value=(dias[max(0, len(dias) - 30)], dias[-1]),
    help="Por defecto, los últimos 30 días con actividad.",
)
ciclos_v = ciclos[(ciclos["dia"] >= rango[0]) & (ciclos["dia"] <= rango[1])]
# Solo las jornadas que definen una ventana medible.
ventanas_v = ventanas[
    (ventanas["dia"] >= rango[0]) & (ventanas["dia"] <= rango[1]) & (ventanas["utilizable"] == 1)
]

if ciclos_v.empty and ventanas_v.empty:
    st.info("Sin actividad en el periodo seleccionado.")
    st.stop()

(
    tab_capacidad,
    tab_ciclos,
    tab_carga,
    tab_despacho,
) = st.tabs(
    [
        "Capacidad del equipo",
        "Tiempos de ciclo",
        "Carga por persona",
        "Cumplimiento de despacho",
    ],
    # on_change="rerun" (H1/H2, auditoría de rendimiento 2026-08-10/12): sin
    # esto, Streamlit computa las 4 pestañas en cada rerun aunque solo una
    # esté visible — medido en 0,55 s por rerun con caché caliente.
    on_change="rerun",
)

# ── Capacidad ──────────────────────────────────────────────────────────────────
if tab_capacidad.open:
    with tab_capacidad:
        if ventanas_v.empty:
            st.info("Sin jornadas medibles en el periodo.")
        else:
            por_dia = (
                ventanas_v.groupby("dia", as_index=False)
                .agg(
                    alistadores=("usuario", "nunique"),
                    horas=("ventana_h", "sum"),
                    subpedidos=("subpedidos", "sum"),
                    lineas=("lineas", "sum"),
                    unidades=("unidades", "sum"),
                )
                .sort_values("dia")
            )
            # Ritmo del equipo: líneas por hora-alistador disponible.
            por_dia["lineas_hora"] = por_dia["lineas"] / por_dia["horas"]

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Alistadores por día (mediana)", f"{por_dia['alistadores'].median():.0f}")
            k2.metric(
                "Jornada media",
                f"{ventanas_v['ventana_h'].median():.1f} h",
                help="Del primer al último cierre registrado en el día.",
            )
            k3.metric("Líneas por día (mediana)", f"{por_dia['lineas'].median():,.0f}")
            k4.metric(
                "Líneas por hora-alistador",
                f"{por_dia['lineas_hora'].median():.1f}",
                help="Del equipo completo, no de una persona.",
            )

            # El número se lee del dato, no se escribe en la prosa: con el
            # periodo filtrable, un valor fijo se desincroniza de la métrica de
            # arriba y queda contradiciéndola en pantalla.
            st.caption(
                f"La jornada media de **{ventanas_v['ventana_h'].median():.1f} h** es la "
                "validación de que el método mide algo real: reproduce un turno de trabajo "
                "sin que se le haya dicho cuál es."
            )

            # Dos medidas de escala distinta → dos gráficos, nunca dos ejes.
            g1, g2 = st.columns(2)
            with g1:
                fig = go.Figure()
                fig.add_bar(
                    x=por_dia["dia"],
                    y=por_dia["lineas"],
                    marker_color=GRAFICO_SERIES[0],
                    marker_line=dict(color=BG_DEEP, width=1),
                    marker_cornerradius=3,
                    hovertemplate="<b>%{x}</b><br>%{y:,.0f} líneas<extra></extra>",
                )
                fig.update_layout(
                    title=dict(
                        text="Líneas cerradas por día", font=dict(color=TEXT_SECONDARY, size=13)
                    ),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_SECONDARY, size=12),
                    margin=dict(l=10, r=10, t=40, b=10),
                    height=280,
                    showlegend=False,
                    bargap=0.2,
                )
                fig.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
                fig.update_yaxes(
                    gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True
                )
                st.plotly_chart(fig, config={"displayModeBar": False})

            with g2:
                fig2 = go.Figure()
                fig2.add_scatter(
                    x=por_dia["dia"],
                    y=por_dia["horas"],
                    mode="lines",
                    line=dict(color=GRAFICO_SERIES[1], width=2, shape="spline"),
                    hovertemplate="<b>%{x}</b><br>%{y:,.0f} horas-alistador<extra></extra>",
                )
                fig2.update_layout(
                    title=dict(
                        text="Horas-alistador disponibles",
                        font=dict(color=TEXT_SECONDARY, size=13),
                    ),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_SECONDARY, size=12),
                    margin=dict(l=10, r=10, t=40, b=10),
                    height=280,
                    showlegend=False,
                )
                fig2.update_xaxes(
                    showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True
                )
                fig2.update_yaxes(
                    gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True
                )
                st.plotly_chart(fig2, config={"displayModeBar": False})

            st.dataframe(
                por_dia.sort_values("dia", ascending=False),
                hide_index=True,
                column_config={
                    "dia": st.column_config.TextColumn("Día", width="small"),
                    "alistadores": st.column_config.NumberColumn("Alistadores", format="%d"),
                    "horas": st.column_config.NumberColumn("Horas-alistador", format="%.1f"),
                    "subpedidos": st.column_config.NumberColumn("Subpedidos", format="%d"),
                    "lineas": st.column_config.NumberColumn("Líneas", format="%.0f"),
                    "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
                    "lineas_hora": st.column_config.NumberColumn("Líneas/hora", format="%.1f"),
                },
            )

# ── Tiempos de ciclo ───────────────────────────────────────────────────────────
if tab_ciclos.open:
    with tab_ciclos:
        if ciclos_v.empty:
            st.info("Sin subpedidos con marcas completas en el periodo.")
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Subpedidos", f"{len(ciclos_v):,}")
            k2.metric(
                "Cola + picking",
                f"{ciclos_v['cola_y_picking_h'].median():.1f} h",
                help=(
                    "Desde que entra a la cola hasta que el alistamiento termina. "
                    "NO es tiempo de picking: la espera lo domina."
                ),
            )
            k3.metric("Inspección", f"{ciclos_v['inspeccion_h'].median():.1f} h")
            k4.metric("Ciclo total", f"{ciclos_v['ciclo_total_h'].median():.1f} h")

            etapas = [
                ("Cola + picking", "cola_y_picking_h"),
                ("Inspección", "inspeccion_h"),
                ("Ciclo total", "ciclo_total_h"),
            ]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Etapa": nombre,
                            "Mediana (h)": ciclos_v[col].median(),
                            "p90 (h)": ciclos_v[col].quantile(0.9),
                            "Máximo (h)": ciclos_v[col].max(),
                        }
                        for nombre, col in etapas
                    ]
                ),
                hide_index=True,
                column_config={
                    "Mediana (h)": st.column_config.NumberColumn(format="%.1f"),
                    "p90 (h)": st.column_config.NumberColumn(format="%.1f"),
                    "Máximo (h)": st.column_config.NumberColumn(format="%.1f"),
                },
            )
            st.caption(
                "Se usa **mediana y p90**, no promedio: la distribución tiene cola larga y "
                "el promedio quedaría arrastrado por unos pocos subpedidos muy demorados."
            )

            por_dia_c = (
                ciclos_v.groupby("dia", as_index=False)
                .agg(ciclo=("ciclo_total_h", "median"), cola=("cola_y_picking_h", "median"))
                .sort_values("dia")
            )
            fig3 = go.Figure()
            for nombre, columna, color in (
                ("Ciclo total", "ciclo", GRAFICO_SERIES[0]),
                ("Cola + picking", "cola", GRAFICO_SERIES[1]),
            ):
                fig3.add_scatter(
                    x=por_dia_c["dia"],
                    y=por_dia_c[columna],
                    mode="lines",
                    name=nombre,
                    line=dict(color=color, width=2, shape="spline"),
                    hovertemplate=f"<b>%{{x}}</b><br>{nombre}: %{{y:.1f}} h<extra></extra>",
                )
            fig3.update_layout(
                title=dict(text="Mediana diaria (horas)", font=dict(color=TEXT_SECONDARY, size=13)),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_SECONDARY, size=12),
                # Con 2 series la leyenda es obligatoria.
                legend=dict(orientation="h", y=1.16, x=0, font=dict(color=TEXT_SECONDARY)),
                margin=dict(l=10, r=10, t=60, b=10),
                height=300,
                hovermode="x unified",
            )
            fig3.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
            fig3.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
            st.plotly_chart(fig3, config={"displayModeBar": False})

# ── Carga por persona ──────────────────────────────────────────────────────────
if tab_carga.open:
    with tab_carga:
        st.warning(
            "**Esto mide reparto de trabajo, no rendimiento.** El alistamiento se hace por "
            "familia y un subpedido puede tener varios alistadores, pero el sistema registra "
            "un solo usuario al cerrarlo: el **83,8% de las líneas** queda acreditado a quien "
            "cerró, no a quien picó. Además, el ritmo diario de una misma persona varía tanto "
            "como su propio promedio. Comparar personas con esta base daría conclusiones "
            "equivocadas.",
            icon="⚖️",
        )

        con_alistador = ciclos_v[ciclos_v["alistador"].notna()]
        if con_alistador.empty:
            st.info("Sin alistador registrado en el periodo.")
        else:
            expandido = con_alistador.assign(
                persona=con_alistador["alistador"].str.split(",")
            ).explode("persona")
            expandido["persona"] = expandido["persona"].str.strip()
            expandido = expandido[expandido["persona"] != ""]

            carga = (
                expandido.groupby("persona")
                .agg(participaciones=("id_pedido", "size"), dias=("dia", "nunique"))
                .sort_values("participaciones", ascending=False)
                .reset_index()
                .rename(columns={"persona": "Alistador"})
            )
            carga["% del total"] = carga["participaciones"] / carga["participaciones"].sum() * 100
            carga["Participaciones por día"] = carga["participaciones"] / carga["dias"]

            # El cociente máximo/mínimo lo domina cualquiera con una sola
            # participación y da cifras absurdas (642x medido). La cuota del
            # quintil más activo es robusta a ese outlier y responde la
            # pregunta real: ¿está repartido o concentrado?
            quintil = max(1, round(len(carga) * 0.2))
            cuota_top = (
                carga["participaciones"].head(quintil).sum() / carga["participaciones"].sum()
            )

            c1, c2, c3 = st.columns(3)
            c1.metric("Alistadores activos", f"{len(carga):,}")
            c2.metric(
                f"Cuota del top {quintil}",
                f"{cuota_top * 100:.0f}%",
                help=(
                    "Qué parte de las participaciones concentra el 20% más activo. "
                    "Un reparto parejo daría cerca del 20%."
                ),
            )
            c3.metric("Participaciones", f"{carga['participaciones'].sum():,}")

            st.dataframe(
                carga,
                hide_index=True,
                column_config={
                    "Alistador": st.column_config.TextColumn(width="large"),
                    "participaciones": st.column_config.NumberColumn(
                        "Participaciones", format="%d"
                    ),
                    "dias": st.column_config.NumberColumn("Días activos", format="%d"),
                    "% del total": st.column_config.NumberColumn(format="%.1f"),
                    "Participaciones por día": st.column_config.NumberColumn(format="%.1f"),
                },
            )
            st.download_button(
                "⬇️ Descargar (CSV)",
                carga.to_csv(index=False).encode("utf-8-sig"),
                file_name="carga_alistadores.csv",
                mime="text/csv",
            )

# ── Cumplimiento de despacho ───────────────────────────────────────────────────
if tab_despacho.open:
    with tab_despacho:
        if despacho.empty:
            st.info(
                "Todavía no hay datos de cumplimiento de despacho. Los genera el ETL "
                "(`python -m etl.etl_principal`)."
            )
        else:
            periodo = despacho[(despacho["fecha"] >= rango[0]) & (despacho["fecha"] <= rango[1])]
            pedidos_t = int(periodo["pedidos"].sum())
            con_falta = int(periodo["pedidos_con_faltante"].sum())

            if not pedidos_t:
                st.info("Sin pedidos en el periodo seleccionado.")
            else:
                completos = pedidos_t - con_falta
                # DEC-069: se acotan por rango en SQL, no en pandas — traer los 7
                # meses cuesta 5,4 s contra 1,3 s del mes que se muestra.
                try:
                    lineas_p = get_despacho_faltantes(rango[0], rango[1])
                    lineas_periodo = get_despacho_lineas_diario(rango[0], rango[1])
                except sqlite3.OperationalError as e:
                    st.error(
                        f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos."
                    )
                    st.stop()

                # Clase ABC por referencia y stock actual, para separar el
                # faltante de abastecimiento del de operación.
                abc_ref = (
                    _abc[_abc["nivel"] == "referencia"][["clave", "abc"]].rename(
                        columns={"clave": "referencia", "abc": "clase"}
                    )
                    if not _abc.empty and "nivel" in _abc.columns
                    else pd.DataFrame(columns=["referencia", "clase"])
                )
                stock_ref = (
                    _salud[["referencia", "disponible"]]
                    if not _salud.empty
                    else pd.DataFrame(columns=["referencia", "disponible"])
                )

                # DEC-069: las dos escalas juntas. El indicador por pedido es el
                # más exigente —una línea corta arruina el pedido entero— y solo
                # con ese a la vista parece un problema sistémico. Por línea es
                # 99%: el problema es real pero concentrado, y esa diferencia
                # cambia por completo qué se hace al respecto.
                lp = lineas_periodo
                lineas_t = int(lp["lineas"].sum()) if not lp.empty else 0
                lineas_falta = int(lp["lineas_con_faltante"].sum()) if not lp.empty else 0
                u_pedidas = float(lp["unidades_pedidas"].sum()) if not lp.empty else 0.0
                u_falta = float(lp["unidades_faltantes"].sum()) if not lp.empty else 0.0

                k1, k2, k3, k4 = st.columns(4)
                k1.metric(
                    "Pedidos completos",
                    f"{completos / pedidos_t:.1%}",
                    help=f"De {pedidos_t:,} pedidos despachados, {completos:,} salieron con "
                    "todo lo pedido. Es el indicador más exigente: una sola línea corta "
                    "arruina el pedido entero.",
                )
                k2.metric(
                    "Líneas completas",
                    f"{1 - lineas_falta / lineas_t:.2%}" if lineas_t else "—",
                    help=f"De {lineas_t:,} líneas despachadas, {lineas_falta:,} salieron "
                    "cortas. Es el fill rate clásico de almacén.",
                )
                k3.metric(
                    "Unidades entregadas",
                    f"{1 - u_falta / u_pedidas:.2%}" if u_pedidas else "—",
                    help=f"Faltaron {u_falta:,.0f} unidades de {u_pedidas:,.0f} pedidas.",
                )
                k4.metric(
                    "No facturado",
                    f"${periodo['monto_no_despachado'].sum():,.0f}",
                    help="Lo que el sistema origen dejó de facturar por el faltante. "
                    "Es precio de venta, no costo.",
                )

                st.caption(
                    f"**Las tres cifras describen el mismo hecho a distinta escala.** "
                    f"{lineas_falta:,} líneas cortas de {lineas_t:,} son el "
                    f"{lineas_falta / lineas_t:.2%} del trabajo, pero afectan al "
                    f"{con_falta / pedidos_t:.1%} de los pedidos: el faltante está "
                    "concentrado, no repartido. Atacar esas referencias mueve el "
                    "indicador de pedidos mucho más de lo que sugiere su peso en líneas."
                    if lineas_t
                    else ""
                )

                # Serie mensual: la diaria es demasiado ruidosa para leer tendencia
                # (hay días con 30 pedidos y días con 240).
                mes = (
                    periodo.assign(mes=periodo["fecha"].str.slice(0, 7))
                    .groupby("mes", as_index=False)[["pedidos", "pedidos_con_faltante"]]
                    .sum()
                )
                mes["pct_completos"] = (
                    (mes["pedidos"] - mes["pedidos_con_faltante"]) / mes["pedidos"] * 100
                )

                fig_d = go.Figure()
                fig_d.add_bar(
                    x=mes["mes"],
                    y=mes["pct_completos"],
                    marker_color=GRAFICO_SERIES[0],
                    marker_line=dict(color=BG_DEEP, width=2),
                    marker_cornerradius=4,
                    hovertemplate="<b>%{x}</b><br>%{y:.1f}% completos<extra></extra>",
                )
                fig_d.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_SECONDARY, size=12),
                    margin=dict(l=10, r=10, t=30, b=10),
                    height=280,
                    showlegend=False,
                    bargap=0.35,
                    title=dict(
                        text="% de pedidos despachados completos, por mes",
                        font=dict(color=TEXT_PRIMARY, size=14),
                    ),
                )
                fig_d.update_xaxes(
                    showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True
                )
                # El eje arranca en el mínimo de la serie, no en 0: la variación
                # interesante vive en los últimos puntos porcentuales.
                fig_d.update_yaxes(
                    gridcolor=GRAFICO_GRID,
                    zerolinecolor=GRAFICO_GRID,
                    automargin=True,
                    ticksuffix="%",
                    range=[max(0, mes["pct_completos"].min() - 5), 100],
                )
                st.plotly_chart(fig_d, config={"displayModeBar": False})

                if len(lineas_p):
                    st.subheader("Qué referencia es la que no sale")
                    # DEC-069: agrupado por REFERENCIA y cruzado con la clase ABC
                    # y con la bodega. Con la fuente anterior esto era imposible:
                    # `detalle_diferencias` solo traía el nombre del producto en
                    # texto libre, sin referencia ni código de barras.
                    top = lineas_p.groupby("referencia", as_index=False).agg(
                        producto=("nombre_producto", "first"),
                        lineas=("faltante", "size"),
                        unidades=("faltante", "sum"),
                        pedidos=("id_pedido", "nunique"),
                    )
                    if not abc_ref.empty:
                        top = top.merge(abc_ref, on="referencia", how="left")
                    else:
                        top["clase"] = None
                    if not stock_ref.empty:
                        top = top.merge(stock_ref, on="referencia", how="left")
                    else:
                        top["disponible"] = None
                    top = top.nlargest(25, "unidades")

                    st.dataframe(
                        top,
                        hide_index=True,
                        column_config={
                            "referencia": st.column_config.TextColumn("Referencia", width="small"),
                            "producto": st.column_config.TextColumn("Producto", width="large"),
                            "clase": st.column_config.TextColumn(
                                "Clase",
                                width="small",
                                help="ABC de la referencia. Un faltante repetido en clase A "
                                "es un problema de abastecimiento; en clase C, de catálogo.",
                            ),
                            "lineas": st.column_config.NumberColumn("Líneas", format="%d"),
                            "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                            "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
                            "disponible": st.column_config.NumberColumn(
                                "Stock hoy",
                                format="%.0f",
                                help="Si faltó y hoy sigue en cero, es quiebre sostenido. "
                                "Si faltó y hoy hay stock, fue un problema de la operación, "
                                "no de inventario.",
                            ),
                        },
                    )
                    st.download_button(
                        "⬇️ Descargar (CSV)",
                        lineas_p.to_csv(index=False).encode("utf-8-sig"),
                        file_name="faltantes_despacho.csv",
                        mime="text/csv",
                    )

                    # La lectura accionable: separar el faltante que es de
                    # abastecimiento del que es de operación.
                    con_stock = top[pd.to_numeric(top.get("disponible"), errors="coerce") > 0]
                    if len(con_stock):
                        st.info(
                            f"**{len(con_stock)} de las 25 referencias que más faltaron tienen "
                            "stock disponible hoy.** En esas, el faltante no fue por falta de "
                            "inventario: el producto estaba. Es la lista donde buscar "
                            "problemas de ubicación, de alistamiento o de calidad del dato "
                            "de stock.",
                            icon="🔎",
                        )

                st.caption(
                    "**Es la mitad de E.1, no E.1 completo.** Mide *in full* (salió todo lo "
                    "que se pidió), no *on time*: el sistema origen no expone una fecha "
                    "comprometida de entrega, así que la puntualidad no se puede calcular "
                    "con los datos actuales. **375 de los pedidos con faltante traen la "
                    "diferencia solo en la cabecera, sin detalle por producto** — por eso "
                    "el conteo de pedidos y el de líneas no son proporcionales."
                )

st.caption(
    "**Lo que esta vista no mide, por falta de dato:** unidades por hora-hombre (no hay "
    "registro de horas trabajadas), *dock-to-stock* (no hay datos de recepción) y "
    "exactitud de *put-away*. El rendimiento individual queda pendiente de que se "
    "registre qué familia tomó cada alistador dentro del subpedido — con ese único "
    "campo, el 83,8% de líneas hoy inatribuibles pasaría a ser atribuible."
)
