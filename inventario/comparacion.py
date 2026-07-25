"""
comparacion.py — Inventario teórico vs. Bochica, separando picking (DEC-041).

Cierra DEC-039: con el layout de bodega ya se puede separar `Ubicación`
en picking (sistemáticamente inflado — el sistema de bodega nunca
descuenta sus movimientos) de altura (confiable), así que la comparación
deja de ser contra un total mezclado.

La fórmula se invierte respecto al piloto naive que este módulo
reemplaza: **altura es el dato confiable y picking la incógnita**.

    inventario_teorico = disponible_venta + vendido_no_alistado
    picking_estimado   = inventario_teorico − bochica_altura
    diferencia         = bochica_picking − picking_estimado

`picking_estimado` negativo es la alerta real y el principal valor de
negocio de la vista: el teórico no alcanza a cubrir ni lo que hay en
altura, un faltante que el sesgo conocido de picking no explica.
"""

import logging
import sqlite3

import pandas as pd

from comun import ESTADOS_ACTIVOS_INVENTARIO, familia_de, get_db_path
from inventario.layout import TIPO_ALTURA, TIPO_PASO, TIPO_PICKING
from inventario.normalizador import ALMACEN_BODEGA, marcar_averias

logger = logging.getLogger("inventario.comparacion")

_ESTADOS_SQL = ", ".join(f"'{e}'" for e in ESTADOS_ACTIVOS_INVENTARIO)

# Regla cerrada en docs/decisions.md DEC-039 pregunta 5 (corregida por
# HAL-013): inicio_inspeccion, no inicio_alistamiento, es el campo que
# marca cuándo terminó el alistamiento físico real. Placeholder '-' (no
# NULL, DEC-025). Validado robusto frente a los dos ciclos de forma_pago
# (addendum HAL-013) sin necesidad de distinguir por forma de pago acá.
#
# El filtro de almacén (DEC-041) acota a la bodega que se está comparando:
# `lineas_pedido.almacen` usa los mismos valores que el catálogo admin
# ('Bogotá' = CEDI Mosquera), y descarta ~24.800 de 812.000 líneas.
_QUERY_VENDIDO_NO_ALISTADO = f"""
    SELECT l.referencia, SUM(l.cantidad_comprada) AS vendido_no_alistado
    FROM lineas_pedido l
    JOIN subpedidos s
      ON l.id_pedido = s.id_pedido AND l.numero_subpedido = s.numero_subpedido
    WHERE LOWER(s.estado) IN ({_ESTADOS_SQL})
      AND s.inicio_inspeccion = '-'
      AND l.referencia IS NOT NULL AND l.referencia != ''
      AND l.almacen = ?
    GROUP BY l.referencia
"""


def calcular_vendido_no_alistado(
    db_path: str | None = None, *, almacen: str = ALMACEN_BODEGA
) -> pd.DataFrame:
    """Cantidad vendida pero aún no alistada físicamente, por referencia.

    `lineas_pedido` no tiene `id_especificacion` (ese campo es propio del
    Excel del admin) — `referencia` es la llave disponible, confirmada
    98,5% de cruce contra el catálogo del admin.

    Args:
        db_path: Ruta a pedidos.db. Default: `comun.get_db_path()`.
        almacen: Almacén a considerar. Default: el de la bodega comparada
            (DEC-041); pasar `None` deshabilita el filtro.

    Returns:
        DataFrame con columnas `referencia`, `vendido_no_alistado`.
    """
    query = _QUERY_VENDIDO_NO_ALISTADO
    params: tuple[str, ...] = (almacen,)
    if almacen is None:
        query = query.replace("AND l.almacen = ?", "")
        params = ()
    with sqlite3.connect(db_path or get_db_path(), timeout=5) as con:
        return pd.read_sql(query, con, params=params)


