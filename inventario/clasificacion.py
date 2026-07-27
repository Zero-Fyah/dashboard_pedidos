"""
clasificacion.py — ABC-XYZ jerárquico: familia → referencia → ID (DEC-053).

ABC ordena por cuánto ingreso genera cada unidad; XYZ, por qué tan
predecible es su demanda. Se calcula en **tres niveles encadenados**, de lo
macro a lo micro:

1. **Familia** — el Pareto se corre entre las familias completas.
2. **Referencia, dentro de su familia** — la referencia se trata como un
   único elemento, aunque alguno de sus ID rote poco. El Pareto se corre
   **dentro de cada familia**, no contra todo el catálogo: una referencia
   puede ser A en su familia sin serlo en el total.
3. **ID, dentro de su referencia** — el comportamiento individual de cada
   variante, sin perder la vista consolidada de la referencia.

El nivel 3 es el que habilita el objetivo de fondo: hoy el slotting se hace
por referencia, así que variantes con rotación muy distinta comparten
ubicación lógica. Con ABC-XYZ por ID se pueden asignar ubicaciones acordes
a la rotación real de cada una y acortar los recorridos de picking.

**ABC usa el ingreso real** (`monto_final_num`, poblado al 100%), no un
proxy de demanda × precio de catálogo.
"""

import datetime as dt
import logging
import sqlite3

import numpy as np
import pandas as pd

from comun import familia_de

logger = logging.getLogger("inventario.clasificacion")

# Meses completos hacia atrás. El mes en curso se excluye siempre: está a
# medio transcurrir, así que subestimaría su consumo y metería una caída
# artificial en el coeficiente de variación de todas las unidades a la vez.
VENTANA_MESES = 6

# Cortes de Pareto sobre el porcentaje acumulado de ingreso.
UMBRAL_A_PCT = 80.0
UMBRAL_B_PCT = 95.0

# Cortes del coeficiente de variación de la demanda mensual.
CV_ESTABLE = 0.5
CV_VARIABLE = 1.0

SIN_CONSUMO = "Sin consumo"

NIVEL_FAMILIA = "familia"
NIVEL_REFERENCIA = "referencia"
NIVEL_ID = "id"

# Cuarto nivel, fuera de la jerarquía: el mismo ID comparado contra **todo
# el almacén**, no contra los de su propia referencia (DEC-057).
#
# No reemplaza a `NIVEL_ID`, responde otra pregunta. El jerárquico dice
# "dentro de esta referencia, cuáles mandan" — que es lo que sirve para
# slotting, porque las variantes de una referencia comparten ubicación. El
# global dice "de todo lo que hay en bodega, qué cuento primero" — que es
# lo que sirve para priorizar conteos. Usar el jerárquico para priorizar
# inflaría la clase A: cada referencia aporta las suyas.
NIVEL_ID_GLOBAL = "id_global"

# Política sugerida por celda. Es guía estándar de gestión de inventarios,
# no una regla que el sistema aplique: la decisión sigue siendo del negocio.
POLITICAS = {
    "AX": "Reposición automática ajustada · stock de seguridad bajo · revisar seguido",
    "AY": "Reposición automática · stock de seguridad mayor por la variabilidad",
    "AZ": "Revisión manual frecuente · alto impacto y demanda impredecible",
    "BX": "Reposición periódica · parámetros estables",
    "BY": "Reposición periódica · revisar el stock de seguridad",
    "BZ": "Revisión manual · demanda errática",
    "CX": "Lote grande y poco frecuente · bajo esfuerzo de gestión",
    "CY": "Revisión manual espaciada",
    "CZ": "Contra pedido o mínimos · candidatas a depurar del catálogo",
}

_COLUMNAS = [
    "nivel",
    "clave",
    "padre",
    "etiqueta",
    "valor_consumo",
    "pct_valor",
    "pct_acumulado",
    "abc",
    "unidades",
    "cv",
    "meses_con_venta",
    "xyz",
    "celda",
    "politica",
]


def _meses_completos(hoy: dt.date, cantidad: int) -> list[str]:
    """Los N meses completos anteriores al mes en curso, como 'YYYY-MM'."""
    meses = []
    ancla = dt.date(hoy.year, hoy.month, 1)
    for _ in range(cantidad):
        ancla = (ancla - dt.timedelta(days=1)).replace(day=1)
        meses.append(ancla.strftime("%Y-%m"))
    return sorted(meses)


