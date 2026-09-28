"""Respaldo diario verificado de las bases SQLite (auditoría 2026-09-25, DEC-146).

Hasta esta fecha no había ningún respaldo automático: la única copia de
`pedidos.db` era una manual del 2026-09-01, y la base ya sufrió una
corrupción real (DEC-126). Parte de su contenido el origen no lo vuelve a
entregar (series de `inventario_corridas`, capturas diarias de movimientos,
campos que el origen deja de mostrar — DEC-091/127), y `tareas.db` guarda
trabajo manual.

Corre al final de cada ciclo horario, pero copia **una vez por día**: si ya
existe el respaldo de hoy, termina sin hacer nada (mismo patrón que
`scraper.cambios_inventario`). Usa la API de respaldo en línea de SQLite,
que produce una copia consistente aunque haya lectores o un escritor en WAL.
Cada copia se verifica con `PRAGMA quick_check` antes de contarla como
válida, y se conservan las últimas `RESPALDO_DIAS` (7 por defecto).

Límite conocido: el destino está en el mismo disco (la máquina tiene uno
solo). Protege de corrupción, de una migración mala o de un borrado
accidental; no de la pérdida del disco. La copia fuera de la máquina queda
como decisión pendiente del Arquitecto.

Uso (desde la raíz del proyecto):

    python scripts/respaldar_db.py
"""

import logging
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

logger = logging.getLogger("respaldo")

RAIZ = Path(__file__).resolve().parent.parent
BASES: tuple[str, ...] = ("pedidos", "tareas")
# Solo los archivos que genera este script: `<base>_AAAA-MM-DD.db`. Los
# respaldos manuales (`pedidos_pre-reindex_…`, `pedidos_pre_DEC140_…`) no
# calzan con el patrón y la retención nunca los toca.
_PATRON = r"^{base}_(\d{{4}}-\d{{2}}-\d{{2}})\.db$"


def _dias_retencion() -> int:
    valor = os.environ.get("RESPALDO_DIAS", "7")
    try:
        dias = int(valor)
    except ValueError:
        logger.warning("RESPALDO_DIAS=%r no es un entero — se usan 7", valor)
        return 7
    return max(dias, 1)


def _verificar(ruta: Path) -> str:
    """`PRAGMA quick_check` sobre una conexión nueva a la copia ya cerrada."""
    con = sqlite3.connect(ruta)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


def respaldar(origen: Path, destino_dir: Path, hoy: date) -> Path | None:
    """Copia `origen` a `destino_dir/<base>_<hoy>.db` si todavía no existe.

    La copia se escribe primero en un temporal y solo se renombra tras pasar
    `quick_check`: un respaldo a medias o dañado nunca queda con el nombre
    de uno válido.

    Returns:
        La ruta del respaldo nuevo, o None si el de hoy ya existía.

    Raises:
        RuntimeError: si la copia no pasa `quick_check` (se borra).
    """
    destino_dir.mkdir(parents=True, exist_ok=True)
    final = destino_dir / f"{origen.stem}_{hoy.isoformat()}.db"
    if final.exists():
        return None
    temporal = final.with_suffix(".db.tmp")
    temporal.unlink(missing_ok=True)
    fuente = sqlite3.connect(f"{origen.resolve().as_uri()}?mode=ro", uri=True)
    try:
        copia = sqlite3.connect(temporal)
        try:
            fuente.backup(copia)
            # La copia hereda el modo WAL del original: abrirla crearía sus
            # propios -wal/-shm. En DELETE queda como un único archivo
            # autocontenido, seguro de mover o copiar a otro disco.
            copia.execute("PRAGMA journal_mode=DELETE")
        finally:
            copia.close()
    finally:
        fuente.close()
    resultado = _verificar(temporal)
    if resultado != "ok":
        temporal.unlink(missing_ok=True)
        raise RuntimeError(f"Respaldo de {origen.name} no pasó quick_check: {resultado}")
    temporal.rename(final)
    return final


def purgar(destino_dir: Path, base: str, conservar: int) -> list[Path]:
    """Borra los respaldos automáticos de `base` más viejos, dejando `conservar`.

    Returns:
        Las rutas borradas.
    """
    patron = re.compile(_PATRON.format(base=re.escape(base)))
    propios = sorted(
        (p for p in destino_dir.glob(f"{base}_*.db") if patron.match(p.name)),
        key=lambda p: p.name,
    )
    viejos = propios[:-conservar] if len(propios) > conservar else []
    for p in viejos:
        p.unlink()
    return viejos


def main() -> int:
    destino_dir = RAIZ / "data" / "backups"
    conservar = _dias_retencion()
    hoy = date.today()
    codigo = 0
    for base in BASES:
        origen = RAIZ / "data" / f"{base}.db"
        if not origen.exists():
            logger.warning("respaldo: %s no existe — se omite", origen.name)
            continue
        try:
            nuevo = respaldar(origen, destino_dir, hoy)
        except Exception as exc:  # noqa: BLE001 — se reporta y el ciclo sigue
            logger.error("respaldo: %s falló: %s", origen.name, exc)
            codigo = 1
            continue
        if nuevo is None:
            continue
        borrados = purgar(destino_dir, base, conservar)
        logger.info(
            "respaldo: %s → %s (%.0f MB, quick_check ok); purgados %d",
            origen.name,
            nuevo.name,
            nuevo.stat().st_size / 1e6,
            len(borrados),
        )
    return codigo


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
