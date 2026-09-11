"""monitor_laptop_tailscale.py — registra cuándo la laptop del trabajo se
conecta/desconecta de Tailscale.

Contexto: el acceso end-to-end de la laptop del trabajo contra el
dashboard sigue sin poder probarse porque la laptop está offline
la mayor parte del tiempo (ver memoria de proyecto
`project_migracion_linux.md`) — no hay forma de saber, sin mirar en el
momento exacto, cuándo estuvo conectada. Este script no prueba el acceso
en sí (eso sigue siendo manual, cargar el dashboard desde la laptop); solo
deja un registro de cuándo hubo ventana para probarlo.

Se dispara por `dashboard_pedidos_laptop_tailscale.timer` (systemd, cada 5
minutos — ver scripts/systemd/) y solo escribe una línea cuando el estado
CAMBIA respecto a la corrida anterior (guardado en `data/`), no en cada
corrida — igual de silencioso que el resto del pipeline cuando no hay
nada nuevo que reportar.

Identifica a la laptop por su IP de Tailscale, no por hostname: es el
mismo identificador que ya usa la ACL del tailnet (`estado-actual.md` del
skill de Tailscale), y no cambia si el equipo se renombra.

La IP viene de `TAILSCALE_IP_LAPTOP_TRABAJO` en `.env` (no en el código):
es topología real de la red privada del Arquitecto, y el repo es público
de forma permanente (DEC-017) — mismo criterio que las URLs y credenciales
de `scraper/config.py`. Ver `.env.example` para la variable esperada.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).parent.parent
load_dotenv(RAIZ / ".env")

IP_LAPTOP_TRABAJO = os.environ.get("TAILSCALE_IP_LAPTOP_TRABAJO", "").strip()

RUTA_ESTADO = RAIZ / "data" / "estado_laptop_tailscale.txt"
RUTA_LOG = RAIZ / "logs" / "laptop_tailscale.log"


def _hora_local() -> str:
    # astimezone() sin argumento usa la zona del sistema (America/Bogota,
    # UTC-5 fijo, ya corregida en timedatectl tras el fix de RTC).
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _consultar_estado_actual() -> str | None:
    """ "online" / "offline" / None si tailscale no responde (best-effort)."""
    try:
        salida = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        _registrar(f"ERROR: no se pudo consultar tailscale status ({e})")
        return None

    try:
        datos = json.loads(salida.stdout)
    except json.JSONDecodeError as e:
        _registrar(f"ERROR: tailscale status --json devolvió algo no parseable ({e})")
        return None

    for peer in datos.get("Peer", {}).values():
        if peer.get("TailscaleIPs") and IP_LAPTOP_TRABAJO in peer["TailscaleIPs"]:
            return "online" if peer.get("Online") else "offline"

    _registrar(f"ERROR: {IP_LAPTOP_TRABAJO} no aparece entre los peers — ¿salió de la malla?")
    return None


def _registrar(mensaje: str) -> None:
    RUTA_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUTA_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{_hora_local()} {mensaje}\n")


def main() -> None:
    if not IP_LAPTOP_TRABAJO:
        _registrar("ERROR: falta TAILSCALE_IP_LAPTOP_TRABAJO en .env — ver .env.example")
        return

    actual = _consultar_estado_actual()
    if actual is None:
        return

    anterior = RUTA_ESTADO.read_text(encoding="utf-8").strip() if RUTA_ESTADO.exists() else None

    if actual != anterior:
        if actual == "online":
            _registrar(f"laptop del trabajo CONECTADA ({IP_LAPTOP_TRABAJO})")
        else:
            _registrar(f"laptop del trabajo desconectada ({IP_LAPTOP_TRABAJO})")
        RUTA_ESTADO.parent.mkdir(parents=True, exist_ok=True)
        RUTA_ESTADO.write_text(actual, encoding="utf-8")


if __name__ == "__main__":
    main()
