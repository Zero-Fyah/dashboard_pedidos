#!/bin/bash
# notificar_fallo_scheduler.sh — aviso best-effort de un ciclo del scheduler
# con al menos un paso fallido (deuda "notificación de fallo" de CLAUDE.md).
#
# Equivalente Linux de scripts/notificar_fallo_scheduler.ps1 (que usaba
# System.Windows.Forms.NotifyIcon, Windows-only). Acá el mecanismo nativo es
# notify-send (libnotify) contra el bus de sesión D-Bus del usuario.
#
# Mismo riesgo conocido que en Windows, sin verificar en vivo (ver
# docs/decisions.md DEC-120): si el servicio systemd que llama a este script
# corre sin una sesión gráfica de usuario activa (D-Bus de sesión), la
# notificación puede no mostrarse nunca, en silencio — es el mismo punto
# ciego que tenían con LogonType=Password en Windows, no uno nuevo.
# Confirmar visualmente tras el primer fallo real; si no aparece, la
# alternativa ya prevista es email por SMTP.
set -uo pipefail

LOGFILE="${1:?uso: notificar_fallo_scheduler.sh <ruta-al-log>}"

if command -v notify-send >/dev/null 2>&1; then
    notify-send \
        --urgency=critical \
        --icon=dialog-warning \
        "dashboard_pedidos - fallo en el ciclo del scheduler" \
        "Al menos un paso del ciclo falló. Revisar: ${LOGFILE}" \
        2>>"${LOGFILE}" || echo "[notificacion] notify-send falló (¿sin sesión gráfica/D-Bus?)" >> "${LOGFILE}"
else
    echo "[notificacion] notify-send no está instalado — aviso no mostrado (instalar libnotify-bin)" >> "${LOGFILE}"
fi
