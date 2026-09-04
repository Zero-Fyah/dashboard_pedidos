"""
catalogo_no_arena.py — Comportamiento de venta y de ingreso por ID no-Arena.

Requerimiento del Arquitecto (2026-09-04): una fila por ID de producto (nivel
`id_especificacion`, la granularidad más fina persistida, DEC-045/DEC-111)
que **no** sea Arena, con su clasificación ABC-XYZ, inventario actual, si
está activo para la venta, la fecha del último ingreso al sistema con tipo
de movimiento "Entrada" y la fecha de la última venta.

**No recalcula nada que ya esté persistido** (DEC-071 es el precedente para
esta regla): todas las columnas salen de tablas que el scheduler ya escribe
en cada corrida (`inventario_ubicaciones`, `inventario_sin_ubicacion`,
`inventario_abc`, `inventario_salud`, `catalogo_productos`) más dos consultas
directas sobre tablas transaccionales (`movimientos_inventario`,
`lineas_pedido`). No hace falta el Excel del admin ni un paso nuevo del
scheduler: todo lo que este módulo necesita ya vive en `pedidos.db`.

## Exclusión de Arena — sin criterio nuevo

Arena se excluye **heredando el filtro que ya existe**, no por un patrón de
referencia propio: `inventario_ubicaciones` e `inventario_sin_ubicacion` ya
son el resultado de `filtrar_alcance_admin()` (DEC-041), que saca la
categoría `Arena` (recibida por peso, 100% fuera del layout) y se queda con
`Bogotá`. Partir de esas dos tablas es "no Arena" y "CEDI Bogotá" gratis, sin
reinventar la clasificación de `comun/arena.py` (que es por *referencia*,
para un propósito distinto: el análisis nacional de Arena).

**Consecuencia que hay que declarar, no esconder:** este universo es
Bogotá-céntrico porque *todo* el dominio maduro de inventario lo es
(`inventario_abc`, `inventario_salud`, el layout) — ver `dominios.md` del
skill `supply-chain-analytics`. Un ID no-Arena que solo tiene movimiento en
otra ciudad no aparece acá. Ensancharlo a nacional exigiría reconstruir
"inventario actual" desde `movimientos_inventario` (el único dato con
alcance nacional), que es una matemática distinta a la que ya usa el resto
del dashboard — no se hizo sin decisión explícita del Arquitecto.

## Universo: `en_catalogo=1` únicamente (DEC-072, mismo criterio)

`inventario_ubicaciones` trae posiciones cuyo ID el catálogo del admin no
reconoce (934 al 2026-07-24, ver DEC-072): sin ellas no hay código de
barras, ABC-XYZ, ni vigencia — el admin nunca las publicó. DEC-072 ya decidió
que el alcance de trabajo son los ID que el admin publica; esta vista hereda
esa misma regla, filtrando `en_catalogo=1`. `inventario_sin_ubicacion` no
necesita el filtro: por construcción es siempre lo que el admin declara.

## Huecos de dato reales, medidos, no supuestos

- **Código de barras / ID de producto / nombre comercial**: salen de
  `catalogo_productos` (DEC-045), que solo tiene una fila por
  `id_especificacion` cuando el par `(referencia, código de barras)` resolvió
  sin ambigüedad. Medido sobre este universo el 2026-09-04: **cobertura
  88,7%** (3.997/4.505) — el resto queda con esas tres columnas en `None`,
  nunca inventado.
- **ABC/XYZ**: `inventario_abc` en nivel `id`/`id_global` solo tiene fila
  para un ID si tuvo venta atribuible en los últimos 6 meses (DEC-053) — no
  existe un "Sin consumo" para el nivel ID como sí existe para referencia.
  Con respaldo a nivel de referencia (mismo patrón que
  `inventario/ubicaciones.py`, DEC-057) la cobertura de ABC sube; **desde
  DEC-132 (decisión del Arquitecto, 2026-09-04) XYZ hereda con el mismo
  criterio y el mismo sufijo** `comun.SUFIJO_CLASE_HEREDADA` — deja de ser la
  única asimetría con ABC (ver docstring de `_clasificacion_con_respaldo`).
- **Activo para la venta**: precisión distinta entre las dos poblaciones —
  ver el docstring de `_vigencia_por_id`. Pregunta abierta para el
  Arquitecto, no resuelta en silencio.

## Pregunta de negocio ya resuelta (DEC-132, 2026-09-04)

¿Qué política de respaldo quiere el Arquitecto para XYZ cuando el ID no
tiene ventas atribuibles en los últimos 6 meses — dejarlo vacío (como hacía
`ubicaciones.py`, DEC-057) o heredar el de la referencia (como ya se hacía
con ABC)? **Decidido: hereda**, con el mismo sufijo `" (por referencia)"`
que ya usa ABC. Deja de ser una pregunta abierta; `ubicaciones.py` no se
tocó (esta decisión es de esta vista, no reabre DEC-057).
"""

