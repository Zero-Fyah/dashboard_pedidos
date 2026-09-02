#!/bin/bash
# ============================================================================
# Arranque del dashboard como servicio de Linux (DEC-105).
#
# Equivalente Linux de scripts/iniciar_dashboard.bat (Windows Task Scheduler).
# Migración de entorno Windows → Linux Mint: este script lo invoca la unidad
# systemd dashboard_pedidos.service (Restart=always), que reemplaza a la
# tarea "Al iniciar el equipo, ejecutar tanto si el usuario inició sesión
# como si no" de Windows. No requiere privilegios: escucha en 8501, que no
# es un puerto privilegiado.
# ============================================================================
set -uo pipefail

# Raíz del proyecto (un nivel arriba de scripts/)
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# DEC-015: clavado al python del venv (3.12), por la misma razón que en
# Windows — no depender de un launcher global que pueda apuntar a otra
# versión o tener otro streamlit instalado.
PYEXE=".venv/bin/python"

# Rotación diaria, mismo criterio que DEC-028: un archivo único sin límite
# llegó a 54 GB una vez. Streamlit escribe poco, pero un error en bucle
# llena disco igual.
LOGDATE="$(date +%Y-%m-%d)"
LOGFILE="logs/dashboard_${LOGDATE}.log"

mkdir -p logs

{
    echo ""
    echo "===================================================="
    date "+%Y-%m-%d %H:%M:%S"
    echo "[dashboard] arrancando"
} >> "$LOGFILE"

# La configuración de red vive en .streamlit/config.toml (address, puerto,
# headless, telemetría). Acá no se repite: una sola fuente de verdad.
"$PYEXE" -m streamlit run dashboard/app.py >> "$LOGFILE" 2>&1

# Si el proceso termina, algo lo mató: queda registrado con la hora.
# (systemd con Restart=always lo vuelve a levantar solo)
{
    echo "[dashboard] el proceso TERMINO — revisar el log de arriba"
    date "+%Y-%m-%d %H:%M:%S"
} >> "$LOGFILE"
