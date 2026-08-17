"""
conteos_io.py — Recibe y archiva las hojas de conteo (DEC-060).

El dashboard **no escribe en `pedidos.db`**: eso sigue siendo del pipeline
(DEC-043). Lo que hace acá es dejar el archivo en `data/conteos/`, que es el
origen de verdad de los conteos (DEC-058); el scheduler lo ingiere en su
siguiente corrida.

Esa separación es la que permite subir desde el navegador sin romper el
contrato entre etapas: el dashboard escribe **un archivo**, no una tabla.
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from pathlib import Path

logger = logging.getLogger("conteos_io")

CARPETA_CONTEOS = Path(__file__).parent.parent / "data" / "conteos"
CARPETA_ANULADOS = CARPETA_CONTEOS / "anulados"

# Respaldo a disco externo (deuda cerrada, ver docs/decisions.md): antes de
# esto, data/conteos/ no tenía ninguna copia fuera de este equipo. E: es un
# disco fijo dedicado a respaldo en esta máquina, no un USB intermitente —
# aun así la copia es best-effort: si no está montado no debe romper la
# subida, que ya dejó el archivo a salvo en CARPETA_CONTEOS (DEC-058).
CARPETA_RESPALDO = Path("E:/dashboard_pedidos_respaldo/conteos")

EXTENSION = ".xlsx"

# El nombre viene del navegador, así que es entrada no confiable: sin
# sanear, un "../../algo.xlsx" escribiría fuera de la carpeta.
_PERMITIDOS = re.compile(r"[^A-Za-z0-9._-]")


def nombre_seguro(nombre: str) -> str:
    """Reduce un nombre de archivo subido a algo inocuo y predecible.

    Se queda solo con el nombre base —sin ninguna parte de ruta—, quita
    tildes y reemplaza todo lo que no sea alfanumérico, punto, guion o
    guion bajo.

    Raises:
        ValueError: si no queda nada utilizable o no es un `.xlsx`.
    """
    base = Path(str(nombre)).name
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = _PERMITIDOS.sub("_", base).lstrip(".")
    if not base.lower().endswith(EXTENSION):
        raise ValueError(f"Solo se aceptan archivos {EXTENSION}")
    if len(base) <= len(EXTENSION):
        raise ValueError("El nombre del archivo quedó vacío")
    return base


def existe(nombre: str) -> bool:
    """True si ya hay una hoja activa con ese nombre."""
    return (CARPETA_CONTEOS / nombre_seguro(nombre)).exists()


def _respaldar() -> None:
    """Copia `data/conteos/` completa a `CARPETA_RESPALDO`, best-effort.

    Nunca debe tumbar el flujo de subida/anulación: el archivo ya quedó a
    salvo en `CARPETA_CONTEOS`, que sigue siendo el origen de verdad
    (DEC-058). Si el disco de respaldo no está conectado, la copia falla
    con `OSError` y acá solo se registra — no se propaga.
    """
    try:
        shutil.copytree(CARPETA_CONTEOS, CARPETA_RESPALDO, dirs_exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Respaldo de conteos a %s falló (¿disco desconectado?): %s", CARPETA_RESPALDO, exc
        )


def guardar(nombre: str, contenido: bytes) -> Path:
    """Deja la hoja en `data/conteos/`, lista para la próxima corrida.

    Sobrescribe si ya existe: es el flujo de "corregir una hoja" de
    DEC-058, donde el valor nuevo reemplaza al anterior.
    """
    destino = CARPETA_CONTEOS / nombre_seguro(nombre)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)
    _respaldar()
    return destino


def anular(nombre: str) -> Path:
    """Mueve una hoja a `anulados/`: sale del cálculo sin borrarse.

    Es la operación de deshacer de DEC-058, acá para no tener que ir al
    explorador de archivos.

    Raises:
        FileNotFoundError: si la hoja no está activa.
    """
    seguro = nombre_seguro(nombre)
    origen = CARPETA_CONTEOS / seguro
    if not origen.exists():
        raise FileNotFoundError(seguro)
    CARPETA_ANULADOS.mkdir(parents=True, exist_ok=True)
    destino = CARPETA_ANULADOS / seguro
    # Si ya se anuló una hoja con ese nombre antes, no se pisa: se numera.
    contador = 1
    while destino.exists():
        destino = CARPETA_ANULADOS / f"{Path(seguro).stem}_{contador}{EXTENSION}"
        contador += 1
    origen.replace(destino)
    _respaldar()
    return destino


def listar_activas() -> list[str]:
    """Hojas que hoy están alimentando el cálculo."""
    if not CARPETA_CONTEOS.exists():
        return []
    return sorted(
        p.name for p in CARPETA_CONTEOS.glob(f"*{EXTENSION}") if not p.name.startswith("~$")
    )
