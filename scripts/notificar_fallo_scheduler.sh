#!/bin/bash
# notificar_fallo_scheduler.sh — aviso best-effort de un ciclo del scheduler
# con al menos un paso fallido (deuda "notificación de fallo" de CLAUDE.md).
#
# Mecanismo: notify-send (libnotify) contra el bus de sesión D-Bus del
# usuario.
#
# CONFIRMADO ROTO (DEC-120): el servicio systemd que llama a este script
# corre sin sesión gráfica de usuario activa (sin D-Bus de sesión), así
# que la notificación nunca se muestra — falló las 4 veces que se disparó
# en producción. La alternativa prevista es email por SMTP; pendiente de
# decisión del Arquitecto.
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