from __future__ import annotations

import logging
import sqlite3

import pandas as pd

from comun import SUFIJO_CLASE_HEREDADA, VIGENCIA_ACTIVO, familia_de

logger = logging.getLogger("inventario.catalogo_no_arena")

# Literal exacto del origen (verificado contra `movimientos_inventario` el
# 2026-09-04): "Entrada" es un valor propio, distinto de "Entrada de compra"
# (4 filas) y "Entrada (actualización de producto)" (953 filas). El
# requerimiento dice "tipo de cambio sea 'Entrada'" — se respeta el literal,
# no se amplía a los otros dos por similitud de nombre.
TIPO_CAMBIO_ENTRADA = "Entrada"

# Mismo almacén que acota el resto del universo (DEC-041): un ID puede tener
# movimientos de "Entrada" en otras ciudades (343/5.859 los tienen, medido),
# pero mezclar ciudades rompería la coherencia con "inventario actual", que
# es Bogotá-only por construcción.
ALMACEN_BODEGA = "Bogotá"

# DEC-072: alcance de trabajo son los ID que el catálogo del admin reconoce.
EN_CATALOGO = 1

_COLUMNAS_SALIDA = [
    "id_especificacion",
    "id_producto",
    "codigo_barras",
    "familia",
    "referencia",
    "descripcion",
    "abc",
    "xyz",
    "origen_clasificacion",
    "inventario_actual",
    "activo_venta",
    "fuente_vigencia",
    "ultimo_ingreso_contenedor",
    "ultima_venta",
]


def _universo_con_ubicacion(con: sqlite3.Connection) -> pd.DataFrame:
    """IDs no-Arena con posición conocida en el layout de Bogotá (DEC-041/057).

    Se agrega por ID (`groupby`) porque un mismo ID puede repartirse en
    varias posiciones — `inventario_ubicaciones` es una línea SKU-posición,
    no una línea por ID (DEC-057).
    """
    df = pd.read_sql(
        """SELECT id_especificacion, referencia, familia, cantidad, clase, xyz
           FROM inventario_ubicaciones
           WHERE en_catalogo = ?""",
        con,
        params=(EN_CATALOGO,),
    )
    if df.empty:
        return pd.DataFrame(
            columns=[
                "id_especificacion",
                "referencia",
                "familia",
                "inventario_actual",
                "clase",
                "xyz",
            ]
        )

    # `clase`/`xyz` son atributos del ID, no de la posición: deberían repetirse
    # idénticos en todas las filas del mismo ID. Se verifica en vez de
    # asumirse — si algún ID viniera con más de un valor, tomar el primero en
    # silencio escondería una inconsistencia real del cruce.
    inconsistentes = df.groupby("id_especificacion")["clase"].nunique()
    inconsistentes = inconsistentes[inconsistentes > 1]
    if len(inconsistentes):
        logger.warning(
            "_universo_con_ubicacion: %d ID con más de una 'clase' entre sus "
            "posiciones — se toma la primera, revisar inventario_ubicaciones: %s",
            len(inconsistentes),
            sorted(inconsistentes.index.tolist())[:10],
        )

    agregado = df.groupby(["id_especificacion", "referencia", "familia"], as_index=False).agg(
        inventario_actual=("cantidad", "sum"),
        clase=("clase", "first"),
        xyz=("xyz", "first"),
    )
    return agregado


