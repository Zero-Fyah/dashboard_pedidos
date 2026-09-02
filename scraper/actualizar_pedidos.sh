#!/bin/bash
# Equivalente Linux de scraper/actualizar_pedidos.bat (Windows Task
# Scheduler). Migración de entorno Windows → Linux Mint: este script lo
# invoca dashboard_pedidos_ciclo.service, disparado cada hora por
# dashboard_pedidos_ciclo.timer. El propio timer, al no arrancar una segunda
# instancia de una unidad que ya está activa, replica el "No iniciar una
# nueva instancia" que tenía la tarea de Windows.
set -uo pipefail

# Moverse a la raíz del proyecto (un nivel arriba de scraper/)
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# DEC-015: producción clavada al python del venv (3.12).
PYEXE=".venv/bin/python"

# DEC-028: rotación diaria de scraper_scheduler.log — el archivo único sin
# límite llegó a 54 GB entre 2026-05-24 y 2026-07-18. Un archivo por día
# acota el daño de una corrida ruidosa.
#
# Retención de logs (2026-07-19, a pedido expreso del Arquitecto): política
# ampliada a TODO archivo *.log en logs/, sin excepción — 30 días desde su
# última modificación, incluido el respaldo histórico combinado de DEC-028
# (que hasta BUG-020 estaba protegido a propósito). Se decidió así para
# conservar evidencia mientras se termina de validar que el pipeline
# funciona de forma correcta; pasado ese punto, limpieza total y automática.
# scraper.log queda fuera de este purgado — tiene su propia rotación con
# retención de 30 días en scraper/config.py (TimedRotatingFileHandler), sus
# archivos rotados no terminan en ".log" así que este patrón no los alcanza
# (sin lógica duplicada).
LOGDATE="$(date +%Y-%m-%d)"
LOGFILE="logs/scraper_scheduler_${LOGDATE}.log"
find logs -maxdepth 1 -name "*.log" -mtime +30 -delete 2>/dev/null || true

# Notificación de fallo (deuda de mediano plazo, CLAUDE.md) — cada paso
# abajo corre aunque el anterior falle (aislamiento a propósito, ver
# comentarios de cada bloque); FALLO solo acumula si hubo AL MENOS uno para
# avisar una vez al final, no interrumpir la corrida.
FALLO=0

# Ejecutar el scraper en modo incremental desde la raíz
"$PYEXE" scraper/scraper_principal.py --modo incremental >> "$LOGFILE" 2>&1 || FALLO=1

# DEC-039: descarga del inventario de los dos sistemas fuente, pegada al
# final del scraper de pedidos — minimiza la ventana de desfase entre el
# estado de subpedidos recién actualizado y la foto de inventario (ver
# docs/decisions.md). Cada línea corre aunque la anterior falle: una
# descarga de inventario caída no debe bloquear ni el scraping de pedidos
# ni el ETL.
"$PYEXE" -m scraper.inventario >> "$LOGFILE" 2>&1 || FALLO=1
"$PYEXE" -m scraper.bochica >> "$LOGFILE" 2>&1 || FALLO=1

# TASK-001: captura diaria de "Cambios de inventario". Sin condición de
# fecha/hora acá a propósito — el propio módulo decide si ya capturó el día
# anterior (ya_capturado()) y termina de inmediato si sí, así que esta línea
# puede correr en cualquiera de los ciclos horarios sin duplicar trabajo ni
# necesitar que el scheduler acierte una hora exacta.
"$PYEXE" -m scraper.cambios_inventario >> "$LOGFILE" 2>&1 || FALLO=1

# Captura diaria de movimientos de BOCHICA (Montacargas > Movimientos):
# mismo patrón que TASK-001 arriba — ya_cargado() decide si "ayer" ya está
# cargado y termina de inmediato si sí. Además archiva el snapshot de
# inventario de Bochica de ese día como cierre de jornada (retención 30
# días). Ver DEC-123.
"$PYEXE" -m scraper.movimientos_bochica >> "$LOGFILE" 2>&1 || FALLO=1

# DEC-043: cruce de inventario y persistencia en pedidos.db. Va después de
# las dos descargas (necesita ambos Excel frescos) y con el mismo criterio
# de aislamiento: si falla, no bloquea el ETL. El dashboard lee el
# resultado por VIEW en vez de recalcularlo (14,19 s -> 44 ms). Registra la
# antigüedad de cada fuente: si una descarga de arriba falló y dejó el
# Excel viejo, la corrida se marca datos_desactualizados=1 y el dashboard
# lo advierte en vez de mostrar un número que parece fresco.
"$PYEXE" -m inventario.persistencia >> "$LOGFILE" 2>&1 || FALLO=1

# DEC-092: pasada mensual de mantenimiento — el día 1, una sola vez.
#
# Va DENTRO de este script y no como unidad aparte por una razón medida: el
# ciclo horario ocupa 44-47 min de cada 60, así que no queda ventana libre y
# dos procesos escribiendo pedidos.db a la vez se pelean el lock (una
# corrida de prueba cayó al 71% de éxito por eso). Acá corre en secuencia,
# después del scraper y ANTES del ETL, para que el mismo ciclo normalice a
# _num lo que la pasada acaba de capturar.
#
# La ventana de fechas la calcula el propio modo: mes calendario recién
# cerrado más 4 meses de retroceso (el 20-32% de los pedidos se entrega en
# un mes posterior al de su fecha).
#
# El timer systemd no arranca una segunda instancia mientras la anterior
# sigue activa (equivalente a "No iniciar una nueva instancia" de Windows).
# El día 1 este ciclo dura ~60 min en vez de ~45 y se solaparía con el
# siguiente si no fuera por eso.
DIA_MES="$(date +%-d)"
HORA_DIA="$(date +%-H)"
if [ "$DIA_MES" = "1" ] && [ "$HORA_DIA" = "5" ]; then
    echo "[mantenimiento] pasada mensual — dia 1" >> "$LOGFILE"
    "$PYEXE" scraper/scraper_principal.py --modo mantenimiento >> "$LOGFILE" 2>&1 || FALLO=1
fi

# Ejecutar el ETL después del scraper — como módulo (E-7, DEC-018: el
# paquete editable resuelve los imports, sin hack de sys.path)
"$PYEXE" -m etl.etl_principal >> "$LOGFILE" 2>&1 || FALLO=1

# Aviso best-effort si hubo al menos un fallo (deuda de mediano plazo,
# CLAUDE.md). Reemplaza al PowerShell/NotifyIcon de Windows por notify-send
# — mismo riesgo conocido sin verificar en vivo, ver
# scripts/notificar_fallo_scheduler.sh.
if [ "$FALLO" = "1" ]; then
    "$(dirname "${BASH_SOURCE[0]}")/../scripts/notificar_fallo_scheduler.sh" "$(pwd)/${LOGFILE}" >> "$LOGFILE" 2>&1
fi