def construir_puente_especificaciones(df_admin: pd.DataFrame) -> pd.DataFrame:
    """Puente `(referencia, código de barras)` → ID de especificación.

    Es el que habilita el nivel 3: `lineas_pedido` no guarda ningún ID, así
    que sin este puente no hay forma de atribuir una venta a una variante.

    Igual que `catalogo_productos` (DEC-045), **solo se conservan los pares
    inequívocos**: si un par resolviera a dos ID, el join duplicaría la
    línea de pedido e inflaría el consumo. Medido: 89,4% de los pares son
    inequívocos y cubren el **80,8% de las líneas** — el resto queda sin
    atribuir a propósito, y la vista lo informa.

    Args:
        df_admin: Catálogo del admin ya filtrado por alcance.

    Returns:
        DataFrame con `ref`, `cb`, `id_especificacion` y `especificacion`.
    """
    df = df_admin.copy()
    df["ref"] = df["referencia"].astype(str).str.strip()
    df["cb"] = df["codigo_barras"].astype(str).str.strip()
    df["id_especificacion"] = df["id_especificacion"].astype(str)

    por_par = df.groupby(["ref", "cb"])["id_especificacion"].nunique()
    inequivocos = por_par[por_par == 1].index

    puente = (
        df.set_index(["ref", "cb"])
        .loc[df.set_index(["ref", "cb"]).index.isin(inequivocos)]
        .reset_index()
        .drop_duplicates(subset=["ref", "cb"])
    )
    descartados = len(por_par) - len(puente)
    if descartados:
        logger.info(
            "construir_puente_especificaciones: %d/%d pares descartados por ambigüedad "
            "— esas líneas quedan sin atribuir a un ID (DEC-053)",
            descartados,
            len(por_par),
        )
    return puente[["ref", "cb", "id_especificacion", "especificacion"]]


