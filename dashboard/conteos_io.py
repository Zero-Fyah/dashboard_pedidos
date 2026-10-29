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

from comun import BODEGA_DEFECTO, get_bodega, ruta_bodega

logger = logging.getLogger("conteos_io")

# DEC-165: las carpetas se resuelven por bodega en cada llamada (la del
# selector del dashboard); sin `bodega`, la del proceso (DEC-163). Las tres
# constantes CARPETA_* son solo sustitutos para pruebas; en producción, None.
CARPETA_CONTEOS: Path | None = None
CARPETA_ANULADOS: Path | None = None
CARPETA_RESPALDO: Path | None = None

# Respaldo a disco externo dedicado (EJPC-RESPALDO, montado por udisks2 en
# /media/<usuario>/EJPC-RESPALDO): antes de esto, data/conteos/ no tenía
# ninguna copia fuera de este equipo. Best-effort a propósito: si el disco
# no está disponible, no debe romper la subida, que ya dejó el archivo a
# salvo en CARPETA_CONTEOS (DEC-058). **El disco se dañó físicamente y no
# hay reemplazo todavía (2026-09-28)** — sin conteos reales aún, no protege
# nada en la práctica, pero cuando lo reemplacen esto vuelve a funcionar
# solo, sin tocar código.
# DEC-163: la bodega por defecto conserva su carpeta; otra bodega va en una
# subcarpeta con su nombre, así una copia nunca mezcla conteos de las dos.
_RAIZ_RESPALDO = Path("/media") / Path.home().name / "EJPC-RESPALDO" / "dashboard_pedidos_respaldo"


def carpeta_conteos(*, bodega: str | None = None) -> Path:
    """Donde quedan las hojas activas de la bodega."""
    return CARPETA_CONTEOS if CARPETA_CONTEOS is not None else ruta_bodega("conteos", bodega)


def _carpeta_anulados(bodega: str | None) -> Path:
    return (
        CARPETA_ANULADOS
        if CARPETA_ANULADOS is not None
        else carpeta_conteos(bodega=bodega) / "anulados"
    )


def _carpeta_respaldo(bodega: str | None) -> Path:
    if CARPETA_RESPALDO is not None:
        return CARPETA_RESPALDO
    bodega = bodega or get_bodega()
    raiz = _RAIZ_RESPALDO if bodega == BODEGA_DEFECTO else _RAIZ_RESPALDO / bodega
    return raiz / "conteos"


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
    # El nombre lo manda el navegador del cliente, que puede correr en
    # Windows aunque el servidor sea Linux — normalizar `\` a `/` antes de
    # pedir `.name` para que el recorte de ruta no dependa de en qué SO
    # corre este proceso (Path().name solo entiende `\` como separador en
    # Windows; en Linux lo trataría como parte del nombre).
    base = Path(str(nombre).replace("\\", "/")).name
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = _PERMITIDOS.sub("_", base).lstrip(".")
    if not base.lower().endswith(EXTENSION):
        raise ValueError(f"Solo se aceptan archivos {EXTENSION}")
    if len(base) <= len(EXTENSION):
        raise ValueError("El nombre del archivo quedó vacío")
    return base


def existe(nombre: str, *, bodega: str | None = None) -> bool:
    """True si ya hay una hoja activa con ese nombre."""
    return (carpeta_conteos(bodega=bodega) / nombre_seguro(nombre)).exists()


def _respaldar(bodega: str | None = None) -> None:
    """Copia `data/conteos/` completa a `CARPETA_RESPALDO`, best-effort.

    Nunca debe tumbar el flujo de subida/anulación: el archivo ya quedó a
    salvo en `CARPETA_CONTEOS`, que sigue siendo el origen de verdad
    (DEC-058). Si el disco de respaldo no está conectado, la copia falla
    con `OSError` y acá solo se registra — no se propaga.
    """
    respaldo = _carpeta_respaldo(bodega)
    try:
        shutil.copytree(carpeta_conteos(bodega=bodega), respaldo, dirs_exist_ok=True)
    except OSError as exc:
        logger.warning("Respaldo de conteos a %s falló (¿disco desconectado?): %s", respaldo, exc)


def guardar(nombre: str, contenido: bytes, *, bodega: str | None = None) -> Path:
    """Deja la hoja en `data/conteos/`, lista para la próxima corrida.

    Sobrescribe si ya existe: es el flujo de "corregir una hoja" de
    DEC-058, donde el valor nuevo reemplaza al anterior.
    """
    destino = carpeta_conteos(bodega=bodega) / nombre_seguro(nombre)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)
    _respaldar(bodega)
    return destino


def anular(nombre: str, *, bodega: str | None = None) -> Path:
    """Mueve una hoja a `anulados/`: sale del cálculo sin borrarse.

    Es la operación de deshacer de DEC-058, acá para no tener que ir al
    explorador de archivos.

    Raises:
        FileNotFoundError: si la hoja no está activa.
    """
    seguro = nombre_seguro(nombre)
    origen = carpeta_conteos(bodega=bodega) / seguro
    if not origen.exists():
        raise FileNotFoundError(seguro)
    anulados = _carpeta_anulados(bodega)
    anulados.mkdir(parents=True, exist_ok=True)
    destino = anulados / seguro
    # Si ya se anuló una hoja con ese nombre antes, no se pisa: se numera.
    contador = 1
    while destino.exists():
        destino = anulados / f"{Path(seguro).stem}_{contador}{EXTENSION}"
        contador += 1
    origen.replace(destino)
    _respaldar(bodega)
    return destino


def listar_activas(*, bodega: str | None = None) -> list[str]:
    """Hojas que hoy están alimentando el cálculo."""
    carpeta = carpeta_conteos(bodega=bodega)
    if not carpeta.exists():
        return []
    return sorted(p.name for p in carpeta.glob(f"*{EXTENSION}") if not p.name.startswith("~$"))
