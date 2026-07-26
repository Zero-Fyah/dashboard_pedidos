"""
Tareas — seguimiento de la depuración y estandarización de datos (DEC-046/047).

Dos tipos de tarea conviven acá:

- **Automáticas**: inconsistencias que un detector mide sobre los datos
  actuales (`inventario/hallazgos.py`). Se recalculan en cada corrida del
  scheduler, justo después de descargar el Excel del sistema
  administrativo, y **desaparecen solas** cuando el detector deja de
  encontrar casos. Al seleccionarlas se despliega el listado concreto.
- **Manuales**: las que requieren criterio o son trabajo del proyecto, no
  del sistema administrativo. Se editan a mano y viven en
  `data/tareas.db`.
"""

import sqlite3

import streamlit as st
from tareas_db import CATEGORIAS, ESTADOS, PRIORIDADES, guardar_tareas, listar_tareas

from db import get_detalle_hallazgo, get_hallazgos

st.markdown('<p class="dp-breadcrumb">Dashboard / Tareas</p>', unsafe_allow_html=True)
st.title("🧹 Tareas de calidad de datos")

try:
    hallazgos = get_hallazgos()
    manuales = listar_tareas()
except sqlite3.OperationalError as e:
    st.error(f"No se pudieron leer las tareas ({e}). Recarga la página en unos segundos.")
    st.stop()

# ── Indicadores ────────────────────────────────────────────────────────────────
pendientes_manuales = manuales[manuales["estado"] != "Completada"]
k1, k2, k3 = st.columns(3)
k1.metric("Inconsistencias detectadas", f"{len(hallazgos):,}")
k2.metric("Casos por corregir", f"{int(hallazgos['cantidad'].sum()):,}")
k3.metric("Tareas manuales abiertas", f"{len(pendientes_manuales):,}")

if hallazgos.empty:
    st.success(
        "✅ Sin inconsistencias detectadas en la última corrida. Los detectores "
        "no encontraron casos en el sistema administrativo."
    )
else:
    medido = hallazgos["medido_en"].max()
    st.caption(
        f"Detectado automáticamente en la última descarga del sistema "
        f"administrativo ({medido}). Se recalcula en cada corrida del scheduler."
    )

# ── Automáticas ────────────────────────────────────────────────────────────────
if not hallazgos.empty:
    st.subheader("Inconsistencias detectadas")

    vista = hallazgos[["titulo", "categoria", "prioridad", "cantidad", "unidad", "origen"]]
    seleccion = st.dataframe(
        vista,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="tabla_hallazgos",
        column_config={
            "titulo": st.column_config.TextColumn("Inconsistencia", width="large"),
            "categoria": st.column_config.TextColumn("Categoría", width="medium"),
            "prioridad": st.column_config.TextColumn("Prioridad", width="small"),
            "cantidad": st.column_config.NumberColumn("Casos", format="%d", width="small"),
            "unidad": st.column_config.TextColumn("Unidad", width="small"),
            "origen": st.column_config.TextColumn("Origen", width="small"),
        },
    )

    # ── Detalle de la tarea seleccionada ───────────────────────────────────────
    filas_sel = seleccion.selection.rows
    if not filas_sel:
        st.info("Seleccioná una fila para ver el listado de casos concretos.")
    else:
        fila = hallazgos.iloc[filas_sel[0]]
        st.divider()
        st.subheader(f"Detalle — {fila['titulo']}")
        st.markdown(fila["explicacion"])

        detalle = get_detalle_hallazgo(str(fila["clave"]))
        if detalle.empty:
            st.info("Sin detalle disponible para esta inconsistencia.")
        else:
            st.caption(
                f"{len(detalle):,} fila(s). Corregí los casos en el sistema "
                "administrativo: en la próxima descarga la cifra baja sola, y "
                "cuando llegue a cero la tarea desaparece de esta lista."
            )
            st.dataframe(detalle, hide_index=True)
            st.download_button(
                "⬇️ Descargar el listado (CSV)",
                detalle.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{fila['clave']}.csv",
                mime="text/csv",
                help="Para trabajar la corrección fuera del dashboard.",
            )

st.divider()

# ── Manuales ───────────────────────────────────────────────────────────────────
st.subheader("Tareas manuales")
st.caption(
    "Las que no se pueden detectar midiendo los datos: requieren criterio o son "
    "trabajo del proyecto. Editá cualquier celda, usá la última fila para "
    "agregar y la papelera para borrar. Los cambios no se aplican hasta guardar."
)

editado = st.data_editor(
    manuales,
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
    "⚠️ Las tareas manuales viven en `data/tareas.db`, fuera del repositorio y "
    "sin respaldo automático — es el único dato del proyecto que no se puede "
    "regenerar volviendo a scrapear. Conviene respaldarlo aparte."
)
