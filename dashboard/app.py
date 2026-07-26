"""
Punto de entrada de la app de Streamlit — Dashboard de Pedidos.

Se ejecuta con:
    streamlit run dashboard/app.py

Este archivo hace tres cosas, en orden:
1. Configura la página (debe ir antes que cualquier otro comando de Streamlit).
2. Aplica el tema visual global (theme.inject_global_css).
3. Registra el menú de navegación de la barra lateral.

Cómo agregar una nueva sección o página al menú
-------------------------------------------------
El diccionario que recibe `st.navigation()` define las secciones del
sidebar. Cada clave es el título de una sección (se muestra como grupo
desplegable) y cada valor es una lista de `st.Page`. Para sumar una
página nueva:

    "Inventario": [
        st.Page("pages/inventario.py", title="Stock", icon=":material/inventory_2:"),
    ],

No hace falta tocar el resto del archivo ni la lógica de navegación.

Nota sobre `pages/`: al usar `st.navigation()`, Streamlit **no** arma el menú
automático a partir del directorio `pages/` — el menú es exactamente el que se
declara abajo. Un archivo suelto en `pages/` no aparece hasta registrarlo acá.
"""

from __future__ import annotations

import streamlit as st

from theme import inject_global_css

st.set_page_config(
    page_title="Dashboard de Pedidos",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_global_css()


def _hay_tareas() -> bool:
    """Decide si la sección de Tareas aparece en el menú (DEC-047).

    Se oculta cuando no queda nada por hacer: ni inconsistencias detectadas
    en el sistema administrativo ni tareas manuales abiertas.

    **Nunca deja caer la app.** Ante cualquier error se devuelve True: un
    menú con una sección de más es molesto; un dashboard que no arranca
    porque falló un chequeo secundario, no.
    """
    try:
        from tareas_db import hay_manuales_pendientes

        from db import get_hallazgos

        return not get_hallazgos().empty or hay_manuales_pendientes()
    except Exception:
        return True


paginas: dict[str, list[st.Page]] = {
    "Principal": [
        st.Page(
            "pages/inicio.py",
            title="Inicio",
            icon=":material/home:",
            default=True,
        ),
    ],
    "Pedidos": [
        st.Page(
            "pages/pedidos.py",
            title="Consolidado",
            icon=":material/local_shipping:",
        ),
    ],
    "Inventario": [
        st.Page(
            "pages/inventario.py",
            title="Bodega vs. sistema",
            icon=":material/inventory_2:",
        ),
        st.Page(
            "pages/salud.py",
            title="Salud y cobertura",
            icon=":material/monitor_heart:",
        ),
    ],
    # A medida que el proyecto evolucione, se agregan aquí nuevas
    # secciones con sus páginas: basta con sumar un st.Page a este
    # diccionario, sin tocar el resto del archivo.
}

# DEC-047: la sección solo existe mientras haya algo que mostrar. Cuando
# se corrigen todas las inconsistencias en el sistema administrativo y no
# quedan tareas manuales abiertas, desaparece del menú.
if _hay_tareas():
    paginas["Tareas"] = [
        st.Page(
            "pages/tareas.py",
            title="Calidad de datos",
            icon=":material/checklist:",
        ),
    ]

pg = st.navigation(paginas, expanded=True)

pg.run()