def _universo_sin_ubicacion(con: sqlite3.Connection) -> pd.DataFrame:
    """IDs no-Arena que el admin declara y que no están en ninguna posición (DEC-073)."""
    df = pd.read_sql(
        """SELECT id_especificacion, referencia, nombre_comercial, vigencia, inventario
           FROM inventario_sin_ubicacion""",
        con,
    )
    if df.empty:
        return pd.DataFrame(
            columns=[
                "id_especificacion",
                "referencia",
                "familia",
                "inventario_actual",
                "nombre_comercial",
                "vigencia",
            ]
        )
    df["familia"] = df["referencia"].map(familia_de)
    df = df.rename(columns={"inventario": "inventario_actual"})
    return df


def _clasificacion_con_respaldo(
    ids: pd.Series, referencias: pd.Series, abc: pd.DataFrame
) -> pd.DataFrame:
    """ABC y XYZ, ambos con respaldo a nivel de referencia, por ID.

    Replica el patrón de `inventario/ubicaciones.py::calcular_ubicaciones()`
    (DEC-057): un ID sin ventas atribuibles hereda la clase de su referencia
    (marcada con `comun.SUFIJO_CLASE_HEREDADA`) porque la referencia sí se
    puede clasificar sin pasar por el puente de atribución (DEC-053) — la
    causa de fondo es el código de barras no único por ID.

    **Desde DEC-132 (decisión del Arquitecto, 2026-09-04) XYZ hereda con el
    mismo criterio que ABC** — antes se dejaba sin respaldo, replicando
    `ubicaciones.py` (DEC-057), que sigue sin heredar XYZ porque esa decisión
    es de esta vista y no lo reabre.

    `origen_clasificacion` describe el origen de **ABC**, no un origen
    combinado: en el caso raro (8/1.437 referencias medido) de una
    referencia `"Sin consumo"` en valor pero con `xyz` propio (hubo
    movimiento en unidades sin ingreso neto), un ID sin fila propia puede
    heredar `xyz` de la referencia mientras `abc` queda sin clasificar — la
    columna reporta el estado de ABC en ese caso, XYZ puede diferir.

    Args:
        ids: Serie de `id_especificacion` a clasificar.
        referencias: Serie alineada con `ids`, su `referencia`.
        abc: `inventario_abc` completo (los 4 niveles apilados).

    Returns:
        DataFrame con `id_especificacion`, `abc`, `xyz`, `origen_clasificacion`.
    """
    base = pd.DataFrame(
        {"id_especificacion": ids.astype(str), "referencia": referencias.astype(str)}
    )

    globales = abc[abc["nivel"] == "id_global"][["clave", "abc", "xyz"]].copy()
    globales["clave"] = globales["clave"].astype(str)
    base = base.merge(
        globales.rename(columns={"clave": "id_especificacion", "abc": "_abc_id", "xyz": "_xyz_id"}),
        on="id_especificacion",
        how="left",
    )

    refs = abc[abc["nivel"] == "referencia"][["clave", "abc", "xyz"]].copy()
    refs["clave"] = refs["clave"].astype(str)
    base = base.merge(
        refs.rename(columns={"clave": "referencia", "abc": "_abc_ref", "xyz": "_xyz_ref"}),
        on="referencia",
        how="left",
    )

    base["abc"] = base["_abc_id"]
    base["origen_clasificacion"] = "ID"
    heredable = base["_abc_id"].isna() & base["_abc_ref"].isin(["A", "B", "C"])
    base.loc[heredable, "abc"] = base.loc[heredable, "_abc_ref"] + SUFIJO_CLASE_HEREDADA
    base.loc[heredable, "origen_clasificacion"] = "Referencia"
    base.loc[base["abc"].isna(), "origen_clasificacion"] = "Sin ventas"

    # DEC-132: mismo respaldo que ABC, mismo sufijo — decisión del
    # Arquitecto, 2026-09-04. `heredable_xyz` es independiente de
    # `heredable` (ABC): un ID puede heredar uno sin heredar el otro (ver
    # el caso raro documentado arriba).
    base["xyz"] = base["_xyz_id"]
    heredable_xyz = base["_xyz_id"].isna() & base["_xyz_ref"].isin(["X", "Y", "Z"])
    base.loc[heredable_xyz, "xyz"] = base.loc[heredable_xyz, "_xyz_ref"] + SUFIJO_CLASE_HEREDADA

    return base[["id_especificacion", "abc", "xyz", "origen_clasificacion"]]


