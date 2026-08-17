"""
comun/arena.py — Demanda y punto de reorden de Arena por ciudad (DEC-119).

Primera fase posterior al núcleo de Arena (DEC-118): "cuánto hay hoy" ya
estaba resuelto (`inventario/arena.py::calcular_arena()`), esto responde
"cuándo se agota" por (código de barras, ciudad).

**Reusa `comun.reposicion.calcular_reposicion()` tal cual, sin tocarla.**
Esa función ya implementa toda la matemática de punto de reorden (demanda
× lead time + stock de seguridad, con `cv`/clase ABC) sobre una columna
`referencia` que trata como clave de agrupación opaca — no le importa qué
signifique. Acá la clave es compuesta (`codigo_barras + "__" + almacen`,
para lograr granularidad por ciudad, decisión del Arquitecto) y se separa
de vuelta por *merge*, nunca por `str.split()`, para no depender de que el
nombre de ninguna ciudad futura no contenga el separador. Arena no tiene
clasificación ABC-XYZ (excluida del pipeline principal, DEC-041), así que
se le pasa un `abc` vacío — `calcular_reposicion()` ya maneja ese caso con
gracia (usa `CV_DEFECTO`/`Z_SERVICIO_DEFECTO`).

**Las 3 modalidades de venta directa se suman en un solo pool de stock y
demanda** por (código de barras, ciudad) — decisión del Arquitecto: no hay
reserva formal que impida vender el inventario de una modalidad bajo otra,
así que sumarlas da la disponibilidad real. Respaldo y el hub de Yumbo NO
entran (no son modalidad de venta, mismo criterio que ya separa la tabla
núcleo del resto en `dashboard/pages/arena.py`).

**`CV_DEFECTO` (de `comun/reposicion.py`) puede no ser representativo de
Arena**: es la mediana del catálogo general con clasificación ABC-XYZ, y
Arena no tiene un `cv` propio medido. Limitación conocida, no un bug — ver
DEC-119.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from comun import ARENA_MODALIDADES_NUCLEO, VENTANA_DEMANDA_D, clasificar_modalidad_arena
from comun.reposicion import calcular_reposicion

# Colombia, UTC-5 sin horario de verano. Duplicado deliberado de 2 líneas:
# comun/ no puede depender de inventario/ (sería un ciclo) y dashboard/ no
# puede importar inventario/ (DEC-022) — no hay un lugar común más alto
# donde vivir esto sin romper la dirección de las capas.
_OFFSET_CO_H = 5


def _hoy_colombia() -> dt.date:
    return (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=_OFFSET_CO_H)).date()


def demanda_arena_por_ciudad(
    movimiento: pd.DataFrame,
    *,
    ventana_dias: int = VENTANA_DEMANDA_D,
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Demanda diaria por (codigo_barras, almacen) sobre los últimos N días.

    Args:
        movimiento: Líneas de venta de Arena con columnas `referencia`,
            `codigo_barras`, `almacen`, `cantidad_comprada`, `fecha`,
            `estado` — el resultado de `db.get_arena_movimiento()`.
        ventana_dias: Tamaño de la ventana. Por defecto `VENTANA_DEMANDA_D`
            (90 días), el mismo criterio que el resto del catálogo.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        Una fila por (`codigo_barras`, `almacen`) con `demanda_diaria`.
        Vacío si no hay movimiento o ninguna línea cae en la ventana.
    """
    columnas = ["codigo_barras", "almacen", "demanda_diaria"]
    if movimiento.empty:
        return pd.DataFrame(columns=columnas)

    m = movimiento.copy()
    m["cantidad_comprada"] = pd.to_numeric(m["cantidad_comprada"], errors="coerce")
    m["modalidad"] = m["referencia"].map(clasificar_modalidad_arena)
    m = m[m["modalidad"].isin(ARENA_MODALIDADES_NUCLEO)]
    # Una venta cancelada no es demanda real (mismo criterio que
    # inventario/salud.py::_demanda_por_referencia).
    m = m[m["estado"].str.lower() != "cancelado"]

    referencia_hoy = hoy or _hoy_colombia()
    desde = (referencia_hoy - dt.timedelta(days=ventana_dias)).isoformat()
    ventana = m[m["fecha"] >= desde]
    if ventana.empty:
        return pd.DataFrame(columns=columnas)

    demanda = ventana.groupby(["codigo_barras", "almacen"], as_index=False)[
        "cantidad_comprada"
    ].sum()
    demanda["demanda_diaria"] = demanda["cantidad_comprada"] / ventana_dias
    return demanda[columnas]


