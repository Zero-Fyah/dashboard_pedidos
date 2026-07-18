"""
separar_descuento_tipo.py
Script de migración de única ejecución.

Puebla `descuento_tipo` en `lineas_pedido` y `detalle_diferencias` a partir
de los datos ya almacenados, sin re-scrapear (ver docs/decisions.md,
DEC-024).

Tres casos, todos recuperables desde la columna `descuento`:

1. Filas legadas con salto de línea ("-\\nTipo de cambio3%"): la extracción
   anterior al 2026-07-02 concatenaba la celda entera. La primera línea es
   la ranura del monto y el resto son etiquetas.
2. Filas cuya `descuento` es una etiqueta ("Promoción3%"): la ranura del
   monto resolvía al tag. La etiqueta pasa a `descuento_tipo` y el monto
   queda en "-".
3. Filas con "-" limpio: no hay etiqueta que recuperar — el tipo se
   poblará en el próximo scrape del pedido, si sigue activo.

PRECONDICIÓN: ejecutar DESPUÉS de desplegar el scraper con DEC-024
(columnas `descuento_tipo` creadas por init_db).

Uso (desde la raíz del proyecto):
    python scraper/migrations/separar_descuento_tipo.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from comun import to_num  # noqa: E402

DB_PATH = Path(__file__).parent.parent.parent / "data" / "pedidos.db"

TABLAS = ("lineas_pedido", "detalle_diferencias")


def separar(valor: str) -> tuple[str, str]:
    """Separa un `descuento` almacenado en (monto, tipos).

    Misma semántica que `leer_celda_descuento()` del extractor, aplicada
    al texto ya persistido.
    """
    partes = [p.strip() for p in valor.split("\n") if p.strip()]
    if not partes:
        return ("-", "")

    monto = partes[0]
    etiquetas = partes[1:]

    # La ranura trae una etiqueta en vez de un monto.
    if monto != "-" and to_num(monto) is None:
        etiquetas.insert(0, monto)
        monto = "-"

    vistas: list[str] = []
    for e in etiquetas:
        if e not in vistas:
            vistas.append(e)
    return (monto, " | ".join(vistas))


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB no encontrada en {DB_PATH.resolve()}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    try:
        for tabla in TABLAS:
            cols = {c[1] for c in con.execute(f"PRAGMA table_info({tabla})").fetchall()}
            if "descuento_tipo" not in cols:
                print(
                    f"ERROR: falta {tabla}.descuento_tipo — ejecuta el scraper una "
                    "vez para que init_db aplique la migración de schema.",
                    file=sys.stderr,
                )
                sys.exit(1)

        plan: dict[str, list[tuple[str, str, int]]] = {}
        for tabla in TABLAS:
            filas = con.execute(
                f"SELECT id, descuento FROM {tabla} "
                "WHERE descuento IS NOT NULL AND descuento != '' "
                "AND (descuento_tipo IS NULL OR descuento_tipo = '')"
            ).fetchall()
            cambios = []
            for fid, valor in filas:
                monto, tipos = separar(valor)
                if tipos or monto != valor:
                    cambios.append((monto, tipos, fid))
            plan[tabla] = cambios
            print(f"{tabla}: {len(cambios):,} filas a actualizar (de {len(filas):,} revisadas)")

        total = sum(len(v) for v in plan.values())
        if total == 0:
            print("\nNada que hacer — la migración ya se aplicó.")
            sys.exit(0)

        confirmacion = (
            input(f"\n¿Confirmas actualizar {total:,} filas? (escribe 's' para confirmar): ")
            .strip()
            .lower()
        )
        if confirmacion != "s":
            print("Cancelado sin cambios.")
            sys.exit(0)

        for tabla, cambios in plan.items():
            con.executemany(
                f"UPDATE {tabla} SET descuento = ?, descuento_tipo = ? WHERE id = ?",
                cambios,
            )
        con.commit()

        print(f"\nHecho. {total:,} filas actualizadas.")
        for tabla in TABLAS:
            n = con.execute(
                f"SELECT COUNT(*) FROM {tabla} "
                "WHERE descuento_tipo IS NOT NULL AND descuento_tipo != ''"
            ).fetchone()[0]
            print(f"  {tabla}: {n:,} filas con descuento_tipo poblado")
        sys.exit(0)

    except Exception as exc:
        con.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
