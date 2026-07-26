"""
Identidad visual del Dashboard de Pedidos.

Este módulo centraliza toda la paleta de colores y expone `inject_global_css()`,
que debe llamarse una única vez desde `app.py` (el punto de entrada) para que
el tema se aplique a toda la aplicación, sin importar cuántas páginas se
agreguen en el futuro.

No hardcodear colores en otros archivos: siempre importar las constantes
de este módulo para mantener consistencia visual en todo el proyecto.
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Fondos
# ---------------------------------------------------------------------------
BG_PRIMARY_START = "#071C2E"  # Navy oscuro — inicio del degradado
BG_PRIMARY_END = "#0A2540"  # Navy medio — fin del degradado
BG_DEEP = "#051520"  # Navy profundo — sidebar y tarjetas/paneles

# ---------------------------------------------------------------------------
# Acentos
# ---------------------------------------------------------------------------
ACCENT_TEAL = "#1D9E75"  # Color principal — KPIs positivos, éxito
ACCENT_TEAL_LIGHT = "#5DCAA5"  # Teal secundario — hover, elementos suaves
ACCENT_BLUE = "#185FA5"  # Azul — gráficos secundarios, barras
ACCENT_BLUE_LIGHT = "#85B7EB"  # Azul claro — texto sobre azul, enlaces

# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------
TEXT_PRIMARY = "#FFFFFF"  # Títulos y valores destacados
TEXT_SECONDARY = "rgba(255,255,255,0.4)"  # Etiquetas y descripciones
TEXT_TERTIARY = "rgba(255,255,255,0.25)"  # Pie de página y metadatos
TEXT_TEAL_LABEL = "rgba(29,158,117,0.7)"  # Etiquetas de métricas en teal

# ---------------------------------------------------------------------------
# Estados
# ---------------------------------------------------------------------------
STATUS_SUCCESS = "#1D9E75"  # Estado correcto
STATUS_WARNING = "#E0A458"  # Advertencia
STATUS_CRITICAL = "#C4574B"  # Estado crítico (terracota, no rojo puro)

# ---------------------------------------------------------------------------
# Estructurales
# ---------------------------------------------------------------------------
BORDER_SUBTLE = "rgba(255,255,255,0.06)"  # Líneas divisorias y cuadrículas
BORDER_TEAL_ACCENT = "#1D9E75"  # Línea de acento (3px)
DECORATION_OPACITY = 0.06  # Opacidad de elementos decorativos de fondo

# ---------------------------------------------------------------------------
# Paleta categórica para gráficos (DEC-048)
# ---------------------------------------------------------------------------
# Los acentos de arriba están pensados para texto y bordes; como series de un
# gráfico sobre `BG_DEEP` no pasan las verificaciones de accesibilidad:
# `ACCENT_BLUE` queda en 2,67:1 de contraste (mínimo 3:1) y `STATUS_WARNING`
# se sale de la banda de luminosidad del modo oscuro (L 0,761 contra un
# máximo de 0,67).
#
# Estos tres se ajustaron hasta pasar las cinco verificaciones contra la
# superficie real (`#051520`): banda de luminosidad, piso de croma,
# separación para daltonismo (ΔE 15,4 deutan), piso de visión normal
# (ΔE 17,4) y contraste. El teal es el mismo `ACCENT_TEAL`, así que la
# identidad visual no cambia.
#
# **Se asignan en orden fijo, nunca cíclico.** Una cuarta serie no se
# inventa: se agrupa en "Otros" o se separa en gráficos chicos.
GRAFICO_SERIES = ("#1D9E75", "#4A90D9", "#BE8034")

# Grilla y ejes recesivos: la data manda, el andamiaje acompaña.
GRAFICO_GRID = "rgba(255,255,255,0.06)"

# Constantes de layout reutilizables (no forman parte de la paleta original,
# pero se centralizan aquí para mantener consistencia en tarjetas y grillas)
CARD_RADIUS = "12px"
CARD_PADDING = "20px"
GRID_GAP = "20px"


def inject_global_css() -> None:
    """
    Inyecta el CSS que aplica la identidad visual a toda la app: fondo con
    degradado navy, sidebar en navy profundo, tipografía en blanco/opacidades
    y una clase `.dp-card` reutilizable para las futuras tarjetas de KPIs
    y gráficos.

    Llamar una sola vez, en `app.py`, inmediatamente después de
    `st.set_page_config(...)`.
    """
    st.markdown(
        f"""
        <style>
        /* Fondo general con degradado navy */
        [data-testid="stAppViewContainer"] {{
            background: linear-gradient(
                180deg, {BG_PRIMARY_START} 0%, {BG_PRIMARY_END} 100%
            );
        }}
        [data-testid="stHeader"] {{
            background: transparent;
        }}

        /* Sidebar en navy profundo */
        [data-testid="stSidebar"] {{
            background-color: {BG_DEEP};
            border-right: 1px solid {BORDER_SUBTLE};
        }}
        [data-testid="stSidebarNav"] span {{
            color: {TEXT_SECONDARY};
        }}
        /* Página activa en el menú: acento teal a la izquierda */
        [data-testid="stSidebarNav"] a[aria-current="page"] {{
            background-color: rgba(29, 158, 117, 0.12);
            border-left: 3px solid {ACCENT_TEAL};
        }}
        [data-testid="stSidebarNav"] a[aria-current="page"] span {{
            color: {TEXT_PRIMARY} !important;
        }}

        /* Tipografía base */
        [data-testid="stAppViewContainer"] p {{
            color: {TEXT_SECONDARY};
        }}
        h1, h2, h3, h4 {{
            color: {TEXT_PRIMARY} !important;
        }}

        /* Breadcrumb tipo "Dashboard / Inicio" */
        .dp-breadcrumb {{
            color: {ACCENT_TEAL};
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 0.25rem;
        }}

        /* Tarjeta base reutilizable para KPIs y gráficos */
        .dp-card {{
            background-color: {BG_DEEP};
            border: 1px solid {BORDER_SUBTLE};
            border-radius: {CARD_RADIUS};
            padding: {CARD_PADDING};
            height: 100%;
        }}
        .dp-card-label {{
            color: {TEXT_SECONDARY};
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.4rem;
        }}
        .dp-card-value {{
            color: {TEXT_PRIMARY};
            font-size: 2rem;
            font-weight: 700;
            line-height: 1.2;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
