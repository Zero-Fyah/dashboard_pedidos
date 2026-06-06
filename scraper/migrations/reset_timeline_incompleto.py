"""
reset_timeline_incompleto.py
Script de migración de única ejecución.

Resetea scraping_completo = 0 en pedidos sin datos en timeline_pedido,
causados por el bug de espera fija (ver docs/decisions.md, BUG-012).

PRECONDICIÓN: ejecutar DESPUÉS de desplegar la corrección del scraper,
no antes. De lo contrario el re-scraping usará la versión con el bug.

IMPORTANTE: después de este script es obligatorio ejecutar el scraper
con --desde para recuperar los pedidos reseteados. El modo incremental
solo procesa pedidos con scraping_completo=1 y NO recuperará estos.

Uso (desde la raíz del proyecto):
    python scraper/migrations/reset_timeline_incompleto.py
"""
import sys
import sqlite3
from pathlib import Path

# Dos niveles arriba desde scraper/migrations/ llega a la raíz
DB_PATH = Path(__file__).parent.parent.parent / "data" / "pedidos.db"

QUERY_CONTAR = """
    SELECT COUNT(*) FROM pedidos p
    WHERE scraping_completo = 1
      AND NOT EXISTS (
          SELECT 1 FROM timeline_pedido t
          WHERE t.id_pedido = p.id_pedido
      )
"""

QUERY_RESETEAR = """
    UPDATE pedidos
    SET scraping_completo = 0
    WHERE scraping_completo = 1
      AND NOT EXISTS (
          SELECT 1 FROM timeline_pedido t
          WHERE t.id_pedido = pedidos.id_pedido
      )
"""

def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB no encontrada en {DB_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    try:
        afectados = con.execute(QUERY_CONTAR).fetchone()[0]
        print(f"Pedidos a resetear: {afectados:,}")

        if afectados == 0:
            print("Nada que hacer — todos los pedidos tienen timeline.")
            sys.exit(0)

        confirmacion = input(
            f"\n¿Confirmas resetear {afectados:,} pedidos "
            f"para re-scraping? (escribe 's' para confirmar): "
        ).strip().lower()

        if confirmacion != "s":
            print("Cancelado sin cambios.")
            sys.exit(0)

        con.execute(QUERY_RESETEAR)
        con.commit()
        print(f"\nHecho. {afectados:,} pedidos marcados para re-scraping.")
        print("\nPróximo paso OBLIGATORIO:")
        print("  python scraper/scraper_principal.py --desde 2026-05-01")
        sys.exit(0)

    except Exception as exc:
        con.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()

if __name__ == "__main__":
    main()
