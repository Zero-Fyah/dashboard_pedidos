"""Aviso por email de un ciclo del scheduler con al menos un paso fallido
(DEC-161, 2026-09-28) — la vía de repliegue prevista desde DEC-120, ahora
que `notify-send` está confirmado roto sin sesión gráfica
(`scripts/notificar_fallo_scheduler.sh`).

Best-effort a propósito, igual que el aviso de notify-send: si el SMTP no
está configurado o falla (credenciales, red, servidor caído), este script
lo deja escrito en el log y termina con éxito — un aviso que falla nunca
debe tumbar el ciclo ni sumar un paso a "pasos con fallo".

**Requiere credenciales que solo tiene el Arquitecto** — sin ellas, el
script se queda en no-op explicado, no es un email que "no llega en
silencio": deja dicho en el log exactamente qué variable falta.

Configuración en `.env` (ver `.env.example`):
    SMTP_HOST, SMTP_PORT (587 por defecto), SMTP_USUARIO, SMTP_PASSWORD,
    SMTP_DESTINATARIO (a quién avisar — puede ser igual a SMTP_USUARIO),
    SMTP_REMITENTE (opcional, por defecto SMTP_USUARIO).

Uso (mismo contrato que notificar_fallo_scheduler.sh):

    python scripts/notificar_fallo_email.py <ruta-al-log>
"""

import logging
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("notificar_fallo_email")

_TIMEOUT_S = 15
_VARS_REQUERIDAS = ("SMTP_HOST", "SMTP_USUARIO", "SMTP_PASSWORD", "SMTP_DESTINATARIO")


def _resumen_del_log(ruta: Path, maximo_lineas: int = 15) -> str:
    """Las líneas `[paso] ... fin rc=N` y `[ciclo] fin` del log de hoy — lo
    justo para que el aviso sea accionable sin tener que abrir el archivo."""
    try:
        lineas = [
            linea
            for linea in ruta.read_text(errors="replace").splitlines()
            if linea.startswith(("[paso]", "[ciclo]", "[mantenimiento]"))
        ]
    except OSError as exc:
        return f"(no se pudo leer el log: {exc})"
    if not lineas:
        return "(el log no tiene líneas de [paso]/[ciclo] — revisar el archivo completo)"
    return "\n".join(lineas[-maximo_lineas:])


def enviar(ruta_log: Path) -> bool:
    """Intenta el envío. Devuelve True si salió, False si no (nunca lanza)."""
    faltantes = [v for v in _VARS_REQUERIDAS if not os.environ.get(v)]
    if faltantes:
        logger.info(
            "SMTP no configurado — faltan %s en .env; sin aviso por email (best-effort).",
            ", ".join(faltantes),
        )
        return False

    host = os.environ["SMTP_HOST"]
    puerto = int(os.environ.get("SMTP_PORT", "587"))
    usuario = os.environ["SMTP_USUARIO"]
    password = os.environ["SMTP_PASSWORD"]
    destinatario = os.environ["SMTP_DESTINATARIO"]
    remitente = os.environ.get("SMTP_REMITENTE", usuario)

    msg = EmailMessage()
    msg["Subject"] = f"dashboard_pedidos — ciclo con fallo ({ruta_log.name})"
    msg["From"] = remitente
    msg["To"] = destinatario
    msg.set_content(
        "Al menos un paso del ciclo del scheduler falló.\n\n"
        f"Log completo: {ruta_log}\n\n"
        "Últimas líneas relevantes:\n"
        f"{_resumen_del_log(ruta_log)}\n"
    )

    try:
        with smtplib.SMTP(host, puerto, timeout=_TIMEOUT_S) as smtp:
            smtp.starttls()
            smtp.login(usuario, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        # Best-effort: un SMTP caído no debe sumar un fallo al ciclo.
        logger.warning("Envío por SMTP falló (%s): %s", type(exc).__name__, exc)
        return False

    logger.info("Aviso por email enviado a %s", destinatario)
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print("uso: notificar_fallo_email.py <ruta-al-log>", file=sys.stderr)
        return 2
    enviar(Path(sys.argv[1]))
    return 0  # best-effort: nunca falla el paso que lo invoca


if __name__ == "__main__":
    sys.exit(main())
