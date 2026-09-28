"""Copia el respaldo diario ya verificado a un destino fuera de esta máquina
(DEC-161, 2026-09-28) — el límite que `scripts/respaldar_db.py` (DEC-146)
deja anotado desde el principio: protege de corrupción, de una migración
mala o de un borrado accidental, pero no de perder este disco.

Best-effort, mismo contrato que `notificar_fallo_email.py`: sin destino
configurado, o si el destino no responde, queda en no-op explicado en el
log y termina con éxito — nunca suma un fallo al ciclo. `rsync` es
incremental: en el ciclo que crea el respaldo del día transfiere el archivo
completo; en los demás ciclos de esa misma hora no hay nada nuevo que
copiar, así que el costo real es solo la verificación de checksum.

**Requiere que el Arquitecto decida el destino** — este agente no puede
elegirlo ni configurar el acceso SSH a una máquina ajena. Cualquier host
Linux/macOS alcanzable por SSH sirve: otro equipo, un servidor propio, un
VPS. La laptop del trabajo, en el mismo tailnet, es una opción pero con una
salvedad conocida (ver `project_migracion_linux.md`): no siempre está
encendida, así que no sustituye una copia con disponibilidad garantizada.

Configuración en `.env` (ver `.env.example`):
    RESPALDO_REMOTO_HOST, RESPALDO_REMOTO_RUTA (directorio destino, debe
    existir), RESPALDO_REMOTO_USUARIO (opcional, usa el de la config de
    SSH si no se da), RESPALDO_REMOTO_PUERTO (22 por defecto). El acceso
    debe estar resuelto de antemano con una clave SSH sin passphrase (o un
    agente ya cargado) — este script nunca pide una contraseña
    interactiva (`BatchMode=yes`): si hace falta una, falla rápido.

Uso (desde la raíz del proyecto, después de que respaldar_db.py corrió):

    python scripts/respaldar_remoto.py
"""

import datetime
import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("respaldar_remoto")

_TIMEOUT_S = 120
RAIZ = Path(__file__).resolve().parent.parent
CARPETA_BACKUPS = RAIZ / "data" / "backups"


def _archivos_de_hoy() -> list[Path]:
    """Los respaldos que `respaldar_db.py` ya verificó con quick_check hoy."""
    hoy = datetime.date.today().isoformat()
    return [
        p
        for p in (CARPETA_BACKUPS / f"pedidos_{hoy}.db", CARPETA_BACKUPS / f"tareas_{hoy}.db")
        if p.exists()
    ]


def copiar() -> bool:
    """Intenta la copia. Devuelve True si salió, False si no (nunca lanza)."""
    host = os.environ.get("RESPALDO_REMOTO_HOST")
    ruta_remota = os.environ.get("RESPALDO_REMOTO_RUTA")
    if not host or not ruta_remota:
        logger.info(
            "Respaldo remoto no configurado — faltan RESPALDO_REMOTO_HOST/"
            "RESPALDO_REMOTO_RUTA en .env; sin copia fuera de la máquina (best-effort)."
        )
        return False

    archivos = _archivos_de_hoy()
    if not archivos:
        logger.warning(
            "No hay respaldo de hoy en %s todavía — ¿corrió respaldar_db.py antes que esto?",
            CARPETA_BACKUPS,
        )
        return False

    usuario = os.environ.get("RESPALDO_REMOTO_USUARIO", "")
    puerto = os.environ.get("RESPALDO_REMOTO_PUERTO", "22")
    destino = f"{usuario}@{host}:{ruta_remota}" if usuario else f"{host}:{ruta_remota}"

    comando = [
        "rsync",
        "-az",
        "--timeout=30",
        "-e",
        f"ssh -p {puerto} -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        *[str(a) for a in archivos],
        destino,
    ]
    try:
        resultado = subprocess.run(
            comando, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Copia remota falló (%s): %s", type(exc).__name__, exc)
        return False

    if resultado.returncode != 0:
        logger.warning(
            "rsync terminó con código %d: %s", resultado.returncode, resultado.stderr.strip()
        )
        return False

    logger.info("Copiados %d archivo(s) a %s", len(archivos), destino)
    return True


def main() -> int:
    copiar()
    return 0  # best-effort: nunca falla el paso que lo invoca


if __name__ == "__main__":
    raise SystemExit(main())
