"""
arena.py — Inventario de la categoría Arena por código de barras, modalidad
y ciudad (DEC-118, módulo 1 del sistema analítico de Arenas).

`inventario/normalizador.py::filtrar_alcance_admin()` excluye la categoría
Arena del pipeline principal a propósito (DEC-041: se recibe por peso, vive
100% fuera del layout de picking). Este módulo lee el mismo
`admin_inventario.xlsx` pero **antes** de ese filtro — el catálogo completo,
sin recortar a Bogotá — porque acá la ciudad es el eje del análisis, no algo
que se excluye.

**Un código de barras identifica el producto físico (aroma/peso), no la
modalidad.** El mismo barcode aparece en hasta 29 filas del Excel — una por
cada combinación (referencia comercial × almacén). La modalidad
(Unidades/Tonelada/Corporativo/Respaldo/Yumbo-hub) sale de
`comun.clasificar_modalidad_arena()` sobre `Referencia del producto.`,
verificada fila por fila contra el dato real, no inferida por texto salvo
Corporativo (prefijo `CORPORATIVO`, abierto a ciudades nuevas).

**`producto_activo` es contraintuitivo, confirmado con el Arquitecto:**
`'Fue'` = activo para venta directa, `'No hay'` = inactivo para venta
directa. Corporativo y Respaldo son `'No hay'` a propósito — no se venden
por catálogo público, se asignan bajo solicitud.
"""

import logging

import pandas as pd

from comun import ARENA_CIUDADES, ARENA_REFERENCIAS_EXCLUIDAS, clasificar_modalidad_arena

logger = logging.getLogger("inventario.arena")

CATEGORIA_ARENA = "Arena"

_COLUMNAS_ARENA = (
    "codigo_barras",
    "especificacion",
    "nombre_comercial",
    "referencia",
    "almacen",
    "peso_g",
    "inventario",
    "existencias_restantes",
    "producto_activo",
    "modalidad",
)


def calcular_arena(catalogo_completo: pd.DataFrame) -> pd.DataFrame:
    """Filtra, clasifica y valida el catálogo de Arena para persistir.

    Args:
        catalogo_completo: Resultado de
            `inventario.normalizador.cargar_admin()` **sin** pasar por
            `filtrar_alcance_admin()` — ese filtro descarta Arena entera.

    Returns:
        Una fila por `(codigo_barras, referencia, almacen)` con la
        modalidad ya clasificada. Las referencias en
        `comun.ARENA_REFERENCIAS_EXCLUIDAS` (relleno de prueba, o
        `PB008`/`PS0199` pendientes de investigar) y las que
        `clasificar_modalidad_arena()` no reconoce quedan fuera —ambos
        casos se cuentan y se loguean, nunca en silencio (mismo criterio
        que DEC-072)—. Columnas: ver `_COLUMNAS_ARENA`.
    """
    arena = catalogo_completo[catalogo_completo["categoria"] == CATEGORIA_ARENA].copy()
    arena = arena.rename(columns={"peso": "peso_g"})
    # Espacios sueltos en el origen ('Villavicencio ') — normalizar acá evita
    # que se cuenten como ciudad "desconocida" y que aparezca duplicada en
    # los filtros del dashboard.
    arena["almacen"] = arena["almacen"].str.strip()
    total = len(arena)

    arena["modalidad"] = arena["referencia"].map(clasificar_modalidad_arena)

    sin_modalidad = arena[arena["modalidad"].isna()]
    excluidas = sin_modalidad[sin_modalidad["referencia"].isin(ARENA_REFERENCIAS_EXCLUIDAS)]
    no_reconocidas = sin_modalidad[~sin_modalidad["referencia"].isin(ARENA_REFERENCIAS_EXCLUIDAS)]
    if len(excluidas):
        logger.info(
            "calcular_arena: %d/%d filas excluidas (referencias conocidas sin uso: %s)",
            len(excluidas),
            total,
            sorted(excluidas["referencia"].unique().tolist()),
        )
    if len(no_reconocidas):
        # Una referencia de Arena que no cae en ningún bucket conocido es una
        # modalidad real sin clasificar, no ruido — merece WARNING, no INFO.
        logger.warning(
            "calcular_arena: %d/%d filas con referencia NO reconocida por el "
            "clasificador — modalidad real sin mapear, revisar comun.clasificar_modalidad_arena: %s",
            len(no_reconocidas),
            total,
            sorted(no_reconocidas["referencia"].unique().tolist()),
        )

    clasificada = arena[arena["modalidad"].notna()].copy()

    # --- Data Quality Gate ---------------------------------------------
    sin_barcode = clasificada["codigo_barras"].isna()
    if sin_barcode.any():
        logger.warning("calcular_arena: %d filas sin código de barras", int(sin_barcode.sum()))

    pesos_por_barcode = clasificada.groupby("codigo_barras")["peso_g"].nunique()
    inconsistentes = pesos_por_barcode[pesos_por_barcode > 1]
    if len(inconsistentes):
        logger.warning(
            "calcular_arena: %d código(s) de barras con más de un peso registrado: %s",
            len(inconsistentes),
            sorted(inconsistentes.index.astype(str).tolist()),
        )

    negativos = pd.to_numeric(clasificada["inventario"], errors="coerce") < 0
    if negativos.any():
        logger.warning("calcular_arena: %d filas con inventario negativo", int(negativos.sum()))

    almacen_desconocido = ~clasificada["almacen"].isin(ARENA_CIUDADES)
    if almacen_desconocido.any():
        logger.warning(
            "calcular_arena: %d filas con almacén fuera de las %d ciudades conocidas: %s",
            int(almacen_desconocido.sum()),
            len(ARENA_CIUDADES),
            sorted(clasificada.loc[almacen_desconocido, "almacen"].unique().tolist()),
        )
    # ---------------------------------------------------------------------

    faltantes = [c for c in _COLUMNAS_ARENA if c not in clasificada.columns]
    if faltantes:
        raise ValueError(f"calcular_arena: faltan columnas esperadas en el catálogo: {faltantes}")

    logger.info(
        "calcular_arena: %d/%d filas clasificadas (%s)",
        len(clasificada),
        total,
        clasificada["modalidad"].value_counts().to_dict(),
    )
    return clasificada[list(_COLUMNAS_ARENA)].reset_index(drop=True)
