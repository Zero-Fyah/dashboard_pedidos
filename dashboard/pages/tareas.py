"""
Tareas — seguimiento de la depuración y estandarización de datos (DEC-046).

Único lugar donde se gestionan, priorizan y siguen las actividades de
limpieza de la información del sistema administrativo.

Se agrega, edita y borra en la misma tabla; los cambios se guardan al
presionar el botón. Los datos viven en `data/tareas.db`, separado de
`pedidos.db` porque son el único dato del proyecto que no se puede
regenerar re-scrapeando.
"""

import sqlite3

import streamlit as st
from tareas_db import CATEGORIAS, ESTADOS, PRIORIDADES, guardar_tareas, listar_tareas

st.markdown('<p class="dp-breadcrumb">Dashboard / Tareas</p>', unsafe_allow_html=True)
st.title("🧹 Tareas de calidad de datos")
st.markdown(
    "Actividades de limpieza, organización y estandarización de la información "
    "del sistema administrativo. La columna **Origen** apunta a la decisión "
    "donde el hallazgo está documentado y medido."
)

try:
    df = listar_tareas()
except sqlite3.OperationalError as e:
    st.error(f"No se pudo abrir la base de tareas ({e}). Recarga la página en unos segundos.")
    st.stop()

# ── Indicadores ────────────────────────────────────────────────────────────────
conteo = df["estado"].value_counts().to_dict()
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total", f"{len(df):,}")
k2.metric("Pendientes", f"{conteo.get('Pendiente', 0):,}")
k3.metric("En progreso", f"{conteo.get('En progreso', 0):,}")
k4.metric("Completadas", f"{conteo.get('Completada', 0):,}")

n_alta = int(((df["prioridad"] == "Alta") & (df["estado"] != "Completada")).sum())
if n_alta:
    st.caption(f"🔺 {n_alta} tarea(s) de prioridad alta sin completar.")

st.divider()

# ── Editor ─────────────────────────────────────────────────────────────────────
st.caption(
    "Editá cualquier celda, usá la última fila para agregar y el ícono de "
    "papelera para borrar. Los cambios no se aplican hasta guardar."
)

editado = st.data_editor(
    df,
    hide_index=True,
    num_rows="dynamic",
    key="editor_tareas",
    column_config={
        # id oculto: es la llave con la que se sincroniza, no algo que el
        # usuario deba ver ni tocar.
        "id": None,
        "titulo": st.column_config.TextColumn(
            "Tarea", width="large", required=True, help="Qué hay que hacer."
        ),
        "detalle": st.column_config.TextColumn(
            "Detalle", width="large", help="Contexto, cifras, por qué importa."
        ),
        "categoria": st.column_config.SelectboxColumn(
            "Categoría", options=CATEGORIAS, width="medium"
        ),
        "prioridad": st.column_config.SelectboxColumn(
            "Prioridad", options=PRIORIDADES, width="small"
        ),
        "estado": st.column_config.SelectboxColumn("Estado", options=ESTADOS, width="small"),
        "origen": st.column_config.TextColumn(
            "Origen", width="small", help="DEC/HAL donde está documentado el hallazgo."
        ),
    },
)

col_guardar, col_msg = st.columns([1, 4])
with col_guardar:
    if st.button("💾 Guardar cambios", type="primary"):
        try:
            nuevas, actualizadas, borradas = guardar_tareas(editado)
        except sqlite3.OperationalError as e:
            st.error(f"No se pudieron guardar los cambios ({e}). Intentá de nuevo.")
        else:
            partes = []
            if nuevas:
                partes.append(f"{nuevas} nueva(s)")
            if borradas:
                partes.append(f"{borradas} borrada(s)")
            partes.append(f"{actualizadas} actualizada(s)")
            st.success("Guardado: " + ", ".join(partes) + ".")
            st.rerun()

st.caption(
    "⚠️ Estas tareas viven en `data/tareas.db`, que está fuera del repositorio "
    "y no tiene respaldo automático — es el único dato del proyecto que no se "
    "puede regenerar volviendo a scrapear. Conviene respaldarlo aparte."
)
