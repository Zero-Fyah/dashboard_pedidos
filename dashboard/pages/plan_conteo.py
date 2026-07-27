"""
Plan de conteo — dimensionamiento y priorización del inventario progresivo (DEC-057).

Alinea el dashboard con la estrategia de inventario perpetuo por eventos
que el área presentará a Gerencia. Trabaja sobre la **línea SKU-posición**,
que es la unidad de auditoría que define ese plan: no se cuenta "una
referencia", se cuenta un ID dentro de una posición concreta.

Responde dos preguntas que el plan deja explícitamente abiertas:

1. **¿Cuánto trabajo es de verdad?** El plan dimensiona la Fase 3 como
   *posiciones totales × densidad media*, lo que cuenta las posiciones
   vacías como si hubiera que contarlas. Acá el número sale de las
   posiciones que realmente tienen inventario.
2. **¿Qué se cuenta primero?** El plan pide priorizar por clase ABC, valor
   y antigüedad, pero marca la clasificación ABC como pendiente. Ya está
   construida, así que la cola de trabajo se puede emitir hoy.

**Lo que esta vista NO calcula, a propósito:** exactitud de inventario
(IRA), estado de madurez de la ubicación y score de confiabilidad. Los tres
se alimentan de *eventos de conteo físico* — quién contó, cuándo, qué
encontró — que viven en el registro del área y no entran a este pipeline.
Mostrarlos acá sería inventar el dato que el plan existe para construir.
"""

import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import get_inventario_ubicaciones
from theme import BG_DEEP, GRAFICO_GRID, GRAFICO_SERIES, TEXT_PRIMARY, TEXT_SECONDARY

st.markdown('<p class="dp-breadcrumb">Dashboard / Inventario</p>', unsafe_allow_html=True)
st.title("📋 Plan de conteo")

try:
    df = get_inventario_ubicaciones()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info(
        "Todavía no hay detalle por ubicación. Se genera en cada ciclo del scheduler, "
        "o a mano con `python -m inventario.persistencia`."
    )
    st.stop()

ORDEN_CLASE = ["A", "B", "C", "Sin rotación"]

altura = df[df["tipo"] == "Altura"]
picking = df[df["tipo"] == "Picking"]

tab_dim, tab_cola, tab_pick = st.tabs(
    ["Dimensionamiento", "Cola de conteo", "Auditoría de picking"]
)


