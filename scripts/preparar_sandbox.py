"""Sandbox de desarrollo: prepara un worktree con los datos de producción en
solo lectura (DEC-166).

Los cambios se hacen y se prueban en un worktree de git, nunca en el árbol que
usa el ciclo horario. DEC-163 dejó las rutas de datos y el `.env` anclados a la
raíz del código, así que un worktree con su propio `data/` no lee ni escribe la
de producción. Este script le da a ese `data/` algo con qué trabajar:

- copia **de solo lectura** (API de respaldo de SQLite, con `quick_check`) la
  `pedidos.db` y la `tareas.db` de cada bodega que tenga base;
- copia los archivos de referencia de inventario (layout, nombres canónicos,
  variantes de Arena) y la sesión de BOCHICA, que es una sola para las dos;
- deriva el `.env` de cada bodega **sin email, sin hojas de Google y sin
  respaldo remoto**: esas variables se quitan, así que esos pasos fallan en vez
  de tocar algo real, y además quedan apagados en el ciclo.

Antes de copiar espera a que el ciclo de producción no esté corriendo.

**Lo que no aísla:** el scraper y las descargas hablan con el origen real
(solo lectura). Probarlos fuera de la ventana :00-:25 del ciclo, y levantar el
dashboard del sandbox en el puerto 8599, nunca en el 8501 de producción.

**Resguardos** contra escribir sobre producción: se niega a correr en el árbol
principal; fija su raíz por la ubicación de este archivo e importa el `comun`
de esa raíz (la instalación editable apunta al árbol principal); nunca copia
un archivo sobre sí mismo.

Uso, desde la raíz del worktree:

    python scripts/preparar_sandbox.py              # prepara o refresca
    python scripts/preparar_sandbox.py --sin-esperar
    python scripts/preparar_sandbox.py --borrar     # quita los datos del sandbox

Paso a producción: `docs/sandbox.md`.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# La raíz es la del worktree donde vive este archivo. Va antes de importar
# `comun`: sin esto, la instalación editable entrega el del árbol principal y
# todas las rutas de destino serían las de producción.
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import comun  # noqa: E402
from comun import BODEGA_DEFECTO, BODEGAS, ruta_bodega  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("preparar_sandbox")

PUERTO_DASHBOARD = 8599
UNIDAD_CICLO = "dashboard_pedidos_ciclo.service"
# Variables que nunca llegan al sandbox: con ellas, una prueba mandaría un email,
# escribiría en las hojas de Google o copiaría respaldos fuera de la máquina.
_PREFIJOS_QUITADOS = ("SMTP_", "SHEETS_ID_", "GOOGLE_SHEETS_CREDENTIALS", "RESPALDO_REMOTO_")
_PASOS_APAGADOS = ("sheets", "respaldo_remoto")
_REFERENCIAS_INVENTARIO = (
    "layout_bodega.xlsx",
    "arena_nombres_canonicos.xlsx",
    "arena_variantes_historicas.xlsx",
)
_SESION_BOCHICA = "bochica_storage_state.json"
# Lo que el sandbox pone o acumula en `data/`; `--borrar` quita solo esto.
_DATOS_DEL_SANDBOX = (
    "pedidos.db",
    "tareas.db",
    "inventario",
    "errors",
    "debug",
    "sesiones",
    "conteos",
    "backups",
)


class SandboxInseguroError(RuntimeError):
    """El sandbox podría tocar producción: el script se detiene."""


def _rev_parse(raiz: Path, opcion: str) -> Path:
    salida = subprocess.run(
        ["git", "-C", str(raiz), "rev-parse", "--path-format=absolute", opcion],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(salida).resolve()


def arbol_principal(raiz: Path) -> Path:
    """La raíz del checkout principal (producción) del repositorio de `raiz`.

    Raises:
        SandboxInseguroError: si `raiz` es el propio árbol principal.
    """
    git_dir = _rev_parse(raiz, "--git-dir")
    comun_dir = _rev_parse(raiz, "--git-common-dir")
    if git_dir == comun_dir:
        raise SandboxInseguroError(
            f"{raiz} es el árbol principal, el que usa el ciclo de producción. El sandbox "
            "se prepara en un worktree: git worktree add .claude/worktrees/<nombre>."
        )
    return comun_dir.parent


def verificar_comun(raiz: Path) -> None:
    """Detiene si el `comun` importado no es el de `raiz` (instalación editable)."""
    importado = Path(comun.__file__).resolve().parents[1]
    if importado != raiz.resolve() or Path(comun._RAIZ_PROYECTO).resolve() != raiz.resolve():
        raise SandboxInseguroError(
            f"Se importó el comun de {importado}, no el de {raiz}: las rutas de destino "
            "serían las de otro árbol. Correr con PYTHONPATH=<raíz del worktree>."
        )


def env_seguro(texto: str) -> str:
    """El `.env` de una bodega, sin lo que haría efectos reales fuera del sandbox.

    Quita email, hojas de Google y respaldo remoto, y suma esos pasos a los que
    el ciclo salta (`CICLO_PASOS_APAGADOS`), conservando los que ya estuvieran.
    """
    lineas = [
        "# SANDBOX (DEC-166) — generado por scripts/preparar_sandbox.py. No usar en producción."
    ]
    apagados: list[str] = []
    for linea in texto.splitlines():
        clave = linea.split("=", 1)[0].strip()
        if "=" in linea and not linea.lstrip().startswith("#"):
            if clave.startswith(_PREFIJOS_QUITADOS):
                continue
            if clave == "CICLO_PASOS_APAGADOS":
                valor = linea.split("=", 1)[1]
                apagados += [p for p in valor.replace(",", " ").split() if p]
                continue
        lineas.append(linea)
    apagados += [p for p in _PASOS_APAGADOS if p not in apagados]
    lineas.append(f"CICLO_PASOS_APAGADOS={','.join(dict.fromkeys(apagados))}")
    return "\n".join(lineas) + "\n"


def _no_es_el_mismo(origen: Path, destino: Path) -> None:
    if origen.resolve() == destino.resolve():
        raise SandboxInseguroError(f"Origen y destino son el mismo archivo: {origen}")


def copiar_base(origen: Path, destino: Path) -> None:
    """Copia una base SQLite con la API de respaldo, abriendo el origen en solo lectura.

    La copia queda en un temporal hasta pasar `quick_check`; recién ahí
    reemplaza al destino. Conserva la cabecera, y con ella la marca de bodega.
    """
    _no_es_el_mismo(origen, destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(destino.name + ".copiando")
    temporal.unlink(missing_ok=True)
    fuente = sqlite3.connect(f"{origen.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    copia = sqlite3.connect(temporal)
    try:
        fuente.backup(copia, pages=4096)
        estado = copia.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        copia.close()
        fuente.close()
    if estado != "ok":
        temporal.unlink(missing_ok=True)
        raise RuntimeError(f"La copia de {origen.name} no pasó quick_check: {estado}")
    for sufijo in ("-wal", "-shm"):
        Path(f"{destino}{sufijo}").unlink(missing_ok=True)
    temporal.replace(destino)


def _copiar_archivo(origen: Path, destino: Path, *, privado: bool = False) -> None:
    _no_es_el_mismo(origen, destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origen, destino)
    if privado:
        destino.chmod(0o600)


def esperar_fuera_del_ciclo(espera_s: int = 20) -> None:
    """Espera a que el ciclo de producción no esté corriendo (copiar durante el
    ciclo compite por disco y memoria con él)."""
    while True:
        try:
            estado = subprocess.run(
                ["systemctl", "is-active", UNIDAD_CICLO], capture_output=True, text=True
            ).stdout.strip()
        except FileNotFoundError:
            logger.warning("Sin systemctl: no se puede saber si el ciclo corre; se sigue.")
            return
        if estado not in ("active", "activating"):
            return
        logger.info("El ciclo de producción está corriendo; se espera %d s.", espera_s)
        time.sleep(espera_s)


def _en_principal(principal: Path, ruta: Path) -> Path:
    """La misma ruta de la bodega, pero en el árbol principal."""
    return principal / ruta.relative_to(Path(comun._RAIZ_PROYECTO))


def preparar(raiz: Path, principal: Path, *, esperar: bool = True) -> list[str]:
    """Copia datos y configuración de producción al sandbox. Devuelve lo hecho."""
    if esperar:
        esperar_fuera_del_ciclo()
    hecho: list[str] = []
    # El hook pre-commit corre `.venv/bin/python` relativo a la raíz: sin este
    # enlace, un commit desde el worktree se detiene. Ignorado por `.gitignore`.
    venv = raiz / ".venv"
    if not venv.exists() and not venv.is_symlink() and (principal / ".venv").is_dir():
        venv.symlink_to(principal / ".venv", target_is_directory=True)
        hecho.append(".venv enlazado al del árbol principal (lo necesita el hook pre-commit)")
    for bodega in BODEGAS:
        base = ruta_bodega("pedidos.db", bodega)
        if not _en_principal(principal, base).exists():
            hecho.append(f"{bodega}: sin base en producción, se omite")
            continue
        for recurso in ("pedidos.db", "tareas.db"):
            destino = ruta_bodega(recurso, bodega)
            origen = _en_principal(principal, destino)
            if origen.exists():
                copiar_base(origen, destino)
                hecho.append(f"{bodega}: {recurso} copiada")
        env = ruta_bodega("env", bodega)
        env_prod = _en_principal(principal, env)
        if env_prod.exists():
            env.write_text(env_seguro(env_prod.read_text(encoding="utf-8")), encoding="utf-8")
            env.chmod(0o600)
            hecho.append(f"{bodega}: {env.name} derivado (sin email, Sheets ni respaldo remoto)")
    inventario = ruta_bodega("inventario", BODEGA_DEFECTO)
    for nombre in _REFERENCIAS_INVENTARIO:
        origen = _en_principal(principal, inventario / nombre)
        if origen.exists():
            _copiar_archivo(origen, inventario / nombre)
            hecho.append(f"referencia: {nombre}")
    sesion = ruta_bodega("sesiones", BODEGA_DEFECTO) / _SESION_BOCHICA
    if _en_principal(principal, sesion).exists():
        _copiar_archivo(_en_principal(principal, sesion), sesion, privado=True)
        hecho.append("sesión de BOCHICA copiada")
    return hecho


def borrar(raiz: Path) -> list[str]:
    """Quita lo que el sandbox puso o acumuló en `data/` y sus `.env`. El código no se toca."""
    hecho: list[str] = []
    candidatos = {ruta_bodega("env", b) for b in BODEGAS}
    for bodega in BODEGAS:
        candidatos |= {ruta_bodega(r, bodega) for r in _DATOS_DEL_SANDBOX}
        base = ruta_bodega("pedidos.db", bodega)
        candidatos |= {Path(f"{base}-wal"), Path(f"{base}-shm")}
    for ruta in sorted(candidatos):
        if not Path(ruta).resolve().is_relative_to(raiz.resolve()):
            raise SandboxInseguroError(f"{ruta} está fuera del sandbox {raiz}")
        if ruta.is_dir():
            shutil.rmtree(ruta)
        elif ruta.exists():
            ruta.unlink()
        else:
            continue
        hecho.append(f"borrado: {ruta.relative_to(raiz)}")
    return hecho


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--borrar", action="store_true", help="quita los datos del sandbox")
    parser.add_argument("--sin-esperar", action="store_true", help="no espera al ciclo")
    args = parser.parse_args(argv)
    try:
        principal = arbol_principal(RAIZ)
        verificar_comun(RAIZ)
        hecho = (
            borrar(RAIZ) if args.borrar else preparar(RAIZ, principal, esperar=not args.sin_esperar)
        )
    except SandboxInseguroError as e:
        logger.error("Sandbox detenido: %s", e)
        return 2
    for linea in hecho:
        logger.info(linea)
    if not args.borrar:
        logger.info(
            "Listo. Dashboard del sandbox: PYTHONPATH=%s %s -m streamlit run "
            "dashboard/app.py --server.port %d (nunca el 8501). Scraper y descargas, fuera "
            "de la ventana :00-:25 del ciclo.",
            RAIZ,
            sys.executable,
            PUERTO_DASHBOARD,
        )
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # las copias no quedan legibles para otros usuarios
    sys.exit(main())
