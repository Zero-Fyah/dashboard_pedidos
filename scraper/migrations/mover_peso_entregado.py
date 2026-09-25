"""
mover_peso_entregado.py
Script de migración de única ejecución (DEC-140).

Entre el 2026-08-18 y el 2026-08-19 el origen agregó la columna «Peso
entregado» en la tabla de productos, entre «Peso pedido» y «Observaciones».
`_JS_SUBPEDIDOS` leía por posición, así que desde entonces
`lineas_pedido.observaciones` guardó el peso entregado (medido 2026-09-23:
255.299 líneas en 8.126 pedidos, y creciendo con cada ciclo hasta el fix).

Esta migración repara lo ya persistido:

1. Mueve a `peso_entregado` todo valor de `observaciones` con forma de
   peso (`*[0-9]g`, `*[0-9]KG`) y deja `observaciones` en NULL — no en
   '-': la observación real de esas líneas nunca se leyó. Es seguro: en
   1.135.364 líneas hubo UNA sola observación real (2 líneas de prueba) y
   cero con forma de peso antes del corrimiento.
2. Pone en NULL `peso_total_num` de las líneas en kilos, para que el ETL
   las reconvierta a gramos con `a_gramos()`. Antes `to_num('4.5KG')`
   daba 45 y la columna mezclaba gramos con kilos.

La re-extracción del histórico (DEC-140, por tandas) repone después las
observaciones reales y el peso entregado de todas las líneas.

Idempotente: una segunda corrida no encuentra nada que mover. Hace un
respaldo completo de `pedidos.db` antes de escribir. Correrlo ENTRE
ciclos del timer (el ciclo toma el lock de escritura ~30 min cada hora).

Uso (desde la raíz del proyecto):
    python scraper/migrations/mover_peso_entregado.py            # solo cuenta
    python scraper/migrations/mover_peso_entregado.py --aplicar  # respalda y aplica
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "pedidos.db"

FORMA_PESO = "(observaciones GLOB '*[0-9]g' OR observaciones GLOB '*[0-9]KG')"

QUERY_CONTAR_OBS = (
    f"SELECT COUNT(*), COUNT(DISTINCT id_pedido) FROM lineas_pedido WHERE {FORMA_PESO}"
)

QUERY_MOVER = f"""
UPDATE lineas_pedido
   SET peso_entregado = COALESCE(peso_entregado, observaciones),
       observaciones  = NULL
 WHERE {FORMA_PESO}
"""

QUERY_CONTAR_KG = (
    "SELECT COUNT(*) FROM lineas_pedido WHERE peso_total GLOB '*KG' AND peso_total_num IS NOT NULL"
)

QUERY_RESET_KG = (
    "UPDATE lineas_pedido SET peso_total_num = NULL "
    "WHERE peso_total GLOB '*KG' AND peso_total_num IS NOT NULL"
)


def _columnas(con: sqlite3.Connection) -> set[str]:
    return {fila[1] for fila in con.execute("PRAGMA table_info(lineas_pedido)")}


def main() -> None:
    aplicar = "--aplicar" in sys.argv[1:]
    if not DB_PATH.exists():
        print(f"ERROR: DB no encontrada en {DB_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH, timeout=60)
    try:
        lineas, pedidos = con.execute(QUERY_CONTAR_OBS).fetchone()
        tiene_num = "peso_total_num" in _columnas(con)
        kg = con.execute(QUERY_CONTAR_KG).fetchone()[0] if tiene_num else 0
        print(f"Líneas con peso en observaciones: {lineas:,} ({pedidos:,} pedidos)")
        print(f"Líneas en KG con peso_total_num a reconvertir: {kg:,}")

        if lineas == 0 and kg == 0:
            print("Nada que hacer — ya se aplicó o no hay casos.")
            sys.exit(0)
        if not aplicar:
            print("\nSolo conteo. Correr con --aplicar para respaldar y escribir.")
            sys.exit(0)

        confirmacion = (
            input("\n¿Confirmas respaldar pedidos.db y aplicar? (escribe 's' para confirmar): ")
            .strip()
            .lower()
        )
        if confirmacion != "s":
            print("Cancelado sin cambios.")
            sys.exit(0)

        destino = DB_PATH.with_name(f"pedidos_pre_DEC140_{datetime.now():%Y-%m-%d_%H%M}.db")
        print(f"Respaldando en {destino.name} ...")
        with sqlite3.connect(destino) as copia:
            con.backup(copia)
        print("Respaldo OK.")

        # La columna la crea la migración del scraper; se asegura acá por si
        # el script corre antes del primer ciclo con el código nuevo.
        if "peso_entregado" not in _columnas(con):
            con.execute("ALTER TABLE lineas_pedido ADD COLUMN peso_entregado TEXT DEFAULT NULL")

        con.execute("BEGIN IMMEDIATE")
        movidas = con.execute(QUERY_MOVER).rowcount
        reseteadas = con.execute(QUERY_RESET_KG).rowcount if tiene_num else 0
        con.commit()

        restantes = con.execute(QUERY_CONTAR_OBS).fetchone()[0]
        print(
            f"\nHecho. {movidas:,} líneas movidas a peso_entregado, "
            f"{reseteadas:,} pesos en KG marcados para reconvertir. "
            f"Restantes con peso en observaciones: {restantes:,}"
        )
        print("Siguiente paso: correr el ETL (python -m etl.etl_principal).")
        sys.exit(0)

    except Exception as exc:
        con.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
