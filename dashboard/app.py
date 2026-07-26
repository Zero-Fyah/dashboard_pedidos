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

pg = st.navigation(
    {
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
        "Tareas": [
            st.Page(
                "pages/tareas.py",
                title="Calidad de datos",
                icon=":material/checklist:",
            ),
        ],
        # A medida que el proyecto evolucione, se agregan aquí nuevas
        # secciones con sus páginas. La próxima prevista es la de
        # inventario, que ya tiene su backend listo (DEC-043: lee de
        # v_inventario_comparacion / v_inventario_anomalias /
        # v_inventario_corridas):
        # "Inventario": [
        #     st.Page("pages/inventario.py", title="Bodega vs. sistema",
        #             icon=":material/inventory_2:"),
        # ],
    },
    expanded=True,  # secciones desplegadas por defecto en el sidebar
)

pg.run()
