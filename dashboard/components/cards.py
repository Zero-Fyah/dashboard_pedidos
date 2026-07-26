"""
Componentes de tarjetas reutilizables para el dashboard.

Cada función renderiza HTML ya estilizado según `theme.py`, para que
cualquier página nueva que se agregue reutilice el mismo look and feel
sin tener que reescribir CSS.
"""

from __future__ import annotations

import streamlit as st

from theme import (
    ACCENT_BLUE,
    ACCENT_TEAL,
    ACCENT_TEAL_LIGHT,
    STATUS_CRITICAL,
    STATUS_WARNING,
)

_ACCENT_COLORS = {
    "teal": ACCENT_TEAL,
    "blue": ACCENT_BLUE,
    "teal_light": ACCENT_TEAL_LIGHT,
    "warning": STATUS_WARNING,
    "critical": STATUS_CRITICAL,
}


def kpi_card(label: str, value: str, color: str = "teal") -> None:
    """
    Renderiza una tarjeta KPI: etiqueta pequeña + valor grande, con un
    acento de color a la izquierda tomado de la paleta del proyecto.

    Parameters
    ----------
    label:
        Texto descriptivo corto, p. ej. "Pedidos procesados".
    value:
        Valor a destacar como texto libre, p. ej. "128", "64%", "—".
        Se recibe como str para permitir cualquier formato (unidades,
        porcentajes, valores aún sin calcular, etc.).
    color:
        Una de "teal", "blue", "teal_light", "warning", "critical".
        Cualquier otro valor cae por defecto a "teal".
    """
    accent = _ACCENT_COLORS.get(color, ACCENT_TEAL)
    st.markdown(
        f"""
        <div class="dp-card" style="border-left: 3px solid {accent};">
            <div class="dp-card-label">{label}</div>
            <div class="dp-card-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
