"""
cargar_inicial_movimientos_bochica.py
Script de migración de única ejecución.

Carga la exportación inicial de "Movimiento Inventario" de BOCHICA (38.056
filas, 2026-01-02 a 2026-08-25) que el Arquitecto pegó como texto tabulado
desde la propia vista de la app — mismo patrón que
`backfill_cambios_inventario.py` para el sistema administrativo.

Uso (desde la raíz del proyecto):
    python scraper/migrations/cargar_inicial_movimientos_bochica.py [ruta]

Si se omite `ruta`, usa la que el Arquitecto compartió en sesión:
    C:\\Users\\usuario\\Downloads\\Carga_Inicial_Movimiento_Inventario_BOCHICA.txt
"""

import sys
from pathlib import Path

from comun import get_db_path
from scraper.movimientos_bochica import cargar_movimientos, init_schema

RUTA_DEFAULT = Path(r"C:\Users\usuario\Downloads\Carga_Inicial_Movimiento_Inventario_BOCHICA.txt")


def main() -> int:
    ruta = Path(sys.argv[1]) if len(sys.argv) > 1 else RUTA_DEFAULT
    if not ruta.exists():
        print(f"ERROR: no se encontró {ruta}", file=sys.stderr)
        return 1

    db_path = get_db_path()
    init_schema(db_path)

    insertadas = cargar_movimientos(ruta, db_path, origen="backfill")
    print(f"{insertadas:,} filas nuevas insertadas".replace(",", "."))
    print(
        "Nota: si la carga ya corrió antes, las filas repetidas se ignoran "
        "(INSERT OR IGNORE) — volver a correrla es segura."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
