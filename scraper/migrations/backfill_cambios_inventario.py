"""
backfill_cambios_inventario.py
Script de migración de única ejecución.

Carga el histórico de "Cambios de inventario" (TASK-001) que el Arquitecto
descargó a mano en tres bloques trimestrales, 2026-01-01 a 2026-08-13
(1.134.916 filas medidas en el análisis previo de la sesión).

A diferencia del resto de scraper/migrations/, esto no corrige un bug —
puebla movimientos_inventario por primera vez con datos que ya existían en
el sistema origen antes de que existiera el extractor diario
(scraper/cambios_inventario.py). Corre una sola vez, a mano, fuera del
scheduler; la captura diaria en adelante la hace el módulo principal.

Uso (desde la raíz del proyecto, con los tres .xlsx en la ruta indicada):
    python scraper/migrations/backfill_cambios_inventario.py [carpeta]

Si se omite `carpeta`, usa la ruta que el Arquitecto compartió en sesión:
    C:\\Users\\usuario\\Downloads\\Cambios de inventario\\
"""

import sys
from pathlib import Path

from comun import get_db_path
from scraper.cambios_inventario import cargar_movimientos, init_schema

CARPETA_DEFAULT = Path(r"C:\Users\usuario\Downloads\Cambios de inventario")

ARCHIVOS_ESPERADOS = (
    "Cambios de inventario.xlsx",
    "Cambios de inventario (1).xlsx",
    "Cambios de inventario (2).xlsx",
)


def main() -> int:
    carpeta = Path(sys.argv[1]) if len(sys.argv) > 1 else CARPETA_DEFAULT
    rutas = [carpeta / nombre for nombre in ARCHIVOS_ESPERADOS]

    faltantes = [str(r) for r in rutas if not r.exists()]
    if faltantes:
        print("ERROR: no se encontraron estos archivos:", file=sys.stderr)
        for f in faltantes:
            print(f"  - {f}", file=sys.stderr)
        return 1

    db_path = get_db_path()
    init_schema(db_path)

    total_insertadas = 0
    for ruta in rutas:
        print(f"Cargando {ruta.name}...")
        insertadas = cargar_movimientos(ruta, db_path, origen="backfill")
        total_insertadas += insertadas
        print(f"  {insertadas:,} filas nuevas insertadas".replace(",", "."))

    print(f"\nTotal insertado en esta corrida: {total_insertadas:,} filas".replace(",", "."))
    print(
        "Nota: si el backfill ya corrió antes, las filas repetidas se ignoran "
        "(INSERT OR IGNORE) — volver a correrlo es seguro."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