def calcular_clasificacion(
    df_admin: pd.DataFrame,
    con: sqlite3.Connection,
    *,
    almacen: str = "Bogotá",
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Clasifica en ABC-XYZ los tres niveles de la jerarquía.

    Args:
        df_admin: Catálogo del admin ya filtrado por alcance.
        con: Conexión de lectura a pedidos.db.
        almacen: Almacén cuyas ventas se consideran.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        Un DataFrame con las tres jerarquías apiladas, distinguidas por
        `nivel`. `padre` encadena cada fila con su nivel superior: la
        referencia con su familia y el ID con su referencia.
    """
    referencia_hoy = hoy or dt.date.today()
    meses = _meses_completos(referencia_hoy, VENTANA_MESES)
    mov = _movimiento(con, almacen, meses)

    universo = sorted(set(df_admin["referencia"].astype(str).str.strip()))
    # El universo lo define el catálogo, pero solo cuentan las ventas de
    # referencias que siguen en él.
    mov = mov[mov["ref"].isin(universo)]

    puente = construir_puente_especificaciones(df_admin)
    mov_id = mov.merge(puente, on=["ref", "cb"], how="inner")

    familias = _abc_xyz(
        mov.assign(clave=mov["ref"].map(familia_de).fillna("Sin familia")),
        meses,
        nivel=NIVEL_FAMILIA,
    )
    # El universo de referencias es el catálogo completo: una referencia sin
    # ventas también tiene que aparecer, como "Sin consumo".
    base_refs = pd.DataFrame({"clave": universo})
    base_refs["padre"] = base_refs["clave"].map(familia_de).fillna("Sin familia")
    referencias = _abc_xyz(
        mov.assign(clave=mov["ref"], padre=mov["ref"].map(familia_de).fillna("Sin familia")),
        meses,
        nivel=NIVEL_REFERENCIA,
        base=base_refs,
    )
    ids = _abc_xyz(
        mov_id.assign(clave=mov_id["id_especificacion"], padre=mov_id["ref"]),
        meses,
        nivel=NIVEL_ID,
        etiquetas=mov_id.drop_duplicates("id_especificacion").set_index("id_especificacion")[
            "especificacion"
        ],
    )

    # Sin columna `padre`, `_abc_xyz` corre el Pareto sobre todo el conjunto:
    # ese es exactamente el ABC global (DEC-057).
    etiquetas_id = mov_id.drop_duplicates("id_especificacion").set_index("id_especificacion")[
        "especificacion"
    ]
    ids_globales = _abc_xyz(
        mov_id.assign(clave=mov_id["id_especificacion"]),
        meses,
        nivel=NIVEL_ID_GLOBAL,
        etiquetas=etiquetas_id,
    )

    resultado = pd.concat([familias, referencias, ids, ids_globales], ignore_index=True)
    en_a = ids_globales["abc"] == "A"
    logger.info(
        "calcular_clasificacion: ventana %s..%s · %d familias · %d referencias · %d ID "
        "· ABC global: %d ID clase A concentran %.1f%% del valor",
        meses[0],
        meses[-1],
        len(familias),
        len(referencias),
        len(ids),
        int(en_a.sum()),
        ids_globales.loc[en_a, "pct_valor"].sum(),
    )
    return resultado[_COLUMNAS]


def _movimiento(con: sqlite3.Connection, almacen: str, meses: list[str]) -> pd.DataFrame:
    """Líneas de pedido de la ventana, sin cancelados y del almacén pedido."""
    lineas = pd.read_sql(
        """SELECT id_pedido, numero_subpedido, referencia, codigo_barras, almacen,
                  cantidad_comprada, monto_final_num
           FROM lineas_pedido
           WHERE referencia IS NOT NULL AND referencia != ''""",
        con,
    )
    pedidos = pd.read_sql("SELECT id_pedido, fecha FROM pedidos", con)
    subpedidos = pd.read_sql("SELECT id_pedido, numero_subpedido, estado FROM subpedidos", con)

    mov = lineas.merge(pedidos, on="id_pedido").merge(
        subpedidos, on=["id_pedido", "numero_subpedido"]
    )
    # Un subpedido cancelado no es consumo: contarlo distorsionaría tanto el
    # ranking de valor como la variabilidad.
    mov = mov[(mov["almacen"] == almacen) & (mov["estado"].str.lower() != "cancelado")]
    mov["ref"] = mov["referencia"].astype(str).str.strip()
    mov["cb"] = mov["codigo_barras"].astype(str).str.strip()
    mov["mes"] = mov["fecha"].astype(str).str[:7]
    return mov[mov["mes"].isin(meses)]


def _abc_xyz(
    mov: pd.DataFrame,
    meses: list[str],
    *,
    nivel: str,
    base: pd.DataFrame | None = None,
    etiquetas: pd.Series | None = None,
) -> pd.DataFrame:
    """Calcula ABC y XYZ para una unidad de análisis.

    `mov` debe traer una columna `clave` (la unidad) y opcionalmente
    `padre`. **El Pareto se corre dentro de cada `padre`**, que es lo que
    hace jerárquico el análisis: una referencia se compara contra las de su
    propia familia, no contra todo el catálogo.

    Args:
        mov: Movimiento con `clave`, `mes`, `cantidad_comprada`,
            `monto_final_num` y, si aplica, `padre`.
        meses: Ventana, para que los meses sin venta cuenten como cero.
        nivel: Etiqueta del nivel jerárquico.
        base: Universo completo de claves (con su `padre`), para que las
            unidades sin ventas aparezcan como "Sin consumo".
        etiquetas: Nombre legible por clave (la especificación, en el
            nivel de ID).
    """
    tiene_padre = "padre" in mov.columns
    llaves = ["clave", "padre"] if tiene_padre else ["clave"]

    consumo = mov.groupby(llaves, as_index=False).agg(
        valor_consumo=("monto_final_num", "sum"), unidades=("cantidad_comprada", "sum")
    )
    if base is not None:
        columnas_base = [c for c in llaves if c in base.columns]
        consumo = base[columnas_base].merge(consumo, on=columnas_base, how="left")
    for columna in ("valor_consumo", "unidades"):
        # `astype(float)` explícito: si el merge contra la base no encontró
        # ninguna venta, la columna llega como `object` y el `cumsum()` del
        # Pareto revienta con "not supported for object dtype".
        consumo[columna] = consumo[columna].fillna(0.0).astype(float)
    if not tiene_padre:
        consumo["padre"] = None

    df = _clasificar_abc(consumo, agrupar_por="padre" if tiene_padre else None)
    df = df.merge(_calcular_xyz(mov, meses), on="clave", how="left")

    df["nivel"] = nivel
    df["etiqueta"] = df["clave"].map(etiquetas) if etiquetas is not None else df["clave"]
    df["celda"] = np.where(
        (df["abc"] != SIN_CONSUMO) & df["xyz"].notna(),
        df["abc"].astype(str) + df["xyz"].astype(str),
        None,
    )
    df["politica"] = df["celda"].map(POLITICAS)
    return df


def _clasificar_abc(df: pd.DataFrame, *, agrupar_por: str | None) -> pd.DataFrame:
    """Asigna A/B/C por porcentaje acumulado de ingreso (Pareto).

    Con `agrupar_por`, el Pareto se corre **dentro de cada grupo**: los
    porcentajes son sobre el ingreso de ese padre, no del total.

    El corte se evalúa sobre el acumulado ANTERIOR al ítem: la unidad que
    cruza la línea del 80% sigue siendo A, porque es parte del grupo que
    llega hasta ahí. Con el acumulado posterior, una unidad que concentrara
    el 100% del ingreso quedaría clasificada C.

    Las unidades sin consumo no se fuerzan a C: no aportan poco, no aportan
    nada, y mezclarlas diluiría la clase C y correría los cortes.
    """
    orden = (["padre"] if agrupar_por else []) + ["valor_consumo"]
    df = df.sort_values(orden, ascending=[True] * bool(agrupar_por) + [False]).reset_index(
        drop=True
    )

    if agrupar_por:
        total = df.groupby(agrupar_por)["valor_consumo"].transform("sum")
        acumulado = df.groupby(agrupar_por)["valor_consumo"].cumsum()
    else:
        total = df["valor_consumo"].sum()
        acumulado = df["valor_consumo"].cumsum()

    df["pct_valor"] = np.where(total > 0, df["valor_consumo"] / total * 100, 0.0)
    df["pct_acumulado"] = np.where(total > 0, acumulado / total * 100, 0.0)
    previo = df["pct_acumulado"] - df["pct_valor"]

    df["abc"] = np.where(
        df["valor_consumo"] <= 0,
        SIN_CONSUMO,
        np.where(previo < UMBRAL_A_PCT, "A", np.where(previo < UMBRAL_B_PCT, "B", "C")),
    )
    df.loc[df["abc"] == SIN_CONSUMO, ["pct_valor", "pct_acumulado"]] = np.nan
    return df


def _calcular_xyz(mov: pd.DataFrame, meses: list[str]) -> pd.DataFrame:
    """Coeficiente de variación de la demanda mensual, y su clase X/Y/Z.

    Los meses sin venta cuentan como cero — es la práctica estándar y es lo
    correcto: una unidad que vende dos meses de seis tiene demanda
    intermitente, y eso es exactamente lo que XYZ debe capturar.

    Se expone `meses_con_venta` porque con una ventana de 6 meses una
    unidad **nueva** también sale con CV alto, y ahí la variabilidad no es
    del producto sino de que antes no existía.
    """
    vacio = pd.DataFrame(columns=["clave", "cv", "meses_con_venta", "xyz"])
    if mov.empty:
        return vacio

    piv = (
        mov.pivot_table(index="clave", columns="mes", values="cantidad_comprada", aggfunc="sum")
        .reindex(columns=meses)
        .fillna(0)
    )
    if piv.empty:
        return vacio

    media = piv.mean(axis=1)
    # ddof=0: se describe la variabilidad observada de estos meses, no se
    # estima la de una población mayor.
    cv = (piv.std(axis=1, ddof=0) / media.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

    xyz = pd.DataFrame(
        {
            "clave": piv.index,
            "cv": cv.to_numpy(),
            "meses_con_venta": (piv > 0).sum(axis=1).to_numpy(),
        }
    )
    xyz["xyz"] = np.where(
        xyz["cv"].isna(),
        None,
        np.where(xyz["cv"] <= CV_ESTABLE, "X", np.where(xyz["cv"] <= CV_VARIABLE, "Y", "Z")),
    )
    return xyz