def _vigencia_por_id(universo: pd.DataFrame, con: sqlite3.Connection) -> pd.DataFrame:
    """Activo para la venta, por ID — con precisión distinta según la fuente.

    **Los dos lados de este universo no tienen la misma precisión, y hay que
    decirlo en vez de mezclarlos en silencio:**

    - Para los ID que ya traían `vigencia` propia (`inventario_sin_ubicacion`,
      calculada por `clasificar_vigencia()` sobre el `producto_activo` de
      *ese* ID exacto, DEC-073/DEC-122) se usa tal cual — es la precisión
      correcta, por ID.
    - Para los ID con posición conocida (`inventario_ubicaciones`), el
      pipeline **no persiste** `producto_activo` por ID — solo lo hace por
      `referencia`, en `inventario_salud.vigencia` (DEC-065), con la regla
      "basta una especificación vigente para que la referencia lo sea". Se
      usa ese valor como aproximación conservadora, y se marca en
      `fuente_vigencia` para que quien consuma la tabla sepa qué tan fino es
      el dato en cada fila.

    Args:
        universo: DataFrame con `id_especificacion`, `referencia`, y
            opcionalmente `vigencia` (ya presente para el lado
            `sin_ubicacion`).
        con: Conexión de lectura.

    Returns:
        `universo` con `activo_venta` (bool) y `fuente_vigencia`
        (`"id"` o `"referencia"`) agregadas.
    """
    salud = pd.read_sql("SELECT referencia, vigencia FROM inventario_salud", con)
    salud["referencia"] = salud["referencia"].astype(str).str.strip()

    df = universo.copy()
    df["referencia"] = df["referencia"].astype(str).str.strip()

    tiene_propia = "vigencia" in df.columns
    if not tiene_propia:
        df["vigencia"] = pd.NA
    df["fuente_vigencia"] = df["vigencia"].notna().map({True: "id", False: "referencia"})

    df = df.merge(
        salud.rename(columns={"vigencia": "_vigencia_referencia"}), on="referencia", how="left"
    )
    df["vigencia"] = df["vigencia"].fillna(df["_vigencia_referencia"])
    df["activo_venta"] = df["vigencia"] == VIGENCIA_ACTIVO
    return df.drop(columns=["_vigencia_referencia"])


def _ultimo_ingreso_contenedor(con: sqlite3.Connection) -> pd.DataFrame:
    """Última fecha de un movimiento `tipo_cambio = 'Entrada'`, por ID.

    "Ingreso al sistema por contenedor" es lenguaje de negocio del
    Arquitecto para lo que el origen registra literalmente como tipo de
    cambio `"Entrada"` — no hay una columna ni un concepto de "contenedor"
    en `movimientos_inventario` (verificado: la palabra "contenedor" que sí
    aparece en 480 filas es el *nombre de un producto* —"Contenedor de
    almacenamiento..."—, no una unidad logística). Se filtra por el literal
    exacto, sin incluir "Entrada de compra" ni "Entrada (actualización de
    producto)".
    """
    df = pd.read_sql(
        """SELECT sku_id AS id_especificacion, MAX(fecha_operacion) AS ultimo_ingreso_contenedor
           FROM movimientos_inventario
           WHERE tipo_cambio = ? AND almacen = ?
           GROUP BY sku_id""",
        con,
        params=(TIPO_CAMBIO_ENTRADA, ALMACEN_BODEGA),
    )
    df["id_especificacion"] = df["id_especificacion"].astype(str)
    return df