# ─────────────────────────────────────────────
# Dimensionamiento
# ─────────────────────────────────────────────
with tab_dim:
    st.subheader("El trabajo real de la Fase 3")

    lineas_alt = len(altura)
    ocupadas = altura["ubicacion"].nunique()
    ab = altura["clase"].isin(["A", "B"]).sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Líneas SKU-posición en altura", f"{lineas_alt:,}".replace(",", "."))
    c2.metric("Posiciones ocupadas", f"{ocupadas:,}".replace(",", "."))
    c3.metric("Líneas por posición ocupada", f"{lineas_alt / ocupadas:.2f}" if ocupadas else "—")
    c4.metric("Son clase A o B", f"{ab / lineas_alt * 100:.1f}%" if lineas_alt else "—")

    st.caption(
        "La unidad es la línea SKU-posición: un ID dentro de una posición. "
        "Es lo que efectivamente se cuenta cuando alguien sube a verificar una estiba."
    )

    st.markdown("##### Contraste con los supuestos del plan")
    st.markdown(
        "Las dos cifras que sostienen el compromiso de 90-120 días resultan ser "
        "**más favorables** de lo estimado. El plan las marca como pendientes de "
        "confirmar; acá salen medidas."
    )

    supuestos = pd.DataFrame(
        [
            {
                "Supuesto del plan": "Líneas SKU-posición a contar",
                "Estimado": "~6.860",
                "Medido": f"{lineas_alt:,}".replace(",", "."),
                "Por qué difiere": (
                    "El estimado multiplica las 3.176 posiciones por la densidad media. "
                    f"Solo {ocupadas:,} tienen inventario: las vacías no se cuentan.".replace(
                        ",", "."
                    )
                ),
            },
            {
                "Supuesto del plan": "Clases A y B, como % del alcance",
                "Estimado": "~50%",
                "Medido": f"{ab / lineas_alt * 100:.1f}%" if lineas_alt else "—",
                "Por qué difiere": (
                    "El plan lo marca como ilustrativo, a confirmar con la "
                    "clasificación ABC. Ya está construida."
                ),
            },
            {
                "Supuesto del plan": "Densidad media de referencias por posición",
                "Estimado": "~2,2",
                "Medido": f"{lineas_alt / ocupadas:.2f}".replace(".", ",") if ocupadas else "—",
                "Por qué difiere": "Coincide — la revisión preliminar del plan dio en el clavo.",
            },
        ]
    )
    st.dataframe(supuestos, hide_index=True, width="stretch")

    st.markdown("##### Qué implica para la capacidad")
    ritmo_lo, ritmo_hi = 20, 25
    filas = []
    for etiqueta, n in [("Todo el alcance de altura", lineas_alt), ("Solo clases A y B", ab)]:
        for personas in (1, 2):
            lo = n / (ritmo_hi * personas) / 5
            hi = n / (ritmo_lo * personas) / 5
            filas.append(
                {
                    "Alcance": etiqueta,
                    "Dotación": f"{personas} asistente/día"
                    if personas == 1
                    else "2 asistentes/día",
                    "Semanas": f"{lo:.0f} a {hi:.0f}",
                    "Meses": f"{lo / 4.3:.1f} a {hi / 4.3:.1f}",
                }
            )
    st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
    st.caption(
        f"Al ritmo de {ritmo_lo}-{ritmo_hi} líneas por persona y día que estima el plan, "
        "sobre 5 días hábiles. El ritmo sigue siendo el supuesto sin medir: conviene "
        "confirmarlo en los primeros días de conteo real y recalcular acá."
    )

    st.divider()
    st.subheader("Hasta dónde contar")

    orden = altura.sort_values("valor_linea", ascending=False).reset_index(drop=True)
    total_valor = orden["valor_linea"].sum()
    if total_valor > 0:
        acum = orden["valor_linea"].cumsum() / total_valor * 100
        pct_lineas = (orden.index + 1) / len(orden) * 100
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=pct_lineas,
                y=acum,
                mode="lines",
                line={"color": GRAFICO_SERIES[0], "width": 2},
                name="Valor acumulado",
                hovertemplate="Contando el %{x:.0f}% de las líneas<br>"
                "se cubre el %{y:.1f}% del valor<extra></extra>",
            )
        )
        for hito in (80, 95):
            cruce = float(pct_lineas[acum >= hito].min()) if (acum >= hito).any() else None
            if cruce is not None:
                fig.add_hline(
                    y=hito,
                    line_dash="dot",
                    line_color=TEXT_SECONDARY,
                    line_width=1,
                    annotation_text=f"{hito}% del valor con el {cruce:.0f}% de las líneas",
                    annotation_font_color=TEXT_SECONDARY,
                    annotation_position="top left",
                )
        fig.update_layout(
            height=360,
            margin={"l": 10, "r": 10, "t": 20, "b": 10},
            paper_bgcolor=BG_DEEP,
            plot_bgcolor=BG_DEEP,
            font={"color": TEXT_PRIMARY},
            showlegend=False,
            xaxis={
                "title": "% de líneas contadas (de mayor a menor valor)",
                "gridcolor": GRAFICO_GRID,
                "automargin": True,
            },
            yaxis={
                "title": "% del valor cubierto",
                "gridcolor": GRAFICO_GRID,
                "automargin": True,
            },
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "El valor está a precio de venta del catálogo, no a costo: sirve para "
            "ordenar el trabajo, no como cifra financiera. El plan ya advierte que el "
            "costeo no está formalizado con Contabilidad."
        )


