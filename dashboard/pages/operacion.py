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

**Cumplimiento de despacho (DEC-065):** la cuarta pestaña mide el *fill
rate* —qué proporción de pedidos salió con todo lo pedido— a partir de
`v_diferencias_resumen` y `v_detalle_diferencias_num`. Es la mitad *in
full* de E.1; la mitad *on time* sigue sin dato en el origen.
"""

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from comun import formato_miles
from comun.motivos import CAUSA_FALTA_DE_PAGO, DIAS_LIMITE_PAGO, clasificar_motivo
from db import (
    get_auditoria_pago,
    get_comprobantes,
    get_credito_abierto,
    get_despacho_diario,
    get_despacho_faltantes,
    get_despacho_lineas_diario,
    get_estado_pago,
    get_inventario_abc,
    get_inventario_salud,
    get_motivos_cancelacion,
    get_operacion_ciclos,
    get_operacion_ventanas,
    get_pedidos_impagos,
    get_saldo_a_favor,
)
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Operación</p>', unsafe_allow_html=True)
st.title("⏱ Capacidad y tiempos de ciclo")

try:
    ciclos = get_operacion_ciclos()
    ventanas = get_operacion_ventanas()
    # DEC-065: el cumplimiento de despacho sale del ETL, no de la corrida de
    # inventario. Comparte el selector de periodo de la página —filtra por
    # `pedidos.fecha`, que es la misma escala de días— para que cambiar el
    # rango no deje una pestaña hablando de otro lapso.
    despacho = get_despacho_diario()
    # DEC-069: el detalle ahora trae `referencia`, así que se puede cruzar
    # con la clase ABC y con el stock actual — con `detalle_diferencias` era
    # imposible, no tenía con qué unir.
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

# DEC-082: el ciclo de auditoría de pago sale de `registro_operaciones`, que
# el scraper ya traía desde el 2026-01-23 — nadie lo había cruzado. Se acota
# por el mismo periodo que el resto de la página.
try:
    pagos = get_auditoria_pago(rango[0], rango[1])
    saldos = get_saldo_a_favor(rango[0], rango[1])
    # DEC-085: los motivos se acotan al periodo de la página, pero los pedidos
    # impagos NO — son el estado vivo de hoy y no dependen del rango elegido.
    motivos = get_motivos_cancelacion(rango[0], rango[1])
    impagos = get_pedidos_impagos()
    # DEC-086: tampoco depende del rango — es el crédito vivo de hoy.
    credito = get_credito_abierto()
    # DEC-089: el estado que calcula el origen, sin derivar.
    estado_pago = get_estado_pago(rango[0], rango[1])
    # DEC-090: el detalle por comprobante — canal, cuenta, revisor.
    comprobantes = get_comprobantes(rango[0], rango[1])
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

(
    tab_capacidad,
    tab_ciclos,
    tab_carga,
    tab_despacho,
    tab_pago,
    tab_cancel,
) = st.tabs(
    [
        "Capacidad del equipo",
        "Tiempos de ciclo",
        "Carga por persona",
        "Cumplimiento de despacho",
        "Auditoría de pago",
        "Cancelaciones",
    ]
)

# ── Capacidad ──────────────────────────────────────────────────────────────────
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
            fig.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
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
            fig2.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
            fig2.update_yaxes(gridcolor=GRAFICO_GRID, zerolinecolor=GRAFICO_GRID, automargin=True)
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
        expandido = con_alistador.assign(persona=con_alistador["alistador"].str.split(",")).explode(
            "persona"
        )
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
        cuota_top = carga["participaciones"].head(quintil).sum() / carga["participaciones"].sum()

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
                "participaciones": st.column_config.NumberColumn("Participaciones", format="%d"),
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
                st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
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
            fig_d.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
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
                        "inventario: el producto estaba. Es la lista donde buscar problemas "
                        "de ubicación, de alistamiento o de calidad del dato de stock.",
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

# ── Auditoría de pago ──────────────────────────────────────────────────────────
with tab_pago:
    # ── Estado de pago según el origen (DEC-089) ───────────────────────────
    # Va primero a propósito: es la respuesta del propio sistema, y todo lo
    # que sigue son derivaciones o cruces de eventos.
    if not estado_pago.empty:
        ep = estado_pago.copy()
        pendiente = ep[ep["saldo"] > 1]
        st.markdown("#### Estado de pago, calculado por el origen")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric(
            "Pedidos con dato",
            formato_miles(len(ep)),
            help="La tarjeta «Operación de pago» del sistema origen. No se "
            "deriva nada: el estado y el saldo los calcula él.",
        )
        e2.metric(
            "Por cobrar",
            f"${formato_miles(pendiente['saldo'].sum() / 1e6, 1)} M",
            help=f"En {formato_miles(len(pendiente))} pedidos con saldo mayor a cero.",
        )
        e3.metric(
            "Pagados",
            f"{(ep['pago_estado'] == 'Pagado').mean() * 100:.0f}%",
            help=f"{formato_miles(int((ep['pago_estado'] == 'Pagado').sum()))} de "
            f"{formato_miles(len(ep))} pedidos del periodo con tarjeta.",
        )
        e4.metric(
            "Con saldo",
            formato_miles(len(pendiente)),
            help="Pedidos donde el origen dice que todavía falta cobrar. El detalle "
            "por comprobante está más abajo.",
        )

        por_estado = (
            ep.groupby("pago_estado", as_index=False)
            .agg(pedidos=("id_pedido", "size"), saldo=("saldo", "sum"))
            .sort_values("pedidos", ascending=False)
        )
        st.dataframe(
            por_estado,
            hide_index=True,
            column_config={
                "pago_estado": st.column_config.TextColumn("Estado", width="large"),
                "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                "saldo": st.column_config.NumberColumn("Saldo pendiente", format="$%.0f"),
            },
        )

        # El canal importa: un pago en línea NO genera el evento
        # `Subir comprobante de pago`, así que las secciones de más abajo
        # —que se apoyan en ese evento— son ciegas a él (DEC-088).
        canales = (
            ep[ep["metodos"].notna()]["metodos"].str.split(",").explode().str.strip().value_counts()
        )
        if len(canales):
            linea = " · ".join(f"**{formato_miles(v)}** {k.lower()}" for k, v in canales.items())
            st.caption(
                f"Canales de pago: {linea}. Un **pago en línea no genera el evento "
                "«Subir comprobante de pago»**, así que las métricas de auditoría de "
                "más abajo —construidas sobre ese evento— no lo ven (DEC-088)."
            )

        st.caption(
            f"Cubre **{formato_miles(len(ep))} pedidos del periodo**: la tarjeta existe "
            "en el origen desde el **2026-07-16** y se verificó que no se renderiza para "
            "pedidos anteriores. Sustituye a la derivación `total − pagado`, que sobre "
            "los mismos pedidos marcaba impagos a 6 que el origen da por pagados. "
            "**No sirve para el saldo a favor**: `Saldo pendiente` nunca es negativo."
        )
        st.divider()

    if pagos.empty:
        st.info(
            "No hay comprobantes de pago registrados en el periodo. El registro de "
            "operaciones del sistema origen arranca el 2026-01-23."
        )
    else:
        pg = pagos.copy()
        pg["subido_en"] = pd.to_datetime(pg["subido_en"], errors="coerce")
        pg["auditado_en"] = pd.to_datetime(pg["auditado_en"], errors="coerce")
        pg["horas"] = (pg["auditado_en"] - pg["subido_en"]).dt.total_seconds() / 3600
        # "Ahora" se toma del dato, no del reloj: la base puede tener horas de
        # atraso y un pendiente no debe envejecer más rápido que su fuente.
        ahora = pg["subido_en"].max()
        pendientes = pg[pg["auditado_en"].isna()].copy()
        pendientes["espera_h"] = (ahora - pendientes["subido_en"]).dt.total_seconds() / 3600
        medibles = pg["horas"].dropna()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "Sin auditar",
            f"{len(pendientes):,}".replace(",", "."),
            help="Comprobante subido y nunca revisado. Es la cola de trabajo que "
            "hoy no se ve en ningún lado.",
        )
        k2.metric(
            "Mediana de revisión",
            f"{medibles.median():.1f} h" if len(medibles) else "—",
            help=f"Sobre {len(medibles):,} pedidos con ciclo completo.".replace(",", "."),
        )
        k3.metric(
            "p90",
            f"{medibles.quantile(0.9):.1f} h" if len(medibles) else "—",
            help="Nueve de cada diez comprobantes se revisan antes de este tiempo.",
        )
        k4.metric(
            "Más de 72 h",
            f"{int((medibles > 72).sum()):,}".replace(",", "."),
            help="Revisiones que tardaron más de tres días.",
        )

        # ── Veredicto (DEC-083) ────────────────────────────────────────────
        # `referencia` solo trae Aprobado/Rechazado desde el 2026-04-10. Los
        # pedidos anteriores se excluyen del denominador en vez de contarse
        # como "no rechazados", que hundiría la tasa del primer trimestre.
        con_ver = pg[pg["con_veredicto"] > 0]
        rechazados = pg[pg["rechazos"] > 0]
        sin_reaprobar = rechazados[rechazados["aprobaciones"] == 0]

        if len(con_ver):
            st.markdown("#### En qué termina la revisión")
            v1, v2, v3 = st.columns(3)
            v1.metric(
                "Tasa de rechazo",
                f"{len(rechazados) / len(con_ver) * 100:.1f}%",
                help=f"{formato_miles(len(rechazados))} pedidos con al menos un "
                f"comprobante rechazado, sobre {formato_miles(len(con_ver))} con "
                "veredicto registrado.",
            )
            v2.metric(
                "Rechazados sin re-aprobar",
                formato_miles(len(sin_reaprobar)),
                help="El comprobante se rechazó y nunca se aprobó uno nuevo. "
                "Es una cola distinta de la de 'sin auditar'.",
            )
            reintentos = rechazados["comprobantes"].median() if len(rechazados) else float("nan")
            base = pg[pg["rechazos"] == 0]["comprobantes"].median()
            v3.metric(
                "Comprobantes por pedido",
                f"{reintentos:.0f} vs {base:.0f}" if len(rechazados) else "—",
                help="Mediana con rechazo vs. sin rechazo. La diferencia es el "
                "retrabajo que cuesta cada rechazo.",
            )

            if len(sin_reaprobar):
                n = len(sin_reaprobar)
                st.error(
                    f"**{n} {'pedidos tienen' if n != 1 else 'pedido tiene'} el "
                    "comprobante rechazado y ningún reemplazo aprobado.** El cliente "
                    "cree que pagó y el pedido no avanza. Es la cola más urgente de "
                    "esta pestaña porque nadie está esperando por ella: la pelota "
                    "quedó del lado del cliente y no hay quien lo persiga.",
                    icon="🚫",
                )

            st.caption(
                "El veredicto (`Aprobado`/`Rechazado`) **existe en el origen desde el "
                "2026-04-10**. Antes de esa fecha las auditorías no lo registran, así "
                f"que {formato_miles(len(pg) - len(con_ver))} pedidos del periodo "
                "quedan fuera de esta tasa en lugar de contarse como aprobados."
            )
            st.divider()

        if len(pendientes):
            viejos = pendientes[pendientes["espera_h"] > 24]
            st.warning(
                f"**{len(pendientes)} comprobantes esperan revisión**"
                + (f", de los cuales **{len(viejos)} llevan más de 24 h**." if len(viejos) else ".")
                + " Un comprobante sin auditar bloquea el pedido: el cliente pagó y "
                "el sistema todavía no lo da por pagado.",
                icon="⏳",
            )

        # Serie mensual: la diaria es ruido, y lo que interesa es si el
        # tiempo de revisión se está degradando o no.
        if len(medibles) > 1:
            mes = (
                pg.dropna(subset=["horas"])
                .assign(mes=lambda d: d["subido_en"].dt.strftime("%Y-%m"))
                .groupby("mes", as_index=False)
                .agg(mediana=("horas", "median"), p90=("horas", lambda s: s.quantile(0.9)))
            )
            fig_p = go.Figure()
            fig_p.add_bar(
                x=mes["mes"],
                y=mes["mediana"],
                marker_color=GRAFICO_SERIES[0],
                marker_line=dict(color=BG_DEEP, width=2),
                marker_cornerradius=4,
                name="Mediana",
                hovertemplate="<b>%{x}</b><br>mediana %{y:.1f} h<extra></extra>",
            )
            fig_p.add_scatter(
                x=mes["mes"],
                y=mes["p90"],
                mode="lines+markers",
                line=dict(color=GRAFICO_SERIES[2], width=2, shape="spline"),
                marker=dict(size=8),
                name="p90",
                hovertemplate="<b>%{x}</b><br>p90 %{y:.1f} h<extra></extra>",
            )
            fig_p.update_layout(
                title=dict(
                    text="Horas hasta la revisión del comprobante, por mes",
                    font=dict(color=TEXT_PRIMARY, size=14),
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_SECONDARY, size=12),
                margin=dict(l=10, r=10, t=44, b=10),
                height=300,
                bargap=0.35,
                legend=dict(orientation="h", y=-0.16, font=dict(color=TEXT_SECONDARY)),
            )
            fig_p.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
            fig_p.update_yaxes(
                gridcolor=GRAFICO_GRID,
                zerolinecolor=GRAFICO_GRID,
                ticksuffix=" h",
                automargin=True,
            )
            st.plotly_chart(fig_p, config={"displayModeBar": False})

        if len(pendientes):
            st.subheader("Comprobantes esperando revisión")
            st.dataframe(
                pendientes.sort_values("espera_h", ascending=False)[
                    ["id_pedido", "fecha", "forma_pago", "subido_por", "espera_h"]
                ],
                hide_index=True,
                height=260,
                column_config={
                    "id_pedido": st.column_config.TextColumn("Pedido", width="medium"),
                    "fecha": st.column_config.TextColumn("Fecha", width="small"),
                    "forma_pago": st.column_config.TextColumn("Forma de pago", width="medium"),
                    "subido_por": st.column_config.TextColumn("Subió el comprobante"),
                    "espera_h": st.column_config.NumberColumn("Horas esperando", format="%.0f"),
                },
            )
            st.download_button(
                "⬇️ Descargar pendientes (CSV)",
                pendientes.to_csv(index=False).encode("utf-8-sig"),
                file_name="comprobantes_sin_auditar.csv",
                mime="text/csv",
            )

        auditores = (
            pg.dropna(subset=["auditado_por"])
            .groupby("auditado_por")
            .agg(revisiones=("horas", "size"), mediana_h=("horas", "median"))
            .sort_values("revisiones", ascending=False)
            .reset_index()
        )
        if len(auditores):
            st.subheader("Quién revisa los pagos")
            st.dataframe(
                auditores,
                hide_index=True,
                column_config={
                    "auditado_por": st.column_config.TextColumn("Auditor", width="large"),
                    "revisiones": st.column_config.NumberColumn("Revisiones", format="%d"),
                    "mediana_h": st.column_config.NumberColumn("Mediana (h)", format="%.1f"),
                },
            )
            st.caption(
                "A diferencia del alistamiento, **esta atribución sí es individual**: "
                "el sistema registra un solo usuario por auditoría y esa persona es "
                "quien la hizo. La mediana por auditor depende del turno y del tipo "
                "de pedido que le toca, así que compara cargas, no destrezas."
            )

        st.caption(
            "Sale de `registro_operaciones`, que el scraper ya traía: cruza "
            "**Subir comprobante de pago** con **Auditoría de pago**. La tarjeta "
            "«Operación de pago» que el sistema origen agregó en julio daría además "
            "el estado y el saldo sin derivarlos — hoy no se captura (DEC-081)."
        )

    # ── Comprobantes, uno por uno (DEC-090) ────────────────────────────────
    st.divider()
    st.subheader("Comprobantes de pago")
    if comprobantes.empty:
        st.info("No hay comprobantes registrados en el periodo.")
    else:
        cp = comprobantes.copy()
        # `estado_revision` vacío NO significa "pendiente de trabajo": 527 de
        # los 544 sin veredicto están en pedidos que el origen ya da por
        # PAGADOS. Un pedido pagado con cuatro comprobantes puede tener solo
        # uno adjudicado. Lo accionable son los que quedan sobre un pedido que
        # todavía debe — y son 17, no 544.
        cp = cp.merge(estado_pago[["id_pedido", "pago_estado"]], on="id_pedido", how="left")
        sin_veredicto = cp[cp["estado"] == "Sin revisar"]
        sin_veredicto_debe = sin_veredicto[sin_veredicto["pago_estado"] != "Pagado"]
        rechazados = cp[cp["estado"] == "Rechazado"]

        st.markdown(
            "**Esto no es una conciliación bancaria** y conviene decirlo: hay una "
            "sola cuenta receptora y el pipeline no ve ningún extracto contra el cual "
            "cruzar. Es **el lado del libro** — qué dice el sistema que entró, cuándo, "
            "por qué canal y si alguien lo verificó."
        )

        p1, p2, p3, p4 = st.columns(4)
        p1.metric(
            "Comprobantes",
            formato_miles(len(cp)),
            help="Registrados en el periodo, uno por fila de la tabla del origen.",
        )
        p2.metric(
            "Sin veredicto",
            formato_miles(len(sin_veredicto)),
            help=f"De los cuales {formato_miles(len(sin_veredicto) - len(sin_veredicto_debe))} "
            "están en pedidos que el origen ya da por pagados: el campo vacío no "
            "significa trabajo pendiente.",
        )
        p3.metric(
            "Rechazados",
            formato_miles(len(rechazados)),
            help=f"${formato_miles(rechazados['monto_pago'].sum() / 1e6, 1)} M "
            "reclamados y refutados.",
        )
        revisados = cp[cp["estado"].isin(["Aprobado", "Rechazado"])]
        p4.metric(
            "Rechazo por comprobante",
            f"{len(rechazados) / len(revisados) * 100:.1f}%" if len(revisados) else "—",
            help=f"Sobre {formato_miles(len(revisados))} comprobantes con veredicto. "
            "La otra tasa de esta pestaña se mide por PEDIDO y sobre los 7 meses del "
            "registro de operaciones; las dos convergen en ~5% por caminos distintos.",
        )

        if len(sin_veredicto_debe):
            st.warning(
                f"**{formato_miles(len(sin_veredicto_debe))} comprobantes sin veredicto "
                "sobre pedidos que todavía deben**, por "
                f"${formato_miles(sin_veredicto_debe['monto_pago'].sum() / 1e6, 1)} M. "
                f"Los otros {formato_miles(len(sin_veredicto) - len(sin_veredicto_debe))} "
                "sin veredicto están en pedidos ya pagados y **no son cola de trabajo**: "
                "un pedido pagado con varios comprobantes puede tener solo uno "
                "adjudicado.",
                icon="🧾",
            )

        # Canal: la distinción importa porque el pago en línea NO genera el
        # evento `Subir comprobante de pago` y es invisible para todo lo que
        # se construye sobre el registro de operaciones (DEC-088).
        por_canal = (
            cp.groupby("metodo_pago", as_index=False)
            .agg(comprobantes=("id_pedido", "size"), monto=("monto_pago", "sum"))
            .sort_values("monto", ascending=False)
        )
        c_izq, c_der = st.columns(2)
        with c_izq:
            st.markdown("**Por canal**")
            st.dataframe(
                por_canal,
                hide_index=True,
                column_config={
                    "metodo_pago": st.column_config.TextColumn("Canal", width="medium"),
                    "comprobantes": st.column_config.NumberColumn("Comprobantes", format="%d"),
                    "monto": st.column_config.NumberColumn("Monto", format="$%.0f"),
                },
            )
        with c_der:
            st.markdown("**Quién revisa**")
            por_revisor = (
                cp[cp["revisor"] != ""]
                .groupby("revisor", as_index=False)
                .agg(revisiones=("id_pedido", "size"), monto=("monto_pago", "sum"))
                .sort_values("revisiones", ascending=False)
            )
            st.dataframe(
                por_revisor,
                hide_index=True,
                column_config={
                    "revisor": st.column_config.TextColumn("Revisor", width="large"),
                    "revisiones": st.column_config.NumberColumn("Revisiones", format="%d"),
                    "monto": st.column_config.NumberColumn("Monto revisado", format="$%.0f"),
                },
            )

        if len(sin_veredicto_debe):
            st.markdown("**Comprobantes sin veredicto sobre pedidos que deben**")
            st.dataframe(
                sin_veredicto_debe.sort_values("fecha")[
                    [
                        "id_pedido",
                        "fecha",
                        "nombre_empresa",
                        "metodo_pago",
                        "monto_pago",
                        "hora_pago",
                    ]
                ],
                hide_index=True,
                height=280,
                column_config={
                    "id_pedido": st.column_config.TextColumn("Pedido", width="medium"),
                    "fecha": st.column_config.TextColumn("Fecha", width="small"),
                    "nombre_empresa": st.column_config.TextColumn("Cliente", width="large"),
                    "metodo_pago": st.column_config.TextColumn("Canal", width="medium"),
                    "monto_pago": st.column_config.NumberColumn("Monto", format="$%.0f"),
                    "hora_pago": st.column_config.TextColumn("Hora de pago"),
                },
            )

        st.download_button(
            "⬇️ Descargar comprobantes (CSV)",
            cp.to_csv(index=False).encode("utf-8-sig"),
            file_name="comprobantes_de_pago.csv",
            mime="text/csv",
        )
        st.caption(
            "Se guardan **`Monto del comprobante` y `Monto de pago` por separado** "
            "porque el origen los trae como columnas distintas; coinciden en el 76% de "
            "las filas y en un pedido con varios comprobantes cada uno lleva su "
            "fracción del total — es **pago fraccionado**. La columna `Comprobante` del "
            "origen **no es un identificador** sino el texto de un enlace (`Ver`), así "
            "que no se puede aparear un comprobante entre pedidos."
        )

    # ── Saldo a favor del cliente (DEC-084) ────────────────────────────────
    st.divider()
    st.subheader("Saldo a favor del cliente")
    if saldos.empty:
        st.info("Sin diferencias de pago registradas en el periodo.")
    else:
        sa = saldos.copy()
        favor = sa[sa["saldo"] > 0]
        sano = favor[favor["anomalo"] == 0]
        raro = favor[favor["anomalo"] == 1]
        contra = sa[sa["saldo"] < 0]

        st.markdown(
            "El cliente paga primero; si el pedido sale corto, lo facturado baja y "
            "**la diferencia le queda a favor**. Sale de `gestion_diferencias`, donde "
            "el origen ya hizo la cuenta — acá no se deriva nada."
        )

        s1, s2, s3 = st.columns(3)
        s1.metric(
            "Saldo a favor",
            f"${formato_miles(sano['saldo'].sum() / 1e6, 1)} M",
            help=f"En {formato_miles(len(sano))} pedidos. Es plata que se le debe al cliente.",
        )
        s2.metric(
            "Saldo en contra",
            f"${formato_miles(-contra['saldo'].sum() / 1e6, 1)} M",
            help=f"En {formato_miles(len(contra))} pedidos el faltante no alcanzó a "
            "cubrir lo que el cliente aún debía.",
        )
        s3.metric(
            "Pedidos con saldo",
            formato_miles(len(sa)),
            help="Pedidos del periodo con diferencia entre lo pagado y lo facturado.",
        )

        if len(raro):
            # La frase se escribe sin concordancia de número: con un solo caso
            # "registran / entre todos suman" queda mal, y ramificar toda la
            # oración por singular y plural es peor que redactarla neutra.
            n_raro = len(raro)
            st.warning(
                f"**Fuera de la cifra de arriba: {n_raro} "
                f"{'pedidos' if n_raro != 1 else 'pedido'}** con un pago registrado de "
                f"más de **2 veces** el valor del pedido, por "
                f"${formato_miles(raro['saldo'].sum() / 1e6, 1)} M —el "
                f"{raro['saldo'].sum() / favor['saldo'].sum() * 100:.0f}% del bruto—. "
                "Un saldo a favor de 107 veces el pedido no existe: es un dato malo del "
                "origen. Va aparte porque promediarlo convertiría una cifra accionable "
                "en ruido.",
                icon="⚠️",
            )
            with st.expander(f"Ver los {len(raro)} pedidos anómalos"):
                st.dataframe(
                    raro.sort_values("saldo", ascending=False)[
                        ["id_pedido", "fecha", "nombre_empresa", "total", "pagado", "saldo"]
                    ],
                    hide_index=True,
                    column_config={
                        "id_pedido": st.column_config.TextColumn("Pedido"),
                        "fecha": st.column_config.TextColumn("Fecha", width="small"),
                        "nombre_empresa": st.column_config.TextColumn("Cliente", width="large"),
                        "total": st.column_config.NumberColumn("Total", format="$%.0f"),
                        "pagado": st.column_config.NumberColumn("Pagado", format="$%.0f"),
                        "saldo": st.column_config.NumberColumn("Saldo", format="$%.0f"),
                    },
                )

        if len(sano):
            por_cliente = (
                sano.groupby(["nit", "nombre_empresa"], as_index=False)
                .agg(pedidos=("saldo", "size"), saldo=("saldo", "sum"))
                .sort_values("saldo", ascending=False)
            )
            st.markdown("**Clientes con más saldo a favor**")
            st.dataframe(
                por_cliente.head(25),
                hide_index=True,
                height=280,
                column_config={
                    "nit": st.column_config.TextColumn("NIT", width="small"),
                    "nombre_empresa": st.column_config.TextColumn("Cliente", width="large"),
                    "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                    "saldo": st.column_config.NumberColumn("Saldo a favor", format="$%.0f"),
                },
            )
            st.download_button(
                "⬇️ Descargar saldo a favor por cliente (CSV)",
                por_cliente.to_csv(index=False).encode("utf-8-sig"),
                file_name="saldo_a_favor_clientes.csv",
                mime="text/csv",
            )
            st.caption(
                "**Un NIT no siempre es un cliente.** Varios de los NIT con más saldo "
                "comparten raíz y se diferencian solo en el dígito de verificación, así "
                "que podrían ser el mismo cliente contado tres veces. No se consolidan: "
                "exige confirmar la regla de identidad con el área comercial, y agrupar "
                "mal es peor que no agrupar."
            )

    # ── Maduración de crédito (DEC-086) ────────────────────────────────────
    st.divider()
    st.subheader("Crédito abierto y su vencimiento")
    if credito.empty:
        st.info("No hay pedidos a crédito con vencimiento registrado.")
    else:
        cr = credito.copy()
        vencido = cr[cr["atraso"] > 0]
        muy_vencido = cr[cr["atraso"] > 90]

        st.markdown(
            "El origen conserva `vencimiento_credito` **mientras el crédito está "
            "abierto**, y lo suelta cuando se salda. Se considera abierto lo que "
            "conserva el vencimiento, no está cancelado y **nunca tuvo un comprobante "
            "de pago subido** — tres señales independientes, ninguna monetaria."
        )

        d1, d2, d3 = st.columns(3)
        d1.metric(
            "Crédito abierto",
            f"${formato_miles(cr['valor'].sum() / 1e6, 1)} M",
            help=f"{formato_miles(len(cr))} pedidos que el sistema sigue mostrando "
            "con crédito vivo.",
        )
        d2.metric(
            "Vencido",
            f"${formato_miles(vencido['valor'].sum() / 1e6, 1)} M",
            help=f"{formato_miles(len(vencido))} pedidos pasados de su fecha de vencimiento.",
        )
        d3.metric(
            "Más de 90 días",
            formato_miles(len(muy_vencido)),
            help=f"${formato_miles(muy_vencido['valor'].sum() / 1e6, 1)} M con más de "
            "tres meses de atraso.",
        )

        # Maduración clásica de cartera: tramos ordenados de menor a mayor
        # atraso, con el vencimiento como frontera visible.
        tramos_cr = [
            ("Al día", cr["atraso"] <= 0),
            ("1-30 d", cr["atraso"].between(1, 30)),
            ("31-60 d", cr["atraso"].between(31, 60)),
            ("61-90 d", cr["atraso"].between(61, 90)),
            ("Más de 90 d", cr["atraso"] > 90),
        ]
        etiquetas = [t[0] for t in tramos_cr]
        valores = [cr[sel]["valor"].sum() / 1e6 for _, sel in tramos_cr]
        conteos = [int(sel.sum()) for _, sel in tramos_cr]
        # Al día en teal (sano) y el resto en el ámbar de advertencia, que se
        # oscurece con el atraso: es una secuencia de gravedad, no categorías.
        colores = [GRAFICO_SERIES[0], "#E0A458", "#D08C45", "#C4743A", "#C4574B"]

        fig_cr = go.Figure()
        fig_cr.add_bar(
            x=etiquetas,
            y=valores,
            marker_color=colores,
            marker_line=dict(color=BG_DEEP, width=2),
            marker_cornerradius=4,
            text=[
                f"${formato_miles(v, 0)} M<br>{formato_miles(n)} ped."
                for v, n in zip(valores, conteos, strict=True)
            ],
            textposition="outside",
            textfont=dict(color=TEXT_PRIMARY, size=11),
            cliponaxis=False,
            hovertemplate="<b>%{x}</b><br>$%{y:,.1f} M<extra></extra>",
        )
        fig_cr.update_layout(
            title=dict(
                text="Maduración del crédito abierto",
                font=dict(color=TEXT_PRIMARY, size=14),
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=TEXT_SECONDARY, size=12),
            margin=dict(l=10, r=10, t=44, b=10),
            height=340,
            showlegend=False,
            bargap=0.35,
        )
        fig_cr.update_xaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
        fig_cr.update_yaxes(
            gridcolor=GRAFICO_GRID,
            zerolinecolor=GRAFICO_GRID,
            ticksuffix=" M",
            automargin=True,
        )
        st.plotly_chart(fig_cr, config={"displayModeBar": False})

        if len(vencido):
            por_cliente_cr = (
                vencido.groupby(["nit", "nombre_empresa"], as_index=False)
                .agg(
                    pedidos=("valor", "size"),
                    valor=("valor", "sum"),
                    atraso_max=("atraso", "max"),
                )
                .sort_values("valor", ascending=False)
            )
            cuota_top2 = por_cliente_cr.head(2)["valor"].sum() / vencido["valor"].sum() * 100
            st.warning(
                f"**El {cuota_top2:.0f}% del vencido está en dos clientes**, y son "
                "justamente los que menos comprobantes cargan (22% y 21%, contra 77-90% "
                "de los medianos). **Que no haya comprobante no prueba que no pagaron**: "
                "un cliente grande puede pagar por transferencia sin que nadie suba nada. "
                "Esto es una **cola de verificación**, no un estado de cuenta.",
                icon="🔍",
            )
            st.dataframe(
                por_cliente_cr.head(25),
                hide_index=True,
                height=280,
                column_config={
                    "nit": st.column_config.TextColumn("NIT", width="small"),
                    "nombre_empresa": st.column_config.TextColumn("Cliente", width="large"),
                    "pedidos": st.column_config.NumberColumn("Pedidos", format="%d"),
                    "valor": st.column_config.NumberColumn("Vencido", format="$%.0f"),
                    "atraso_max": st.column_config.NumberColumn("Atraso máx. (d)", format="%d"),
                },
            )
            st.download_button(
                "⬇️ Descargar crédito vencido (CSV)",
                vencido.sort_values("atraso", ascending=False)
                .to_csv(index=False)
                .encode("utf-8-sig"),
                file_name="credito_vencido.csv",
                mime="text/csv",
            )

        st.caption(
            "**No se usa «Monto pagado» acá.** Para crédito ese campo no discrimina: "
            "su mediana es 0% tanto en los créditos abiertos como en los ya saldados. "
            "El comprobante sí — los pedidos `Completado` que conservan los campos de "
            "crédito tienen comprobante el **100%** de las veces, contra el **5,2%** de "
            f"los `Entregado sin liquidar`. Los plazos pactados son "
            f"{formato_miles(len(cr))} pedidos entre 5 y 45 días, con 30 como el más "
            "común."
        )


# ── Cancelaciones: por qué mueren los pedidos (DEC-085) ────────────────────────
with tab_cancel:
    if motivos.empty:
        st.info(
            "No hay cancelaciones registradas en el periodo. El motivo vive en el "
            "registro de operaciones, que arranca el 2026-01-23."
        )
    else:
        mo = motivos.copy()
        mo["causa"] = mo["motivo"].map(clasificar_motivo)
        pago = mo[mo["causa"] == CAUSA_FALTA_DE_PAGO]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            "Pedidos cancelados",
            formato_miles(len(mo)),
            help="En el periodo seleccionado, contando un solo evento por pedido.",
        )
        c2.metric(
            "Por falta de pago",
            f"{len(pago) / len(mo) * 100:.0f}%",
            help=f"{formato_miles(len(pago))} pedidos. Es la causa dominante, y por bastante.",
        )
        c3.metric(
            "Valor no cobrado",
            f"${formato_miles(pago['valor'].sum() / 1e6, 1)} M",
            help="Suma del total a pagar de los pedidos cancelados por falta de pago.",
        )
        c4.metric(
            "Días hasta cancelar",
            f"{pago['dias'].median():.0f}" if len(pago) else "—",
            help="Mediana. El 94% cae entre el día 3 y el 7, y la mediana no se mueve "
            "mes a mes: es una regla que alguien aplica de forma consistente.",
        )

        # Barras horizontales: los nombres de causa son largos y en vertical se
        # cortarían. Una sola serie, así que no hay leyenda que poner; la causa
        # dominante se destaca por color y el resto queda en gris de fondo.
        por_causa = (
            mo.groupby("causa", as_index=False)
            .agg(pedidos=("id_pedido", "size"), valor=("valor", "sum"))
            .sort_values("pedidos")
        )
        # El neutro NO puede ser GRAFICO_GRID (blanco al 6%): sirve para
        # cuadrículas, pero como relleno de barra desaparece contra el fondo y
        # las causas chicas quedan sin barra visible — se vio exportando el PNG,
        # no en el render. Este es el mismo blanco a una opacidad legible.
        _NEUTRO_BARRA = "rgba(255,255,255,0.22)"
        fig_c = go.Figure()
        fig_c.add_bar(
            x=por_causa["pedidos"],
            y=por_causa["causa"],
            orientation="h",
            marker_color=[
                GRAFICO_SERIES[0] if c == CAUSA_FALTA_DE_PAGO else _NEUTRO_BARRA
                for c in por_causa["causa"]
            ],
            marker_line=dict(color=BG_DEEP, width=2),
            marker_cornerradius=4,
            text=[formato_miles(v) for v in por_causa["pedidos"]],
            textposition="outside",
            textfont=dict(color=TEXT_PRIMARY),
            cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>%{x:,.0f} pedidos<extra></extra>",
        )
        fig_c.update_layout(
            title=dict(
                text="Por qué se cancelan los pedidos",
                font=dict(color=TEXT_PRIMARY, size=14),
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=TEXT_SECONDARY, size=12),
            margin=dict(l=10, r=70, t=44, b=10),
            height=380,
            showlegend=False,
            bargap=0.3,
        )
        fig_c.update_xaxes(showgrid=False, showticklabels=False, automargin=True)
        fig_c.update_yaxes(showgrid=False, tickfont=dict(color=TEXT_PRIMARY), automargin=True)
        st.plotly_chart(fig_c, config={"displayModeBar": False})

        st.caption(
            "El motivo lo escribe a mano quien cancela, en texto libre: hay "
            f"{formato_miles(mo['motivo'].nunique())} redacciones distintas para "
            f"{formato_miles(len(mo))} pedidos, con emojis, iniciales pegadas al final y "
            "mayúsculas inconsistentes. Se clasifican por palabras clave "
            "(`comun/motivos.py`), no por texto exacto. **`Sin motivo registrado` no es lo "
            "mismo que `Otro`**: antes del 2026-01-23 el origen guardaba ahí el ID "
            "numérico del pedido, no una explicación."
        )

        st.divider()

        # ── Lo accionable: los que van camino a cancelarse ─────────────────
        st.subheader("Pedidos camino a cancelarse")
        if impagos.empty:
            st.info("No hay pedidos vivos con saldo pendiente.")
        else:
            st.markdown(
                f"Si la empresa cancela por falta de pago a los **{DIAS_LIMITE_PAGO} días** "
                "—y lo hace de forma consistente—, entonces los pedidos vivos, de pago "
                "inmediato y con saldo son **predecibles**. Esto no explica el pasado: "
                "dice qué mercancía está por liberarse."
            )

            tramos = [
                ("Recién creado", impagos["dias"].between(0, 2), "0-2 días"),
                ("En zona de riesgo", impagos["dias"].between(3, 4), "3-4 días"),
                ("En el límite", impagos["dias"].between(5, 7), "5-7 días"),
                ("Pasado el límite", impagos["dias"] >= 8, "8+ días"),
            ]
            cols = st.columns(4)
            for col, (etiqueta, sel, rango_txt) in zip(cols, tramos, strict=True):
                sub = impagos[sel]
                col.metric(
                    etiqueta,
                    formato_miles(len(sub)),
                    help=f"{rango_txt} desde que se creó el pedido. "
                    f"${formato_miles(sub['saldo'].sum() / 1e6, 1)} M en saldo, "
                    f"{formato_miles(sub['unidades'].sum())} unidades retenidas.",
                )

            riesgo = impagos[impagos["dias"] >= 3].sort_values("dias", ascending=False)
            if len(riesgo):
                st.warning(
                    f"**{formato_miles(len(riesgo))} pedidos llevan 3 días o más sin pagar** "
                    f"y retienen {formato_miles(riesgo['unidades'].sum())} unidades en "
                    f"{formato_miles(riesgo['lineas'].sum())} líneas. Según el "
                    "comportamiento histórico la mayoría no se va a despachar: es stock que "
                    "figura comprometido y está por volver a quedar disponible.",
                    icon="📦",
                )
                st.dataframe(
                    riesgo[
                        [
                            "id_pedido",
                            "fecha",
                            "dias",
                            "nombre_empresa",
                            "saldo",
                            "lineas",
                            "unidades",
                        ]
                    ],
                    hide_index=True,
                    height=300,
                    column_config={
                        "id_pedido": st.column_config.TextColumn("Pedido", width="medium"),
                        "fecha": st.column_config.TextColumn("Fecha", width="small"),
                        "dias": st.column_config.NumberColumn("Días", format="%d"),
                        "nombre_empresa": st.column_config.TextColumn("Cliente", width="large"),
                        "saldo": st.column_config.NumberColumn("Saldo", format="$%.0f"),
                        "lineas": st.column_config.NumberColumn("Líneas", format="%d"),
                        "unidades": st.column_config.NumberColumn("Unidades", format="%.0f"),
                    },
                )
                st.download_button(
                    "⬇️ Descargar pedidos en riesgo (CSV)",
                    riesgo.to_csv(index=False).encode("utf-8-sig"),
                    file_name="pedidos_camino_a_cancelarse.csv",
                    mime="text/csv",
                )

            n_origen = int((impagos["fuente"] == "origen").sum())
            st.caption(
                "Solo **pago inmediato**: el crédito no debe nada hasta su vencimiento, y "
                "en contra entrega el cobro se registra fuera de este campo (figura al 8,4% "
                "pagado, DEC-084). Incluirlos llenaría la lista de falsos positivos. "
                f"El saldo sale **del origen en {formato_miles(n_origen)} de "
                f"{formato_miles(len(impagos))}** pedidos (DEC-089); en el resto —anteriores "
                "al 2026-07-16, donde la tarjeta no existe— se deriva de `total − pagado`, "
                "que marca impagos a algunos que ya pagaron."
            )

        st.caption(
            "**Una hipótesis que la medición descartó.** Era tentador cerrar el círculo "
            "diciendo que la mercancía alistada y luego cancelada (DEC-063, mediana de 90 "
            "días en limbo) es esto mismo. No lo es: de esos 178 pedidos, solo 35 son por "
            "falta de pago. La cancelación por pago ocurre a los 5 días y casi siempre "
            "**antes** de alistar — ahí el sistema está funcionando. Los 90 días son otro "
            "fenómeno, todavía sin explicación."
        )