def _ultima_venta(con: sqlite3.Connection) -> pd.DataFrame:
    """Fecha de la venta más reciente por ID, vía el puente de `catalogo_productos`.

    `lineas_pedido` no guarda ningún ID (DEC-045): se atribuye la línea a un
    `id_especificacion` por `(referencia, código de barras)`, el mismo
    puente que ya usa el resto del dashboard — no un criterio nuevo.
    Excluye subpedidos `cancelado`, mismo criterio que
    `inventario/salud.py::_demanda_por_referencia()` (una venta cancelada no
    es una venta).
    """
    lineas = pd.read_sql(
        """SELECT lp.id_pedido, lp.numero_subpedido, lp.referencia, lp.codigo_barras
           FROM lineas_pedido lp
           WHERE lp.referencia IS NOT NULL AND lp.referencia != ''""",
        con,
    )
    pedidos = pd.read_sql("SELECT id_pedido, fecha FROM pedidos", con)
    subpedidos = pd.read_sql("SELECT id_pedido, numero_subpedido, estado FROM subpedidos", con)
    puente = pd.read_sql(
        "SELECT referencia, codigo_barras, id_especificacion "
        "FROM catalogo_productos WHERE id_especificacion IS NOT NULL",
        con,
    )

    ventas = lineas.merge(pedidos, on="id_pedido").merge(
        subpedidos, on=["id_pedido", "numero_subpedido"]
    )
    ventas = ventas[ventas["estado"].str.lower() != "cancelado"]
    ventas["referencia"] = ventas["referencia"].astype(str).str.strip()
    ventas["codigo_barras"] = ventas["codigo_barras"].astype(str).str.strip()

    puente["referencia"] = puente["referencia"].astype(str).str.strip()
    puente["codigo_barras"] = puente["codigo_barras"].astype(str).str.strip()

    ventas_id = ventas.merge(puente, on=["referencia", "codigo_barras"], how="inner")
    if ventas_id.empty:
        return pd.DataFrame(columns=["id_especificacion", "ultima_venta"])
    return (
        ventas_id.groupby("id_especificacion", as_index=False)["fecha"]
        .max()
        .rename(columns={"fecha": "ultima_venta"})
    )