def alertas_quiebre_arena(
    inventario: pd.DataFrame,
    demanda: pd.DataFrame,
    *,
    lead_time_dias: int,
    dias_cobertura_objetivo: int,
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Punto de reorden y fecha de quiebre proyectada por (codigo_barras, ciudad).

    Args:
        inventario: `db.get_arena_inventario()` — necesita `codigo_barras`,
            `almacen`, `especificacion`, `inventario`, `modalidad`.
        demanda: Resultado de `demanda_arena_por_ciudad()`.
        lead_time_dias: Plazo de reposición/traslado. **Parámetro de
            negocio, no un dato medido** — Arena no tiene tabla de compras
            ni de traslados (mismo límite que documenta DEC-071 para el
            catálogo general).
        dias_cobertura_objetivo: Días de venta que se quiere tener encima
            al recibir el traslado.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        Una fila por (`codigo_barras`, `almacen`) con demanda > 0, con
        `fecha_quiebre_proyectada` = hoy + días de cobertura. Ordenado por
        esa fecha, ascendente (lo más urgente primero). Vacío si no hay
        demanda o inventario para cruzar.
    """
    columnas_salida = [
        "codigo_barras",
        "almacen",
        "especificacion",
        "nombre_comercial",
        "demanda_diaria",
        "disponible",
        "cv",
        "stock_seguridad",
        "punto_reorden",
        "cobertura_dias",
        "sugerido_pedir",
        "estado_reposicion",
        "fecha_quiebre_proyectada",
    ]
    if inventario.empty or demanda.empty:
        return pd.DataFrame(columns=columnas_salida)

    inv = inventario[inventario["modalidad"].isin(ARENA_MODALIDADES_NUCLEO)].copy()
    if inv.empty:
        return pd.DataFrame(columns=columnas_salida)

    # Las 3 modalidades núcleo se suman en un solo pool por (codigo_barras,
    # almacen) — decisión del Arquitecto, ver docstring del módulo.
    base = inv.groupby(["codigo_barras", "almacen"], as_index=False).agg(
        disponible=("inventario", "sum"),
        especificacion=("especificacion", "first"),
        nombre_comercial=("nombre_comercial", "first"),
    )
    base["clave"] = base["codigo_barras"].astype(str) + "__" + base["almacen"].astype(str)

    demanda_clave = demanda.copy()
    demanda_clave["clave"] = (
        demanda_clave["codigo_barras"].astype(str) + "__" + demanda_clave["almacen"].astype(str)
    )
    base = base.merge(demanda_clave[["clave", "demanda_diaria"]], on="clave", how="left")
    base["demanda_diaria"] = base["demanda_diaria"].fillna(0.0)

    salud_arena = base.rename(columns={"clave": "referencia"})[
        ["referencia", "demanda_diaria", "disponible"]
    ]
    # abc vacío a propósito: Arena no tiene ABC-XYZ (DEC-041) — calcular_reposicion
    # ya usa CV_DEFECTO/Z_SERVICIO_DEFECTO cuando abc no trae columna "nivel".
    repo = calcular_reposicion(
        salud_arena,
        pd.DataFrame(),
        lead_time_dias=lead_time_dias,
        dias_cobertura_objetivo=dias_cobertura_objetivo,
    )
    if repo.empty:
        return pd.DataFrame(columns=columnas_salida)

    mapa = base[["clave", "codigo_barras", "almacen", "especificacion", "nombre_comercial"]].rename(
        columns={"clave": "referencia"}
    )
    resultado = repo.merge(mapa, on="referencia", how="left").drop(columns="referencia")

    # calcular_reposicion ya descartó las filas con demanda_diaria == 0, así
    # que cobertura_dias siempre es finita acá.
    referencia_hoy = hoy or _hoy_colombia()
    resultado["fecha_quiebre_proyectada"] = pd.to_datetime(referencia_hoy) + pd.to_timedelta(
        resultado["cobertura_dias"].clip(lower=0), unit="D"
    )
    return resultado.sort_values("fecha_quiebre_proyectada")[columnas_salida]
