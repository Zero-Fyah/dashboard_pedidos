"""
Conversión de horas a hora Colombia (UTC-5) para el dashboard.

Los timestamps que llegan de `pedidos.db` (`actualizado_en`, `ejecutado_en`,
`admin_actualizado_en`, `bochica_actualizado_en`, `estado_cambiado_en`) se
guardan en UTC ISO (scraper e `inventario/persistencia.py`). Mostrarlos tal
cual obliga al lector a restar 5 horas mentalmente cada vez.

Colombia opera en UTC-5 sin horario de verano desde 1993 — mismo offset fijo
que ya usa `dashboard/db.py` (`_HOY_CO`, AUD-B6) e `inventario/salud.py`
(`_hoy_colombia()`) para fechas. Este módulo es el equivalente para
**timestamps de reloj** (hora, no solo fecha): antes de esto, `pedidos.py` e
`inventario.py` formateaban el mismo patrón por separado, cada uno mostrando
UTC crudo etiquetado "UTC" (duplicado, y en el sentido equivocado).
"""

from __future__ import annotations

import datetime as dt

OFFSET_CO_H = 5
_TZ_COLOMBIA = dt.timezone(dt.timedelta(hours=-OFFSET_CO_H))

FORMATO_FECHA_HORA = "%Y-%m-%d %H:%M"


def a_hora_colombia(iso: str | None, fmt: str = FORMATO_FECHA_HORA) -> str:
    """Convierte un timestamp ISO UTC a hora Colombia formateada.

    Args:
        iso: Timestamp ISO, con o sin offset explícito. Si no trae zona
            horaria se asume UTC (es como lo escriben el scraper y
            `inventario/persistencia.py`).
        fmt: Formato de salida (`strftime`).

    Returns:
        El timestamp formateado en hora Colombia, o `"—"` si `iso` es None
        o vacío. Si el valor no es un ISO parseable, se devuelve tal cual
        (mismo criterio defensivo que reemplaza: no reventar el render por
        un dato inesperado).
    """
    if not iso:
        return "—"
    try:
        instante = dt.datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=dt.timezone.utc)
    return instante.astimezone(_TZ_COLOMBIA).strftime(fmt)