def construir_catalogo_no_arena(con: sqlite3.Connection) -> pd.DataFrame:
    """Vista consolidada por ID no-Arena: venta, clasificación e ingreso.

    Una fila por `id_especificacion` (DEC-045/DEC-111 — la granularidad más
    fina que el proyecto persiste; ver docstring del módulo para por qué no
    es `id_producto`). Alcance: **CEDI Bogotá, catálogo del admin
    reconocido, sin Arena** — heredado, no un filtro nuevo (ver docstring del
    módulo). No requiere el Excel del admin: lee únicamente tablas ya
    persistidas por la corrida más reciente del scheduler.

    Args:
        con: Conexión de lectura a `pedidos.db`.

    Returns:
        DataFrame con las columnas de `_COLUMNAS_SALIDA`. `abc`/`xyz`,
        `codigo_barras`/`id_producto`/`descripcion` y las dos fechas pueden
        venir `None` — es un hueco de dato real, documentado en el docstring
        del módulo, no un `0`/cadena vacía inventado.
    """
    con_ubic = _universo_con_ubicacion(con)
    sin_ubic = _universo_sin_ubicacion(con)

    abc_tabla = pd.read_sql("SELECT nivel, clave, abc, xyz FROM inventario_abc", con)

    # ── Lado con posición: ABC/XYZ ya vienen calculados con respaldo (DEC-057) ──
    con_ubic = con_ubic.rename(columns={"clase": "abc"})
    con_ubic["origen_clasificacion"] = "ID"
    sin_clase = con_ubic["abc"].isna()
    con_ubic.loc[sin_clase, "origen_clasificacion"] = "Sin ventas"
    # `inventario_ubicaciones.clase` ya trae el sufijo de heredado si aplica
    # (DEC-057); no hay forma de distinguir "ID" de "Referencia" desde acá sin
    # re-derivarlo — se re-deriva con la misma función que usa el otro lado,
    # así los dos caminos son idénticos y comparables entre sí.
    con_ubic = con_ubic.drop(columns=["abc", "xyz", "origen_clasificacion"]).merge(
        _clasificacion_con_respaldo(
            con_ubic["id_especificacion"], con_ubic["referencia"], abc_tabla
        ),
        on="id_especificacion",
        how="left",
    )

    # ── Lado sin posición: no tiene ABC/XYZ propio, se calcula desde cero ──
    if not sin_ubic.empty:
        sin_ubic = sin_ubic.merge(
            _clasificacion_con_respaldo(
                sin_ubic["id_especificacion"], sin_ubic["referencia"], abc_tabla
            ),
            on="id_especificacion",
            how="left",
        )

    universo = pd.concat(
        [
            con_ubic[
                [
                    "id_especificacion",
                    "referencia",
                    "familia",
                    "inventario_actual",
                    "abc",
                    "xyz",
                    "origen_clasificacion",
                ]
            ],
            sin_ubic[
                [
                    c
                    for c in [
                        "id_especificacion",
                        "referencia",
                        "familia",
                        "inventario_actual",
                        "abc",
                        "xyz",
                        "origen_clasificacion",
                        "vigencia",
                        "nombre_comercial",
                    ]
                    if c in sin_ubic.columns
                ]
            ],
        ],
        ignore_index=True,
    )

    dup = universo["id_especificacion"].duplicated()
    if dup.any():
        logger.warning(
            "construir_catalogo_no_arena: %d ID aparecen en ambas fuentes "
            "(con y sin ubicación) — se conserva la primera aparición, revisar",
            int(dup.sum()),
        )
        universo = universo[~dup]

    universo = _vigencia_por_id(universo, con)

    catalogo = pd.read_sql(
        "SELECT id_especificacion, id_producto, codigo_barras, nombre_comercial "
        "FROM catalogo_productos WHERE id_especificacion IS NOT NULL",
        con,
    )
    sin_barcode = (
        universo["id_especificacion"].astype(str).isin(catalogo["id_especificacion"].astype(str))
    )
    if (~sin_barcode).any():
        logger.info(
            "construir_catalogo_no_arena: %d/%d ID sin fila en catalogo_productos "
            "(par referencia/código de barras ambiguo, DEC-045/DEC-111) — "
            "codigo_barras/id_producto/descripcion quedan en None",
            int((~sin_barcode).sum()),
            len(universo),
        )
    universo = universo.merge(
        catalogo.rename(columns={"nombre_comercial": "_nombre_catalogo"}),
        on="id_especificacion",
        how="left",
    )
    # Descripción: preferir el nombre comercial ya persistido para el lado
    # sin ubicación (más específico, trae la variante) y caer al del catálogo
    # general cuando no hay uno propio.
    if "nombre_comercial" in universo.columns:
        universo["descripcion"] = universo["nombre_comercial"].fillna(universo["_nombre_catalogo"])
    else:
        universo["descripcion"] = universo["_nombre_catalogo"]

    entradas = _ultimo_ingreso_contenedor(con)
    universo = universo.merge(entradas, on="id_especificacion", how="left")

    ventas = _ultima_venta(con)
    universo = universo.merge(ventas, on="id_especificacion", how="left")

    faltan_venta = universo["ultima_venta"].isna().sum()
    faltan_abc = universo["abc"].isna().sum()
    logger.info(
        "construir_catalogo_no_arena: %d ID no-Arena (%d con posición · %d sin "
        "posición) · %d sin ninguna venta atribuible · %d sin ABC/XYZ",
        len(universo),
        len(con_ubic),
        len(sin_ubic),
        int(faltan_venta),
        int(faltan_abc),
    )

    for columna in _COLUMNAS_SALIDA:
        if columna not in universo.columns:
            universo[columna] = pd.NA
    return universo[_COLUMNAS_SALIDA].sort_values("id_especificacion").reset_index(drop=True)