# ─────────────────────────────────────────────
# Cola de conteo
# ─────────────────────────────────────────────
with tab_cola:
    st.subheader("Qué contar primero")
    st.markdown(
        "El orden es el que pide el plan: **manda la clase, y dentro de la clase manda "
        "el valor**. No hay un score con pesos — un número compuesto daría más "
        "precisión aparente que fundamento y nadie podría explicar por qué una "
        "posición quedó sobre otra."
    )

    f1, f2, f3 = st.columns(3)
    tipos = f1.multiselect("Tipo de ubicación", ["Altura", "Picking"], default=["Altura"])
    clases = f2.multiselect("Clase del producto", ORDEN_CLASE, default=["A", "B"])
    racks_disp = sorted(df["rack"].dropna().astype(str).unique())
    racks = f3.multiselect("Rack", racks_disp, default=[])

    cola = df[df["tipo"].isin(tipos) & df["clase"].isin(clases)]
    if racks:
        cola = cola[cola["rack"].astype(str).isin(racks)]
    cola = cola.sort_values("prioridad")

    m1, m2, m3 = st.columns(3)
    m1.metric("Líneas en la cola", f"{len(cola):,}".replace(",", "."))
    m2.metric("Posiciones a visitar", f"{cola['ubicacion'].nunique():,}".replace(",", "."))
    m3.metric("Valor involucrado", f"${cola['valor_linea'].sum():,.0f}".replace(",", "."))

    vista = cola[
        [
            "ubicacion",
            "rack",
            "nivel",
            "referencia",
            "id_especificacion",
            "cantidad",
            "clase",
            "xyz",
            "valor_linea",
            "dias_sin_salida",
        ]
    ]
    st.dataframe(
        vista,
        hide_index=True,
        width="stretch",
        height=420,
        column_config={
            "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
            "rack": st.column_config.TextColumn("Rack", width="small"),
            "nivel": st.column_config.NumberColumn("Nivel", width="small"),
            "referencia": st.column_config.TextColumn("Referencia", width="small"),
            "id_especificacion": st.column_config.TextColumn("ID", width="medium"),
            "cantidad": st.column_config.NumberColumn("Unidades", format="%d"),
            "clase": st.column_config.TextColumn("Clase", width="small"),
            "xyz": st.column_config.TextColumn("XYZ", width="small"),
            "valor_linea": st.column_config.NumberColumn("Valor", format="$%d"),
            "dias_sin_salida": st.column_config.NumberColumn("Días sin salida", format="%d"),
        },
    )
    st.download_button(
        "⬇️ Descargar la cola en CSV",
        vista.to_csv(index=False).encode("utf-8-sig"),
        file_name="cola_de_conteo.csv",
        mime="text/csv",
    )
    st.caption(
        "**Días sin salida** es de la referencia, no de la posición: mide hace cuánto "
        "que la operación no toca ese producto. No es «días desde el último conteo» — "
        "ese dato vive en el registro físico del área, fuera de este pipeline."
    )

    st.divider()
    st.markdown("##### Lo que la operación no va a tocar sola")
    sin_rot = altura[altura["clase"] == "Sin rotación"]
    pos_sin_rot = (
        altura.groupby("ubicacion")["clase"].apply(lambda s: (s == "Sin rotación").all()).sum()
    )
    st.markdown(
        f"**{len(sin_rot):,} líneas en altura** no registran ninguna venta en la ventana "
        f"de análisis, y **{pos_sin_rot:,} posiciones** contienen únicamente producto de "
        f"ese tipo. Representan **${sin_rot['valor_linea'].sum():,.0f}** a precio de "
        "venta. El plan las señala como riesgo de prioridad alta — *«ubicaciones de "
        "rotación cero, nunca tocadas por la operación»* — y son exactamente la razón "
        "por la que la Fase 3 existe: sin barrido dirigido nunca llegarían a "
        "auditarse.".replace(",", ".")
    )


