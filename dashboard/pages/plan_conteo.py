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

Desde DEC-058 el ciclo se cierra: la hoja de conteo sale de acá, se llena en
campo, vuelve como Excel a `data/conteos/` y el scheduler la ingiere. Con
eso sí hay **IRA** y cobertura reales, calculados sobre conteos de verdad.

**Lo que sigue sin calcularse, a propósito:** el score de confiabilidad y
los estados de madurez de la ubicación. Ambos exigen una serie de conteos
*coincidentes consecutivos* por línea; con el historial recién arrancando no
hay con qué, y publicarlos vacíos o inventados sería peor que no tenerlos.
Se habilitan solos cuando el historial dé.
"""

import io
import sqlite3

import conteos_io
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from comun import (
    CAUSAS_DISCREPANCIA,
    COLUMNAS_CONTEO_REQUERIDAS,
    COLUMNAS_HOJA_CONTEO,
    META_ANTIGUEDAD_CONTEO_DIAS,
    META_IRA_POR_CLASE,
)
from db import (
    get_conformidad,
    get_conteos,
    get_conteos_archivos,
    get_inventario_posiciones,
    get_inventario_ubicaciones,
    get_ira,
    get_ira_periodo,
)
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

tab_dim, tab_cola, tab_reg, tab_pick = st.tabs(
    ["Dimensionamiento", "Cola de conteo", "Conteos e IRA", "Auditoría de picking"]
)


# ─────────────────────────────────────────────
# Dimensionamiento
# ─────────────────────────────────────────────
with tab_dim:
    st.subheader("El trabajo real de la Fase 3")

    lineas_alt = len(altura)
    ocupadas = altura["ubicacion"].nunique()
    ab = altura["clase"].str.startswith(("A", "B")).sum()
    heredadas = (altura["origen_clase"] == "Referencia").sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Líneas SKU-posición en altura", f"{lineas_alt:,}".replace(",", "."))
    c2.metric("Posiciones ocupadas", f"{ocupadas:,}".replace(",", "."))
    c3.metric("Líneas por posición ocupada", f"{lineas_alt / ocupadas:.2f}" if ocupadas else "—")
    c4.metric(
        "Son clase A o B",
        f"{ab / lineas_alt * 100:.1f}%" if lineas_alt else "—",
        help="Incluye las clases heredadas de la referencia.",
    )

    st.caption(
        "La unidad es la línea SKU-posición: un ID dentro de una posición. "
        "Es lo que efectivamente se cuenta cuando alguien sube a verificar una estiba. "
        "**{}** de esas líneas ({:.0f}%) heredan la clase de su referencia porque el ID "
        "no es atribuible: el código de barras no es único por ID, que es el riesgo "
        "crítico que el propio plan señala.".format(
            f"{heredadas:,}".replace(",", "."), heredadas / lineas_alt * 100 if lineas_alt else 0
        )
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
# Conteos e IRA
# ─────────────────────────────────────────────
with tab_reg:
    st.subheader("1 · Sacar la hoja de conteo")
    st.markdown(
        "Formato del **Anexo A** del plan. **No lleva la cantidad del sistema**: un "
        "conteo que muestra lo esperado deja de ser un conteo, porque quien cuenta "
        "ancla en ese número y confirma en vez de verificar."
    )

    h1, h2 = st.columns(2)
    cuantas = h1.number_input(
        "Posiciones a incluir", min_value=10, max_value=500, value=40, step=10
    )
    quien = h2.text_input("Asignada a", placeholder="Nombre del asistente")

    # Se corta por posición, no por línea: la hoja no puede terminar a mitad
    # de una estiba.
    pendientes = altura.sort_values("prioridad")
    ubic = pendientes["ubicacion"].drop_duplicates().head(int(cuantas))
    seleccion = pendientes[pendientes["ubicacion"].isin(set(ubic))]
    # La hoja se arma desde el contrato compartido (`comun/`), no desde
    # una lista local: el pipeline valida contra esas mismas columnas.
    hoja = pd.DataFrame(columns=list(COLUMNAS_HOJA_CONTEO))
    hoja["ubicacion"] = seleccion["ubicacion"].to_numpy()
    hoja["id_especificacion"] = seleccion["id_especificacion"].to_numpy()
    hoja["referencia"] = seleccion["referencia"].to_numpy()
    hoja["fecha"] = pd.Timestamp.today().date().isoformat()
    hoja["actividad_origen"] = "Conteo dirigido"
    hoja["contado_por"] = quien or pd.NA

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        hoja.to_excel(writer, index=False, sheet_name="conteo")
    st.download_button(
        f"⬇️ Descargar hoja de conteo ({len(ubic)} posiciones · {len(hoja)} líneas)",
        buffer.getvalue(),
        file_name=f"hoja_conteo_{pd.Timestamp.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.caption(
        "Llenar la columna **cantidad_contada** y volver a subirla en el paso 2. "
        "Las demás columnas son opcionales."
    )
    with st.expander("La columna «causa» — qué poner"):
        st.markdown(
            "Se llena **solo cuando la cantidad no coincide**, y con uno de estos "
            "valores exactos. Es una lista cerrada a propósito: en texto libre, "
            "*«error de despacho»*, *«mal despachado»* y *«despacho»* serían tres "
            "causas distintas y el análisis mensual no podría agrupar nada.\n\n"
            + "\n".join(f"- `{c}`" for c in CAUSAS_DISCREPANCIA)
            + "\n\nPuede llenarse después, cuando se investigue: se corrige la hoja "
            "y se vuelve a subir con el mismo nombre."
        )

    st.divider()
    st.subheader("2 · Subir la hoja llena")

    subidas = st.file_uploader(
        "Arrastrá acá el Excel del conteo",
        type=["xlsx"],
        accept_multiple_files=True,
        help="Se pueden subir varias hojas a la vez.",
    )

    if subidas:
        for archivo in subidas:
            st.markdown(f"**{archivo.name}**")
            try:
                revision = pd.read_excel(io.BytesIO(archivo.getvalue()))
                revision.columns = [str(c).strip().lower() for c in revision.columns]
            except Exception as e:  # noqa: BLE001 — el archivo lo eligió el usuario
                st.error(f"No se pudo leer el archivo: {e}")
                continue

            # Se valida ANTES de guardar. Si no, el error recién aparecería
            # cuando el scheduler corriera, hasta una hora después, y para
            # entonces quien contó ya se fue.
            faltantes = [c for c in COLUMNAS_CONTEO_REQUERIDAS if c not in revision.columns]
            if faltantes:
                st.error(
                    f"Le faltan columnas obligatorias: **{', '.join(faltantes)}**. "
                    "El archivo se rechazaría entero, así que no se guarda. "
                    "Descargá la hoja del paso 1 y volvé a llenarla."
                )
                continue

            cantidades = pd.to_numeric(revision["cantidad_contada"], errors="coerce")
            sin_contar = int(cantidades.isna().sum())
            claves = set(zip(df["ubicacion"], df["id_especificacion"].astype(str), strict=True))
            en_sistema = sum(
                (str(u).strip().upper(), str(i).strip()) in claves
                for u, i in zip(revision["ubicacion"], revision["id_especificacion"], strict=True)
            )

            v1, v2, v3 = st.columns(3)
            v1.metric("Filas", len(revision))
            v2.metric("Con cantidad", len(revision) - sin_contar)
            v3.metric("Cruzan con el sistema", en_sistema)

            if sin_contar:
                st.warning(
                    f"**{sin_contar} filas sin cantidad.** Se descartan al ingerir: si la "
                    "posición se revisó y estaba vacía, va **0**, no en blanco — no es lo mismo "
                    "«no había nada» que «no se contó»."
                )
            if en_sistema < len(revision):
                st.info(
                    f"{len(revision) - en_sistema} filas no cruzan con ninguna línea del "
                    "sistema. Se ingieren igual y salen como **sobrante** con cantidad "
                    "esperada cero, que es lo correcto si apareció producto donde no debía."
                )
            if conteos_io.existe(archivo.name):
                st.warning(
                    f"Ya existe una hoja llamada `{archivo.name}`. Guardarla la **reemplaza** "
                    "— que es lo correcto si estás corrigiendo esa misma hoja."
                )

            if st.button(f"💾 Guardar «{archivo.name}»", key=f"guardar_{archivo.name}"):
                try:
                    destino = conteos_io.guardar(archivo.name, archivo.getvalue())
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.success(
                        f"Guardada como `{destino.name}`. El scheduler la ingiere en su "
                        "próxima corrida (cada hora) y el IRA se actualiza solo."
                    )
                    st.cache_data.clear()

    with st.expander("¿Y si una hoja se cargó mal?"):
        st.markdown(
            "La carpeta manda: lo que está en `data/conteos/` es lo que cuenta.\n\n"
            "- **Deshacer una hoja entera** → el botón *Anular* en la lista de hojas, "
            "al final de esta pestaña. Sale del cálculo y **el archivo no se borra**.\n"
            "- **Corregir una fila** → editar el Excel y volver a subirlo con el mismo "
            "nombre. El valor nuevo reemplaza al anterior, no se suma.\n"
            "- **Recontar la misma posición otro día** → hoja nueva. Los dos conteos "
            "conviven, que es lo que necesita la doble verificación.\n\n"
            "⚠️ `data/conteos/` es el único original de los conteos y no está "
            "versionada: conviene respaldarla aparte."
        )

    st.divider()
    st.subheader("3 · Resultado")

    try:
        conteos = get_conteos()
    except sqlite3.OperationalError:
        conteos = pd.DataFrame()

    if conteos.empty:
        st.info(
            "Todavía no se ha ingerido ningún conteo. Apenas aparezca el primer archivo "
            "en `data/conteos/`, acá salen el IRA por clase y la cobertura."
        )
    else:
        ira = get_ira()
        global_ = ira[ira["clase"] == "Global"].iloc[0]

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("IRA global", f"{global_['ira']:.1f}%")
        k2.metric("Líneas contadas", f"{int(global_['contadas']):,}".replace(",", "."))
        k3.metric(
            "Posiciones auditadas",
            f"{conteos['ubicacion'].nunique():,}".replace(",", "."),
        )
        cobertura = conteos["ubicacion"].nunique() / max(altura["ubicacion"].nunique(), 1) * 100
        k4.metric("Cobertura de altura", f"{cobertura:.1f}%")

        st.markdown("##### IRA por clase, contra la meta del plan")
        tabla = ira[ira["clase"] != "Global"].copy()
        tabla["Meta"] = tabla["clase"].map(META_IRA_POR_CLASE)
        tabla["Cumple"] = [
            "✅" if pd.notna(m) and v >= m else ("—" if pd.isna(m) else "❌")
            for v, m in zip(tabla["ira"], tabla["Meta"], strict=True)
        ]
        st.dataframe(
            tabla.rename(
                columns={
                    "clase": "Clase",
                    "contadas": "Contadas",
                    "exactas": "Exactas",
                    "ira": "IRA %",
                    "ultima_fecha": "Último conteo",
                }
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "«Exacta» es coincidencia perfecta **o dentro de la tolerancia de su clase** "
            "(A ±1%, B ±2%, C ±5%), según la sección 4 del plan. Las metas son las de "
            "su sección 12."
        )

        # ── Evolución del IRA ──────────────────────────────────────
        serie = get_ira_periodo()
        if len(serie["periodo"].unique()) > 1:
            st.markdown("##### Cómo evoluciona la exactitud")
            fig_ira = go.Figure()
            colores = dict(zip(["A", "B", "C"], GRAFICO_SERIES, strict=False))
            for clase in ["Global", "A", "B", "C"]:
                datos = serie[serie["clase"] == clase]
                if datos.empty:
                    continue
                fig_ira.add_trace(
                    go.Scatter(
                        x=datos["periodo"],
                        y=datos["ira"],
                        mode="lines+markers",
                        name=clase,
                        line={
                            "color": colores.get(clase, TEXT_SECONDARY),
                            "width": 3 if clase == "Global" else 2,
                            "dash": "solid" if clase == "Global" else "dot",
                        },
                        hovertemplate="%{x}<br>IRA %{y:.1f}%<extra>" + clase + "</extra>",
                    )
                )
            fig_ira.update_layout(
                height=300,
                margin={"l": 10, "r": 10, "t": 20, "b": 10},
                paper_bgcolor=BG_DEEP,
                plot_bgcolor=BG_DEEP,
                font={"color": TEXT_PRIMARY},
                legend={"orientation": "h", "y": -0.25, "font": {"color": TEXT_SECONDARY}},
                xaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "type": "category"},
                yaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "title": "IRA %"},
            )
            st.plotly_chart(fig_ira, width="stretch")
        elif len(serie):
            st.caption(
                f"La evolución aparece con el segundo período contado (hoy hay "
                f"{len(serie['periodo'].unique())})."
            )

        # ── Pareto de causas ───────────────────────────────────────
        st.markdown("##### Por qué se producen las diferencias")
        con_causa = conteos[(conteos["exacta"] == 0) & conteos["causa"].notna()]
        malas_total = int((conteos["exacta"] == 0).sum())
        if malas_total:
            con_causa_pct = len(con_causa) / malas_total * 100
            st.metric(
                "Discrepancias con causa identificada",
                f"{con_causa_pct:.0f}%",
                delta="Meta: 80%" if con_causa_pct < 80 else "Cumple la meta",
                delta_color="inverse" if con_causa_pct < 80 else "normal",
            )
        if len(con_causa):
            pareto = (
                con_causa.groupby("causa", as_index=False)
                .agg(casos=("causa", "size"), unidades=("diferencia", lambda s: s.abs().sum()))
                .sort_values("casos", ascending=False)
            )
            fig_causa = go.Figure(
                go.Bar(
                    x=pareto["casos"],
                    y=pareto["causa"],
                    orientation="h",
                    marker={"color": GRAFICO_SERIES[0]},
                    hovertemplate="%{y}<br>%{x} discrepancias<extra></extra>",
                )
            )
            fig_causa.update_layout(
                height=60 * len(pareto) + 120,
                margin={"l": 10, "r": 10, "t": 20, "b": 10},
                paper_bgcolor=BG_DEEP,
                plot_bgcolor=BG_DEEP,
                font={"color": TEXT_PRIMARY},
                xaxis={"title": "Discrepancias", "gridcolor": GRAFICO_GRID, "automargin": True},
                yaxis={"gridcolor": GRAFICO_GRID, "automargin": True, "autorange": "reversed"},
            )
            st.plotly_chart(fig_causa, width="stretch")
            st.caption(
                "Separar faltantes de sobrantes antes de buscar el patrón acota la "
                "búsqueda: un faltante recurrente apunta a merma, daño no registrado o "
                "error de despacho; un sobrante, a mala ubicación o error de etiquetado."
            )
        elif malas_total:
            st.info(
                f"Hay **{malas_total} discrepancias sin causa asignada**. La causa se "
                "llena en la columna `causa` de la hoja y puede completarse después de "
                "investigar: se corrige el Excel y se vuelve a subir."
            )

        # ── Conformidad por tipo ───────────────────────────────────
        conformidad = get_conformidad()
        if len(conformidad):
            st.markdown("##### Conformidad por posición")
            st.markdown(
                "Se mide **por posición, no por línea**: una posición con cinco líneas "
                "y una sola mal no es «80% conforme», es una posición no conforme. Es "
                "como el plan define la *Tasa de Conformidad en Auditoría de Picking*, "
                "que se le reporta a Bodega."
            )
            st.dataframe(
                conformidad.rename(
                    columns={
                        "tipo": "Tipo de ubicación",
                        "posiciones": "Auditadas",
                        "conformes": "Sin discrepancia",
                        "conformidad": "Conformidad %",
                        "ultima_fecha": "Última auditoría",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        # ── Antigüedad del último conteo ───────────────────────────
        try:
            posiciones = get_inventario_posiciones()
        except sqlite3.OperationalError:
            posiciones = pd.DataFrame()
        if len(posiciones) and posiciones["dias_desde_conteo"].notna().any():
            st.markdown("##### Antigüedad del último conteo")
            contadas = posiciones[posiciones["dias_desde_conteo"].notna()]
            clase_a = contadas[contadas["clase_posicion"].astype(str).str.startswith("A")]
            a1, a2, a3 = st.columns(3)
            a1.metric("Posiciones con algún conteo", f"{len(contadas):,}".replace(",", "."))
            a2.metric("Antigüedad mediana", f"{contadas['dias_desde_conteo'].median():.0f} días")
            if len(clase_a):
                media_a = clase_a["dias_desde_conteo"].mean()
                a3.metric(
                    "Clase A — antigüedad media",
                    f"{media_a:.0f} días",
                    delta=f"Meta: <{META_ANTIGUEDAD_CONTEO_DIAS}",
                    delta_color="normal" if media_a < META_ANTIGUEDAD_CONTEO_DIAS else "inverse",
                )
            st.caption(
                "Solo cuenta posiciones **con al menos un conteo**. Las que nunca se "
                "contaron no entran como «hace mucho»: no son un número de la misma "
                "escala, y meterlas arruinaría el promedio. Su seguimiento es la "
                "cobertura, no la antigüedad."
            )

        st.markdown("##### Discrepancias por resolver")
        malas = conteos[conteos["exacta"] == 0].sort_values("diferencia", key=abs, ascending=False)
        if malas.empty:
            st.success("Todos los conteos ingeridos cayeron dentro de tolerancia.")
        else:
            st.dataframe(
                malas[
                    [
                        "fecha",
                        "ubicacion",
                        "id_especificacion",
                        "clase",
                        "cantidad_sistema",
                        "cantidad_contada",
                        "diferencia",
                        "hallazgo",
                        "contado_por",
                    ]
                ],
                hide_index=True,
                width="stretch",
                height=320,
                column_config={
                    "fecha": st.column_config.TextColumn("Fecha", width="small"),
                    "ubicacion": st.column_config.TextColumn("Ubicación", width="small"),
                    "id_especificacion": st.column_config.TextColumn("ID", width="medium"),
                    "clase": st.column_config.TextColumn("Clase", width="small"),
                    "cantidad_sistema": st.column_config.NumberColumn("Sistema", format="%d"),
                    "cantidad_contada": st.column_config.NumberColumn("Contado", format="%d"),
                    "diferencia": st.column_config.NumberColumn("Dif.", format="%d"),
                    "hallazgo": st.column_config.TextColumn("Hallazgo", width="small"),
                    "contado_por": st.column_config.TextColumn("Contó", width="medium"),
                },
            )

    st.divider()
    st.markdown("##### Hojas registradas")
    try:
        hojas = get_conteos_archivos()
    except sqlite3.OperationalError:
        hojas = pd.DataFrame()
    if hojas.empty:
        st.caption("Ninguna hoja ingerida todavía.")
    else:
        activas = set(conteos["archivo"]) if not conteos.empty else set()
        hojas = hojas.assign(
            estado=["✅ Activa" if a in activas else "🚫 Anulada" for a in hojas["archivo"]]
        )
        st.dataframe(
            hojas[["archivo", "estado", "filas", "primera_vez", "visto_en"]],
            hide_index=True,
            width="stretch",
            column_config={
                "archivo": st.column_config.TextColumn("Archivo", width="medium"),
                "estado": st.column_config.TextColumn("Estado", width="small"),
                "filas": st.column_config.NumberColumn("Filas", format="%d"),
                "primera_vez": st.column_config.TextColumn("Primera vez", width="medium"),
                "visto_en": st.column_config.TextColumn("Visto por última vez", width="medium"),
            },
        )
        st.caption(
            "Una hoja anulada **queda registrada acá para siempre**: sacarla de la "
            "carpeta la excluye del cálculo, no borra que existió."
        )

        anulables = conteos_io.listar_activas()
        if anulables:
            a1, a2 = st.columns([3, 1])
            elegida = a1.selectbox("Anular una hoja", anulables, key="anular_hoja")
            if a2.button("🚫 Anular", key="btn_anular"):
                try:
                    destino = conteos_io.anular(elegida)
                except FileNotFoundError:
                    st.error(f"`{elegida}` ya no está activa.")
                else:
                    st.success(
                        f"Movida a `anulados/{destino.name}`. Sale del cálculo en la "
                        "próxima corrida; el archivo sigue en disco."
                    )
                    st.cache_data.clear()


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
