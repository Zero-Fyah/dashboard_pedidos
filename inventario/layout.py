"""
layout.py — Layout de bodega: clasificación de ubicaciones (DEC-041).

Carga el documento manual del Arquitecto
(`data/inventario/layout_bodega.xlsx`) y clasifica cada `Ubicación` de
Bochica en `Picking`, `Altura`, `Paso Montacarga` o `FUERA_LAYOUT`.

Es la pieza que desbloquea DEC-039: sin esta clasificación, picking
(sistemáticamente inflado — el sistema de bodega nunca descuenta sus
movimientos) y altura (confiable) quedaban mezclados en un solo total.

El layout es **manual y no regenerable**: a diferencia de los dos Excel
que descarga el scraper, si se pierde `data/` no hay forma de volver a
bajarlo.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("inventario.layout")

RUTA_LAYOUT_DEFAULT = Path(__file__).parent.parent / "data" / "inventario" / "layout_bodega.xlsx"

HOJA_LAYOUT = "layout"

TIPO_PICKING = "Picking"
TIPO_ALTURA = "Altura"
TIPO_PASO = "Paso Montacarga"
TIPO_FUERA = "FUERA_LAYOUT"
TIPO_VIRTUAL = "Virtual"

# Racks que existen en Bochica pero **no son ubicaciones físicas**:
# confirmado por el Arquitecto el 2026-07-27. Se separan de `FUERA_LAYOUT`
# —que mezcla otros almacenes y mercancía por peso— para que queden
# identificadas cuando se decida qué hacer con ellas. `O_51` es el caso
# raro: rack real, posición una más allá del final declarado del layout.
RACKS_VIRTUALES: frozenset[str] = frozenset({"Q", "R", "R1", "S", "T", "U", "Z", "YU"})
UBICACIONES_VIRTUALES: frozenset[str] = frozenset({"O_51_1"})

# Los tres valores que el layout puede traer en `Tipo Ubicación`.
# `Paso Montacarga` (DEC-041) es un túnel transversal a mitad de rack, no
# una posición de almacenamiento: no entra en la fórmula de negocio.
TIPOS_LAYOUT: frozenset[str] = frozenset({TIPO_PICKING, TIPO_ALTURA, TIPO_PASO})

# Alturas de picking según el layout actual. NO se usa para clasificar
# —el origen de verdad es la columna `Tipo Ubicación`— solo para avisar
# si el documento cambia de geometría. Ver `_validar_layout()`.
ALTURAS_PICKING: frozenset[int] = frozenset({1, 2, 3})

_COLUMNAS = {
    "CEDI": "cedi",
    "Subbodega": "subbodega",
    "Ubicación": "ubicacion",
    "Tipo Ubicación": "tipo",
    "Empresa": "empresa",
    "Activa Para Almacenar": "activa",
    "Rack": "rack",
    "Posición": "posicion",
    "Altura": "altura",
}


def cargar_layout(path: Path | None = None) -> pd.DataFrame:
    """Carga y valida el Excel del layout de bodega.

    Args:
        path: Ruta al .xlsx. Default: `RUTA_LAYOUT_DEFAULT`.

    Returns:
        DataFrame con columnas snake_case (`ubicacion`, `tipo`,
        `subbodega`, `activa`, `rack`, `posicion`, `altura`, `cedi`,
        `empresa`). `ubicacion` es la llave de cruce contra
        `cargar_bochica()` — se compara **exacta, sin normalizar
        mayúsculas** (DEC-039 pregunta 2: la ambigüedad temida no existía,
        el layout es 100% mayúsculas).

    Raises:
        FileNotFoundError: Si el archivo no existe.
        ValueError: Si faltan columnas esperadas o hay ubicaciones
            duplicadas — el layout es manual, un error de edición debe
            fallar ruidoso y no propagarse al cálculo.
    """
    ruta = path or RUTA_LAYOUT_DEFAULT
    if not ruta.exists():
        raise FileNotFoundError(
            f"Layout de bodega no encontrado: {ruta}. Es un documento manual "
            "del Arquitecto, no lo descarga el scraper (DEC-041)."
        )

    df = pd.read_excel(ruta, sheet_name=HOJA_LAYOUT)

    faltantes = [c for c in _COLUMNAS if c not in df.columns]
    if faltantes:
        raise ValueError(
            f"El layout {ruta} no trae las columnas esperadas: {faltantes}. "
            f"Columnas presentes: {list(df.columns)}"
        )

    df = df.rename(columns=_COLUMNAS)[list(_COLUMNAS.values())]
    df["ubicacion"] = df["ubicacion"].astype(str)

    _validar_layout(df, ruta)
    df["estiba_completa"] = _marcar_estiba_completa(df)
    logger.info("cargar_layout: %d ubicaciones desde %s", len(df), ruta)
    return df


def _marcar_estiba_completa(df: pd.DataFrame) -> pd.Series:
    """Marca las posiciones de picking donde el bulto ocupa los 3 niveles.

    Semántica confirmada por el Arquitecto (DEC-041): cuando la altura 1
    está `SI` y las alturas 2-3 `NO`, se almacena una estiba completa que
    ocupa los tres niveles físicos, y **todo el inventario se registra en
    el nivel 1**. Es distinto de una posición deshabilitada por completo
    (`NO` en las tres alturas), que no debería tener stock en ninguna.

    Se marcan las tres filas de la posición, no solo la altura 1, para
    que `anomalias_layout()` pueda distinguir los dos casos.

    Returns:
        Serie booleana alineada al índice de `df`.
    """
    picking = df[df["tipo"] == TIPO_PICKING]
    habilitada_en_base = (
        picking[picking["altura"] == 1].set_index(["rack", "posicion"])["activa"].eq("SI")
    )
    claves = pd.MultiIndex.from_frame(df[["rack", "posicion"]])
    es_estiba = claves.map(habilitada_en_base).fillna(False).to_numpy(dtype=bool)
    return pd.Series(es_estiba & (df["tipo"] == TIPO_PICKING).to_numpy(), index=df.index)


def _validar_layout(df: pd.DataFrame, ruta: Path) -> None:
    """Chequea las invariantes del layout; falla duro o avisa según gravedad.

    Falla (`ValueError`) ante duplicados de `ubicacion`, que romperían el
    cruce multiplicando filas de Bochica. Solo avisa (WARNING) ante
    cambios de geometría o tipos nuevos: el layout es el origen de verdad
    y puede evolucionar — el código no debe bloquearse por eso, pero
    tampoco absorberlo en silencio.
    """
    duplicadas = df["ubicacion"].duplicated().sum()
    if duplicadas:
        ejemplos = df.loc[df["ubicacion"].duplicated(keep=False), "ubicacion"].unique()[:5]
        raise ValueError(
            f"El layout {ruta} tiene {duplicadas} ubicaciones duplicadas "
            f"(ej: {list(ejemplos)}). El cruce contra Bochica multiplicaría filas."
        )

    tipos_desconocidos = set(df["tipo"].dropna().unique()) - TIPOS_LAYOUT
    if tipos_desconocidos:
        logger.warning(
            "cargar_layout: tipos de ubicación no reconocidos %s — quedan fuera "
            "de picking y altura en el cálculo (revisar DEC-041)",
            sorted(tipos_desconocidos),
        )

    esperada = (
        df["rack"].astype(str) + "_" + df["posicion"].astype(str) + "_" + df["altura"].astype(str)
    )
    inconsistentes = int((df["ubicacion"] != esperada).sum())
    if inconsistentes:
        logger.warning(
            "cargar_layout: %d ubicaciones no siguen el patrón Rack_Posición_Altura",
            inconsistentes,
        )

    # La regla "altura 1-3 = Picking" se cumple hoy al 100%, pero se lee
    # de la columna, no se deriva. Esto solo avisa si el documento cambia.
    picking = df[df["tipo"] == TIPO_PICKING]
    fuera_de_regla = int((~picking["altura"].isin(ALTURAS_PICKING)).sum())
    if fuera_de_regla:
        logger.warning(
            "cargar_layout: %d posiciones de Picking fuera de las alturas %s — "
            "la geometría del layout cambió",
            fuera_de_regla,
            sorted(ALTURAS_PICKING),
        )


def clasificar_ubicaciones(df_bochica: pd.DataFrame, df_layout: pd.DataFrame) -> pd.DataFrame:
    """Anota cada fila de Bochica con su tipo de ubicación según el layout.

    Left join deliberado (no inner): las filas cuya `ubicacion` no está en
    el layout **no se descartan**, quedan marcadas `FUERA_LAYOUT` —
    mismo criterio que el outer join de `cruzar_inventarios()`. Filtrarlas
    es decisión de `solo_layout()`, explícita y medible.

    Args:
        df_bochica: Resultado de `inventario.normalizador.cargar_bochica()`.
        df_layout: Resultado de `cargar_layout()`.

    Las ubicaciones virtuales confirmadas (`RACKS_VIRTUALES`,
    `UBICACIONES_VIRTUALES`) se marcan `Virtual` en vez de `FUERA_LAYOUT`:
    no son un hallazgo de calidad de datos que haya que investigar, son
    ubicaciones lógicas conocidas. Mezclarlas con las otras esconde las
    que sí hay que revisar.

    Returns:
        `df_bochica` con las columnas `tipo`, `rack`, `posicion`,
        `altura`, `subbodega` y `activa` agregadas. `tipo` nunca es NaN:
        vale `FUERA_LAYOUT` cuando no hubo match.
    """
    columnas_layout = [
        "ubicacion",
        "tipo",
        "rack",
        "posicion",
        "altura",
        "subbodega",
        "activa",
        "estiba_completa",
    ]
    anotado = df_bochica.merge(df_layout[columnas_layout], on="ubicacion", how="left")
    anotado["tipo"] = anotado["tipo"].fillna(TIPO_FUERA)

    ubicacion = anotado["ubicacion"].astype(str)
    es_virtual = ubicacion.isin(UBICACIONES_VIRTUALES) | ubicacion.str.split("_").str[0].isin(
        RACKS_VIRTUALES
    )
    # Solo reclasifica lo que quedó fuera del layout: si el layout llegara a
    # declarar uno de estos racks, manda el layout.
    anotado.loc[es_virtual & (anotado["tipo"] == TIPO_FUERA), "tipo"] = TIPO_VIRTUAL
    return anotado


def solo_layout(df_clasificado: pd.DataFrame) -> pd.DataFrame:
    """Recorta al alcance de la comparación: solo ubicaciones del layout (DEC-041).

    Las ubicaciones fuera del layout son mercancía **recibida por peso**
    (buckets `Q`/`R1`/`YU`/`Z`, prefijos `PU*`, otras sedes), que se
    convierte a unidades en un proceso aparte — fuera de alcance por
    decisión del Arquitecto hasta que el flujo de unidades esté
    controlado. Son ~47% de las unidades de Bochica: el descarte se loguea
    para que nunca sea silencioso.

    Args:
        df_clasificado: Resultado de `clasificar_ubicaciones()`.

    Returns:
        Solo las filas con `tipo` en `TIPOS_LAYOUT`.
    """
    dentro = df_clasificado[df_clasificado["tipo"].isin(TIPOS_LAYOUT)]
    descartadas = len(df_clasificado) - len(dentro)
    if descartadas:
        unidades = float(
            df_clasificado.loc[~df_clasificado.index.isin(dentro.index), "cantidad"].sum()
        )
        logger.info(
            "solo_layout: %d filas fuera del layout descartadas (%.0f unidades, "
            "mercancía recibida por peso — DEC-041)",
            descartadas,
            unidades,
        )
    return dentro


def resumen_por_tipo(df_clasificado: pd.DataFrame) -> pd.DataFrame:
    """Agrega filas y unidades por tipo de ubicación — para logging/diagnóstico.

    Args:
        df_clasificado: Resultado de `clasificar_ubicaciones()`.

    Returns:
        DataFrame indexado por `tipo` con columnas `filas` y `unidades`.
    """
    return df_clasificado.groupby("tipo").agg(
        filas=("cantidad", "size"), unidades=("cantidad", "sum")
    )
