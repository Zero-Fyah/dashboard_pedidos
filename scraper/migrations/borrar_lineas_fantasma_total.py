"""
borrar_lineas_fantasma_total.py
Script de migración de única ejecución.

Corrige el bug encontrado por el Arquitecto el 2026-08-12 (ver
docs/decisions.md, DEC-113): `_JS_SUBPEDIDOS` en `scraper/extractores.py`
seleccionaba `div.goods-table-row` sin excluir la fila de resumen
(`class="goods-table-row summary-row"`, texto "Total") que la SPA agrega
al final de cada tabla de líneas — el selector por clase matchea cualquier
elemento que tenga esa clase, sin importar las demás.

Esa fila se guardaba como una línea de producto más en `lineas_pedido`, con
`referencia`/`codigo_barras`/`nombre_producto` vacíos, `numero_caja='Total'`
y `cantidad_comprada` igual a la SUMA de las líneas reales del subpedido —
inflando cualquier agregado (Cantidad comprada, mercancía comprometida,
sumas del ETL) para los pedidos afectados.

El bug es reciente: medido 2026-08-12, afecta solo pedidos con
`fecha` entre 2026-08-04 y 2026-08-12 (1.549 pedidos, 1.749 filas
fantasma), 4,6% de la suma total de `cantidad_comprada` en toda la tabla.

El fix del extractor (mismo commit/sesión) impide que se sigan generando.
Esta migración limpia las que ya quedaron persistidas. Identificación
inequívoca: `numero_caja = 'Total'` — ningún número de caja real es ese
texto (es un descriptor de peso/piezas, ver HAL-012).

Uso (desde la raíz del proyecto):
    python scraper/migrations/borrar_lineas_fantasma_total.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "pedidos.db"

QUERY_CONTAR = "SELECT COUNT(*), COALESCE(SUM(cantidad_comprada), 0) FROM lineas_pedido WHERE numero_caja = 'Total'"

QUERY_BORRAR = "DELETE FROM lineas_pedido WHERE numero_caja = 'Total'"


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB no encontrada en {DB_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    try:
        filas, cantidad = con.execute(QUERY_CONTAR).fetchone()
        print(f"Filas fantasma ('Total') a borrar: {filas:,}")
        print(f"cantidad_comprada acumulada en esas filas: {cantidad:,.0f}")

        if filas == 0:
            print("Nada que hacer — no hay filas fantasma (o ya se limpiaron).")
            sys.exit(0)

        confirmacion = (
            input(
                f"\n¿Confirmas borrar estas {filas:,} filas de lineas_pedido? (escribe 's' para confirmar): "
            )
            .strip()
            .lower()
        )

        if confirmacion != "s":
            print("Cancelado sin cambios.")
            sys.exit(0)

        con.execute(QUERY_BORRAR)
        con.commit()

        restantes = con.execute(QUERY_CONTAR).fetchone()[0]
        print(
            f"\nHecho. {filas:,} filas borradas. Restantes con numero_caja='Total': {restantes:,}"
        )
        sys.exit(0)

    except Exception as exc:
        con.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