def comparar(
    df_admin: pd.DataFrame,
    df_bochica_layout: pd.DataFrame,
    df_vendido_no_alistado: pd.DataFrame,
) -> pd.DataFrame:
    """Compara inventario teórico vs. Bochica separando picking de altura (DEC-041).

    Todo se agrega por `referencia` y no por `id_especificacion`: una
    referencia agrupa varias `id_especificacion` (variantes de color/
    talla — confirmado en 1.106 de 1.448 referencias del admin, 76%) y
    `lineas_pedido` (pedidos.db) solo tiene `referencia`. Cada fuente se
    agrega por separado antes de mezclarse — sumar sobre el cruce ya
    explotado por ubicación contaría el inventario del admin una vez por
    cada fila de ubicación de Bochica que matchea.

    El universo de referencias lo define `df_admin` **ya filtrado** por
    `filtrar_alcance_admin()`: es el único origen de verdad de qué
    productos son comparables (excluye la categoría recibida por peso y
    los otros almacenes). Lo que Bochica reporta bajo códigos legados
    fuera de catálogo (~28,5%, DEC-039 pregunta 1) queda fuera por
    construcción, igual que en el piloto naive.

    Args:
        df_admin: `cargar_admin()` pasado por `filtrar_alcance_admin()`.
        df_bochica_layout: `cargar_bochica()` pasado por
            `clasificar_ubicaciones()` y `solo_layout()` — necesita la
            columna `tipo`.
        df_vendido_no_alistado: Resultado de `calcular_vendido_no_alistado()`.

    Returns:
        DataFrame por `referencia` con `familia`, `es_averia`,
        `disponible_venta`, `vendido_no_alistado`, `inventario_teorico`,
        `bochica_altura`, `bochica_picking`, `bochica_paso`,
        `picking_estimado` e `diferencia`.
    """
    if "tipo" not in df_bochica_layout.columns:
        raise ValueError(
            "df_bochica_layout no trae la columna 'tipo': pasalo antes por "
            "inventario.layout.clasificar_ubicaciones() (DEC-041)."
        )

    df_admin = df_admin.copy()
    df_admin["es_averia"] = marcar_averias(df_admin)

    disponible = df_admin.groupby("referencia", as_index=False).agg(
        disponible_venta=("inventario", "sum"), es_averia=("es_averia", "any")
    )

    # Bochica trae `id_especificacion`; la traducción a `referencia` sale
    # del catálogo admin (inner join deliberado, ver docstring).
    mapa_id_a_referencia = df_admin[["id_especificacion", "referencia"]].drop_duplicates(
        "id_especificacion"
    )
    bochica = df_bochica_layout[["id_especificacion", "cantidad", "tipo"]].merge(
        mapa_id_a_referencia, on="id_especificacion", how="inner"
    )
    por_tipo = (
        bochica.pivot_table(
            index="referencia", columns="tipo", values="cantidad", aggfunc="sum", fill_value=0
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )
    renombre = {
        TIPO_ALTURA: "bochica_altura",
        TIPO_PICKING: "bochica_picking",
        TIPO_PASO: "bochica_paso",
    }
    por_tipo = por_tipo.rename(columns=renombre)
    for columna in renombre.values():
        if columna not in por_tipo.columns:
            por_tipo[columna] = 0.0

    comparado = disponible.merge(
        por_tipo[["referencia", "bochica_altura", "bochica_picking", "bochica_paso"]],
        on="referencia",
        how="left",
    ).merge(df_vendido_no_alistado, on="referencia", how="left")

    numericas = (
        "disponible_venta",
        "vendido_no_alistado",
        "bochica_altura",
        "bochica_picking",
        "bochica_paso",
    )
    for columna in numericas:
        comparado[columna] = comparado[columna].fillna(0)
    comparado["es_averia"] = comparado["es_averia"].fillna(False)

    comparado["familia"] = comparado["referencia"].map(familia_de)
    comparado["inventario_teorico"] = (
        comparado["disponible_venta"] + comparado["vendido_no_alistado"]
    )
    # Altura es el dato confiable; picking es la incógnita que se estima.
    comparado["picking_estimado"] = comparado["inventario_teorico"] - comparado["bochica_altura"]
    comparado["diferencia"] = comparado["bochica_picking"] - comparado["picking_estimado"]

    columnas = [
        "referencia",
        "familia",
        "es_averia",
        "disponible_venta",
        "vendido_no_alistado",
        "inventario_teorico",
        "bochica_altura",
        "bochica_picking",
        "bochica_paso",
        "picking_estimado",
        "diferencia",
    ]
    return comparado[columnas].sort_values("referencia").reset_index(drop=True)


def anomalias_layout(df_bochica_layout: pd.DataFrame) -> pd.DataFrame:
    """Lista el stock que está donde no debería estar, según el layout (DEC-041).

    Tres situaciones, todas derivadas de la semántica de
    `Activa Para Almacenar` confirmada por el Arquitecto. Ninguna cambia
    el cálculo — se reportan para que la operación las corrija:

    - `paso_montacarga`: stock en el túnel transversal del rack.
    - `estiba_nivel_superior`: stock en alturas 2-3 de una posición donde
      el bulto ocupa los 3 niveles y todo debe registrarse en la altura 1.
    - `posicion_no_habilitada`: stock en posiciones marcadas `NO` en las
      tres alturas de picking.

    Args:
        df_bochica_layout: Resultado de `clasificar_ubicaciones()` +
            `solo_layout()` — necesita `tipo`, `activa`, `altura` y
            `estiba_completa`.

    Returns:
        DataFrame con `motivo`, `ubicacion`, `id_especificacion` y
        `cantidad`, ordenado por cantidad descendente. Vacío si no hay
        anomalías.
    """
    df = df_bochica_layout
    con_stock = df["cantidad"] > 0

    es_paso = df["tipo"] == TIPO_PASO
    # En picking, `activa == "NO"` cubre dos casos distintos: el nivel
    # superior de una estiba completa (esperado que esté vacío porque el
    # stock se registra en la altura 1) y una posición deshabilitada del
    # todo. Los separa `estiba_completa`, no la altura: una posición
    # NO/NO/NO también tiene alturas 2 y 3.
    es_picking_inactivo = (df["tipo"] == TIPO_PICKING) & (df["activa"] == "NO")
    es_estiba = df["estiba_completa"].fillna(False).astype(bool)

    motivo = pd.Series("", index=df.index, dtype="object")
    motivo[es_picking_inactivo] = "posicion_no_habilitada"
    motivo[es_picking_inactivo & es_estiba & (df["altura"] > 1)] = "estiba_nivel_superior"
    motivo[es_paso] = "paso_montacarga"

    resultado = df[con_stock & (motivo != "")].copy()
    resultado["motivo"] = motivo[resultado.index]
    columnas = ["motivo", "ubicacion", "id_especificacion", "cantidad"]
    return resultado[columnas].sort_values("cantidad", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    import sys

    from inventario.layout import (
        cargar_layout,
        clasificar_ubicaciones,
        resumen_por_tipo,
        solo_layout,
    )
    from inventario.normalizador import cargar_admin, cargar_bochica, filtrar_alcance_admin
    from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
    from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not ADMIN_XLSX.exists() or not BOCHICA_XLSX.exists():
        print(
            f"Faltan archivos fuente. Corré antes:\n"
            f"  python -m scraper.inventario  (-> {ADMIN_XLSX})\n"
            f"  python -m scraper.bochica     (-> {BOCHICA_XLSX})"
        )
        sys.exit(1)

    admin = filtrar_alcance_admin(cargar_admin(ADMIN_XLSX))
    layout = cargar_layout()
    bochica_clasificada = clasificar_ubicaciones(cargar_bochica(BOCHICA_XLSX), layout)
    print()
    print("Bochica por tipo de ubicación:")
    print(resumen_por_tipo(bochica_clasificada).to_string())
    print()

    bochica = solo_layout(bochica_clasificada)
    resultado = comparar(admin, bochica, calcular_vendido_no_alistado())

    print()
    print(f"Referencias comparadas: {len(resultado)}")
    print(f"  disponible_venta      {resultado['disponible_venta'].sum():>12,.0f}")
    print(f"  vendido_no_alistado   {resultado['vendido_no_alistado'].sum():>12,.0f}")
    print(f"  inventario_teorico    {resultado['inventario_teorico'].sum():>12,.0f}")
    print(f"  bochica_altura        {resultado['bochica_altura'].sum():>12,.0f}  (confiable)")
    print(f"  bochica_picking       {resultado['bochica_picking'].sum():>12,.0f}  (inflado)")
    print(f"  picking_estimado      {resultado['picking_estimado'].sum():>12,.0f}")
    print(f"  diferencia            {resultado['diferencia'].sum():>12,.0f}")
    print()

    negativos = resultado[resultado["picking_estimado"] < 0]
    print(
        f"⚠️  {len(negativos)} referencias con picking_estimado NEGATIVO "
        f"({negativos['picking_estimado'].sum():,.0f} unidades) — el teórico no "
        "cubre ni lo que hay en altura."
    )
    print(
        negativos.nsmallest(10, "picking_estimado")[
            ["referencia", "familia", "inventario_teorico", "bochica_altura", "picking_estimado"]
        ].to_string(index=False)
    )
    print()
    print("Por familia:")
    print(
        resultado.groupby(resultado["familia"].fillna("SIN_FAMILIA"))[
            ["inventario_teorico", "bochica_altura", "bochica_picking", "diferencia"]
        ]
        .sum()
        .to_string()
    )
    print()
    anomalias = anomalias_layout(bochica)
    print(
        f"Anomalías de ubicación: {len(anomalias)} filas, {anomalias['cantidad'].sum():,.0f} unidades"
    )
    print(anomalias.groupby("motivo")["cantidad"].agg(["size", "sum"]).to_string())
