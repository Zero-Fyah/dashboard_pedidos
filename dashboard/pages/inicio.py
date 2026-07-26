"""
Página de inicio.

Por diseño, esta página NO incluye métricas de negocio todavía: las
visualizaciones reales se irán agregando aquí (o en páginas nuevas) de
forma progresiva, a medida que se definan los indicadores que aporten
valor. Por ahora, sirve para confirmar que la identidad visual está
aplicada y para dejar listo un ejemplo del componente de tarjeta KPI.
"""

from __future__ import annotations

import streamlit as st

from components.cards import kpi_card

st.markdown('<p class="dp-breadcrumb">Dashboard / Inicio</p>', unsafe_allow_html=True)
st.title("Dashboard de Pedidos")
st.markdown(
    "Este espacio irá mostrando los indicadores del proyecto a medida que "
    "se definan. Debajo, un ejemplo del componente `kpi_card` ya con la "
    "paleta de colores aplicada — reemplaza la etiqueta y el valor por "
    "datos reales cuando estén listos."
)

st.write("")  # pequeño respiro vertical antes de las tarjetas

col1, col2, col3 = st.columns(3)
with col1:
    kpi_card("Ejemplo métrica", "—", color="teal")
with col2:
    kpi_card("Ejemplo métrica", "—", color="blue")
with col3:
    kpi_card("Ejemplo métrica", "—", color="warning")