# ─────────────────────────────────────────────
# Auditoría de picking
# ─────────────────────────────────────────────
with tab_pick:
    st.subheader("Muestra mensual de picking")
    st.markdown(
        "El plan asigna a Inventarios un rol de **auditor** sobre las posiciones de "
        "picking, con una muestra mensual priorizada por clase. Acá se genera esa "
        "muestra: en vez de tomarla al azar, se ordena por clase y valor, que es donde "
        "un error cuesta más."
    )

    if picking.empty:
        st.info("No hay líneas de picking en el layout para muestrear.")
    else:
        por_posicion = picking.groupby(["ubicacion", "rack", "clase_posicion"], as_index=False).agg(
            lineas=("id_especificacion", "count"), valor=("valor_linea", "sum")
        )

        # Las ubicaciones comodín no son huecos de picking: son puntos de
        # consolidación con decenas de ID de familias distintas. Auditarlas
        # por muestreo no tiene sentido —una sola se lleva la jornada— y
        # además distorsionan el tamaño de muestra. Se apartan.
        UMBRAL_COMODIN = 10
        comodines = por_posicion[por_posicion["lineas"] > UMBRAL_COMODIN]
        muestreables = por_posicion[por_posicion["lineas"] <= UMBRAL_COMODIN]

        c1, c2 = st.columns(2)
        tamano = c1.slider(
            "Posiciones a auditar este mes", min_value=50, max_value=400, value=175, step=25
        )
        # La semilla hace la muestra reproducible (dos personas obtienen la
        # misma lista) pero distinta cada mes. Sin esto, ordenar por valor
        # devolvería SIEMPRE las mismas posiciones y el universo no se
        # cubriría nunca, por más meses que pasaran.
        ciclo = c2.number_input(
            "Ciclo de muestreo",
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            help="Cambiá el número para obtener la muestra del siguiente ciclo.",
        )

        # Asignación por estrato: la clase A pesa más de lo que le tocaría
        # por tamaño, que es lo que pide el plan ("las de mayor clase
        # revisándose más seguido dentro del ciclo").
        CUOTA = {"A": 0.50, "B": 0.30, "C": 0.15, "Sin rotación": 0.05}
        partes = []
        for clase, cuota in CUOTA.items():
            estrato = muestreables[muestreables["clase_posicion"] == clase]
            n = min(len(estrato), int(round(tamano * cuota)))
            if n:
                partes.append(
                    estrato.sample(n=n, random_state=int(ciclo) * 100 + ORDEN_CLASE.index(clase))
                )
        muestra = (
            pd.concat(partes).sort_values("valor", ascending=False)
            if partes
            else muestreables.head(0)
        )

        universo = len(muestreables)
        c1, c2, c3 = st.columns(3)
        c1.metric("Universo muestreable", f"{universo:,}".replace(",", "."))
        c2.metric("Posiciones en la muestra", f"{len(muestra):,}".replace(",", "."))
        c3.metric("ID a verificar", f"{int(muestra['lineas'].sum()):,}".replace(",", "."))
        st.caption(
            f"Muestra **estratificada y aleatoria dentro de cada clase**, no por ranking "
            f"de valor: un ranking devolvería siempre las mismas posiciones y el universo "
            f"no se cubriría nunca. Reparto {int(CUOTA['A'] * 100)}/{int(CUOTA['B'] * 100)}/"
            f"{int(CUOTA['C'] * 100)}/{int(CUOTA['Sin rotación'] * 100)}% entre A/B/C/sin "
            "rotación, que sobre-representa a propósito las clases altas."
        )

        st.dataframe(
            muestra,
            hide_index=True,
            width="stretch",
            height=340,
            column_config={
                "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
                "rack": st.column_config.TextColumn("Rack", width="small"),
                "clase_posicion": st.column_config.TextColumn("Clase", width="small"),
                "lineas": st.column_config.NumberColumn("ID en la posición", format="%d"),
                "valor": st.column_config.NumberColumn("Valor", format="$%d"),
            },
        )
        st.download_button(
            "⬇️ Descargar la muestra en CSV",
            muestra.to_csv(index=False).encode("utf-8-sig"),
            file_name="muestra_auditoria_picking.csv",
            mime="text/csv",
        )

        st.divider()
        densos = por_posicion[por_posicion["lineas"] > 3]
        if len(densos):
            st.warning(
                f"**{len(densos)} posiciones de picking almacenan más de 3 ID** "
                f"({len(densos) / len(por_posicion) * 100:.0f}% de las ocupadas; el máximo "
                f"llega a {int(por_posicion['lineas'].max())}). El plan describe el estado "
                "actual como *«entre 1 y 3 ID»* por posición. Conviene revisarlo antes de "
                "presentarlo, y de paso es un insumo para el rediseño de picking que Bodega "
                "tiene previsto."
            )

        if len(comodines):
            st.markdown("##### Ubicaciones comodín, fuera del muestreo")
            st.markdown(
                f"**{len(comodines)} posiciones** concentran más de {UMBRAL_COMODIN} ID cada "
                f"una (hasta {int(comodines['lineas'].max())}), con familias mezcladas. No se "
                "comportan como huecos de picking sino como puntos de consolidación: "
                "auditarlas por muestreo no aplica —una sola se lleva la jornada— así que se "
                "apartan del universo muestreable y se listan para tratarlas aparte."
            )
            st.dataframe(
                comodines.sort_values("lineas", ascending=False),
                hide_index=True,
                width="stretch",
                column_config={
                    "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
                    "rack": st.column_config.TextColumn("Rack", width="small"),
                    "clase_posicion": st.column_config.TextColumn("Clase", width="small"),
                    "lineas": st.column_config.NumberColumn("ID en la posición", format="%d"),
                    "valor": st.column_config.NumberColumn("Valor", format="$%d"),
                },
            )
