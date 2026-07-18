"""
corregir_entrega_ruta.py
Script de migración de única ejecución.

Corrige el cruce de campos de BUG-019 (ver docs/decisions.md, DEC-023).

Hasta el 2026-07-18 `extraer_info_entrega()` leía la tabla de entrega por
posición de fila. Con `metodo_entrega = 'Ruta'` la SPA renderiza dos filas
adicionales (Conductor y Vehículo de entrega), de modo que:

    hora_entrega  ← recibía el CONDUCTOR
    obs_entrega   ← recibía el VEHÍCULO

Esta migración mueve cada valor a su columna correcta y deja
`hora_entrega` / `obs_entrega` en NULL para esos pedidos: su valor real
nunca se capturó y solo se recuperaría con un re-scrape.

Solo toca pedidos con `metodo_entrega = 'Ruta'`. Los métodos
'Transportadora' y 'Almacen' no están afectados (su layout no tiene las
filas extra) y se dejan intactos.

PRECONDICIÓN: ejecutar DESPUÉS de desplegar el fix del scraper (columnas
`conductor` y `vehiculo_entrega` creadas por init_db).

Uso (desde la raíz del proyecto):
    python scraper/migrations/corregir_entrega_ruta.py
"""

import sqlite3
import sys
from pathlib import Path

# Dos niveles arriba desde scraper/migrations/ llega a la raíz
DB_PATH = Path(__file__).parent.parent.parent / "data" / "pedidos.db"

QUERY_CONTAR = """
    SELECT COUNT(*) FROM pedidos
    WHERE metodo_entrega = 'Ruta'
      AND (conductor IS NULL OR conductor = '')
      AND hora_entrega IS NOT NULL
"""

# Los valores desplazados se mueven a su columna real. hora_entrega y
# obs_entrega quedan NULL: lo que contenían no era su dato.
QUERY_MIGRAR = """
    UPDATE pedidos
    SET conductor        = hora_entrega,
        vehiculo_entrega = obs_entrega,
        hora_entrega     = NULL,
        obs_entrega      = NULL
    WHERE metodo_entrega = 'Ruta'
      AND (conductor IS NULL OR conductor = '')
      AND hora_entrega IS NOT NULL
"""

QUERY_VERIFICAR = """
    SELECT
        SUM(CASE WHEN conductor IS NOT NULL AND conductor != '' THEN 1 ELSE 0 END),
        SUM(CASE WHEN vehiculo_entrega IS NOT NULL AND vehiculo_entrega != ''
                 THEN 1 ELSE 0 END),
        COUNT(*)
    FROM pedidos WHERE metodo_entrega = 'Ruta'
"""


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB no encontrada en {DB_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    try:
        cols = {c[1] for c in con.execute("PRAGMA table_info(pedidos)").fetchall()}
        faltantes = {"conductor", "vehiculo_entrega"} - cols
        if faltantes:
            print(
                f"ERROR: faltan columnas {sorted(faltantes)} — ejecuta el scraper "
                "una vez para que init_db aplique la migración de schema.",
                file=sys.stderr,
            )
            sys.exit(1)

        afectados = con.execute(QUERY_CONTAR).fetchone()[0]
        print(f"Pedidos de Ruta a corregir: {afectados:,}")

        if afectados == 0:
            print("Nada que hacer — la migración ya se aplicó.")
            sys.exit(0)

        confirmacion = (
            input(
                f"\n¿Confirmas mover conductor/vehículo a sus columnas en "
                f"{afectados:,} pedidos? (escribe 's' para confirmar): "
            )
            .strip()
            .lower()
        )

        if confirmacion != "s":
            print("Cancelado sin cambios.")
            sys.exit(0)

        con.execute(QUERY_MIGRAR)
        con.commit()

        con_cond, con_veh, total = con.execute(QUERY_VERIFICAR).fetchone()
        print(f"\nHecho. {afectados:,} pedidos corregidos.")
        print(f"  Ruta con conductor:  {con_cond:,} de {total:,}")
        print(f"  Ruta con vehículo:   {con_veh:,} de {total:,}")
        print(
            "\nNota: hora_entrega y obs_entrega de esos pedidos quedaron NULL "
            "(su valor real nunca se capturó). Se llenarán si algún día se "
            "re-scrapean."
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
