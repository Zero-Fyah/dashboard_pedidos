"""
comparacion.py — Comparación NAIVE inventario teórico vs. Bochica (DEC-039).

**Temporal a propósito.** Sin el layout de bodega (documento 3, aún
pendiente) no se puede separar `Ubicación` en picking (sistemáticamente
inflado — nunca se descuenta, ver DEC-039) de altura (confiable). Este
módulo compara contra el TOTAL de Bochica (todas las ubicaciones juntas),
así que `diferencia` va a mostrar sesgo en casi todos los productos por
esa razón ya conocida, no necesariamente por un problema real de
inventario. Es plomería de punta a punta, no el resultado de negocio
final — se reemplaza el último paso (`comparar_naive`) cuando el layout
permita separar ubicaciones; `calcular_vendido_no_alistado()` y el resto
de `inventario/normalizador.py` no cambian.
"""

import sqlite3

import pandas as pd

from comun import ESTADOS_ACTIVOS_INVENTARIO, get_db_path

_ESTADOS_SQL = ", ".join(f"'{e}'" for e in ESTADOS_ACTIVOS_INVENTARIO)

# Regla cerrada en docs/decisions.md DEC-039 pregunta 5 (corregida por
# HAL-013): inicio_inspeccion, no inicio_alistamiento, es el campo que
# marca cuándo terminó el alistamiento físico real. Placeholder '-' (no
# NULL, DEC-025). Validado robusto frente a los dos ciclos de forma_pago
# (addendum HAL-013) sin necesidad de distinguir por forma de pago acá.
_QUERY_VENDIDO_NO_ALISTADO = f"""
    SELECT l.referencia, SUM(l.cantidad_comprada) AS vendido_no_alistado
    FROM lineas_pedido l
    JOIN subpedidos s
      ON l.id_pedido = s.id_pedido AND l.numero_subpedido = s.numero_subpedido
    WHERE LOWER(s.estado) IN ({_ESTADOS_SQL})
      AND s.inicio_inspeccion = '-'
      AND l.referencia IS NOT NULL AND l.referencia != ''
    GROUP BY l.referencia
"""


def calcular_vendido_no_alistado(db_path: str | None = None) -> pd.DataFrame:
    """Cantidad vendida pero aún no alistada físicamente, por referencia.

    `lineas_pedido` no tiene `id_especificacion` (ese campo es propio del
    Excel del admin) — `referencia` es la llave disponible, confirmada
    98,5% de cruce contra el catálogo del admin (medido en esta sesión).

    Args:
        db_path: Ruta a pedidos.db. Default: `comun.get_db_path()`.

    Returns:
        DataFrame con columnas `referencia`, `vendido_no_alistado`.
    """
    with sqlite3.connect(db_path or get_db_path(), timeout=5) as con:
        return pd.read_sql(_QUERY_VENDIDO_NO_ALISTADO, con)


def comparar_naive(
    df_admin: pd.DataFrame,
    df_bochica: pd.DataFrame,
    df_vendido_no_alistado: pd.DataFrame,
) -> pd.DataFrame:
    """Compara inventario_teórico vs. el total de Bochica, agregado por `referencia`.

    Todo se agrega por `referencia` y no por `id_especificacion`: una
    referencia agrupa varias `id_especificacion` (variantes de color/
    talla — confirmado en 1.106 de 1.448 referencias del admin, 76%) y
    `lineas_pedido` (pedidos.db) solo tiene `referencia`. Agregar admin y
    Bochica cada uno por separado (en vez de sumar sobre el cruce ya
    explotado por ubicación de `cruzar_inventarios()`) evita contar el
    inventario del admin una vez por cada fila de ubicación de Bochica
    que matchea.

    Bochica se traduce de `id_especificacion` a `referencia` vía el
    propio `df_admin` con un inner join deliberado: las filas de Bochica
    sin match en el catálogo admin (28,5% medido en DEC-039 pregunta 1 —
    códigos legados) quedan fuera de esta comparación "por referencia
    admin", mismo criterio ya documentado, no es un bug nuevo.

    **Temporal — ver docstring del módulo.**

    Args:
        df_admin: Resultado de `inventario.normalizador.cargar_admin()`.
        df_bochica: Resultado de `inventario.normalizador.cargar_bochica()`.
        df_vendido_no_alistado: Resultado de `calcular_vendido_no_alistado()`.

    Returns:
        DataFrame con `referencia`, `disponible_venta`,
        `vendido_no_alistado`, `inventario_teorico`, `bochica_total`,
        `diferencia` (bochica_total - inventario_teorico).
    """
    disponible = (
        df_admin.groupby("referencia", as_index=False)["inventario"]
        .sum()
        .rename(columns={"inventario": "disponible_venta"})
    )

    mapa_id_a_referencia = df_admin[["id_especificacion", "referencia"]].drop_duplicates()
    bochica_con_referencia = df_bochica[["id_especificacion", "cantidad"]].merge(
        mapa_id_a_referencia, on="id_especificacion", how="inner"
    )
    bochica_total = (
        bochica_con_referencia.groupby("referencia", as_index=False)["cantidad"]
        .sum()
        .rename(columns={"cantidad": "bochica_total"})
    )

    comparado = disponible.merge(bochica_total, on="referencia", how="outer")
    comparado = comparado.merge(df_vendido_no_alistado, on="referencia", how="outer")
    for col in ("disponible_venta", "bochica_total", "vendido_no_alistado"):
        comparado[col] = comparado[col].fillna(0)

    comparado["inventario_teorico"] = (
        comparado["disponible_venta"] + comparado["vendido_no_alistado"]
    )
    comparado["diferencia"] = comparado["bochica_total"] - comparado["inventario_teorico"]

    columnas = [
        "referencia",
        "disponible_venta",
        "vendido_no_alistado",
        "inventario_teorico",
        "bochica_total",
        "diferencia",
    ]
    return comparado[columnas].sort_values("referencia").reset_index(drop=True)


if __name__ == "__main__":
    import sys

    from inventario.normalizador import cargar_admin, cargar_bochica
    from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
    from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

    if not ADMIN_XLSX.exists() or not BOCHICA_XLSX.exists():
        print(
            f"Faltan archivos fuente. Corré antes:\n"
            f"  python -m scraper.inventario  (-> {ADMIN_XLSX})\n"
            f"  python -m scraper.bochica     (-> {BOCHICA_XLSX})"
        )
        sys.exit(1)

    admin = cargar_admin(ADMIN_XLSX)
    bochica = cargar_bochica(BOCHICA_XLSX)
    vendido_no_alistado = calcular_vendido_no_alistado()

    resultado = comparar_naive(admin, bochica, vendido_no_alistado)

    print(f"Referencias comparadas: {len(resultado)}")
    print(f"Suma disponible_venta:     {resultado['disponible_venta'].sum():>12,.0f}")
    print(f"Suma vendido_no_alistado:  {resultado['vendido_no_alistado'].sum():>12,.0f}")
    print(f"Suma inventario_teorico:   {resultado['inventario_teorico'].sum():>12,.0f}")
    print(f"Suma bochica_total:        {resultado['bochica_total'].sum():>12,.0f}")
    print(f"Suma diferencia:           {resultado['diferencia'].sum():>12,.0f}")
    print()
    print("⚠️  Comparación NAIVE — mezcla picking (inflado) y altura (confiable).")
    print("    No es el resultado de negocio final, ver docstring del módulo.")
    print()
    print("Top 10 mayores diferencias absolutas:")
    print(
        resultado.reindex(resultado["diferencia"].abs().sort_values(ascending=False).index)
        .head(10)
        .to_string(index=False)
    )
