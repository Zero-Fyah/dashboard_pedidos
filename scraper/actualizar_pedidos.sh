#!/bin/bash
# Ciclo horario del scraper/ETL (DEC-125). Lo invoca
# dashboard_pedidos_ciclo.service, disparado cada hora por
# dashboard_pedidos_ciclo.timer, que no arranca una segunda instancia
# mientras la anterior sigue activa.
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

# Auditoría de seguridad 2026-09-04, hallazgo H-10: data/debug/ (HTML de
# debug) y data/errors/ (screenshots) pueden contener PII de pedidos y
# clientes (ver CLAUDE.md) y el scraper las repuebla en su operación
# normal — se habían purgado a mano una vez (05-08) pero sin retención
# automática volvían a acumularse sin límite. Mismo criterio de 30 días
# que logs/, mismo mecanismo (mtime, sin leer contenido — no viola la
# restricción de CLAUDE.md sobre no leer esa carpeta sin advertencia).
find data/debug data/errors -maxdepth 1 -type f -mtime +30 -delete 2>/dev/null || true

# Notificación de fallo (deuda de mediano plazo, CLAUDE.md) — cada paso
# abajo corre aunque el anterior falle (aislamiento a propósito, ver
# comentarios de cada bloque); FALLO solo acumula si hubo AL MENOS uno para
# avisar una vez al final, no interrumpir la corrida.
FALLO=0

# Auditoría 2026-09-25 (DEC-146): cada paso deja en el log su nombre, su
# código de salida y su duración. Antes solo quedaba registrado que "algún
# paso" había fallado — saber que fue Bochica (timeout de la descarga) exigía
# leer tracebacks a mano — y no había forma de medir cuánto pesa cada paso
# en el ciclo. `paso` nunca corta la cadena: conserva el aislamiento de arriba.
PASOS_FALLIDOS=""
paso() {
    local nombre="$1"
    shift
    local inicio=$SECONDS
    echo "[paso] ${nombre} — inicio $(date '+%F %T')" >> "$LOGFILE"
    "$@" >> "$LOGFILE" 2>&1
    local rc=$?
    echo "[paso] ${nombre} — fin rc=${rc} en $((SECONDS - inicio)) s" >> "$LOGFILE"
    if [ "$rc" -ne 0 ]; then
        FALLO=1
        PASOS_FALLIDOS="${PASOS_FALLIDOS} ${nombre}"
    fi
    return 0
}

# Ejecutar el scraper en modo incremental desde la raíz
paso scraper "$PYEXE" scraper/scraper_principal.py --modo incremental

# DEC-039: descarga del inventario de los dos sistemas fuente, pegada al
# final del scraper de pedidos — minimiza la ventana de desfase entre el
# estado de subpedidos recién actualizado y la foto de inventario (ver
# docs/decisions.md). Cada línea corre aunque la anterior falle: una
# descarga de inventario caída no debe bloquear ni el scraping de pedidos
# ni el ETL.
paso inventario_admin "$PYEXE" -m scraper.inventario
paso bochica "$PYEXE" -m scraper.bochica

# TASK-001: captura diaria de "Cambios de inventario". Sin condición de
# fecha/hora acá a propósito — el propio módulo decide si ya capturó el día
# anterior (ya_capturado()) y termina de inmediato si sí, así que esta línea
# puede correr en cualquiera de los ciclos horarios sin duplicar trabajo ni
# necesitar que el scheduler acierte una hora exacta.
paso cambios_inventario "$PYEXE" -m scraper.cambios_inventario

# Captura diaria de movimientos de BOCHICA (Montacargas > Movimientos):
# mismo patrón que TASK-001 arriba — ya_cargado() decide si "ayer" ya está
# cargado y termina de inmediato si sí. Además archiva el snapshot de
# inventario de Bochica de ese día como cierre de jornada (retención 30
# días). Ver DEC-123.
paso movimientos_bochica "$PYEXE" -m scraper.movimientos_bochica

# DEC-043: cruce de inventario y persistencia en pedidos.db. Va después de
# las dos descargas (necesita ambos Excel frescos) y con el mismo criterio
# de aislamiento: si falla, no bloquea el ETL. El dashboard lee el
# resultado por VIEW en vez de recalcularlo (14,19 s -> 44 ms). Registra la
# antigüedad de cada fuente: si una descarga de arriba falló y dejó el
# Excel viejo, la corrida se marca datos_desactualizados=1 y el dashboard
# lo advierte en vez de mostrar un número que parece fresco.
paso inventario_persistencia "$PYEXE" -m inventario.persistencia

# Exporta a Google Sheets el inventario Arena disponible y los pedidos
# previos a picking, por ciudad — las 11 que solo almacenan Arena (pedido
# del Arquitecto, 2026-09-14). Va después de inventario.persistencia
# (arena_inventario recién recalculada) y no depende del ETL (no usa
# columnas _num). Aislado igual que el resto: si Sheets no responde, no
# bloquea nada del ciclo.
paso sheets "$PYEXE" -m integraciones.sheets_cliente

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
# sigue activa. El día 1 este ciclo dura ~60 min en vez de ~45 y se
# solaparía con el siguiente si no fuera por eso.
#
# Actualización 2026-09-27 (DEC-150): las cifras de arriba son de 2026-08.
# Hoy el ciclo dura 24-27 min con tope pedido de 30, así que el ciclo del
# día 1 es la excepción mensual a ese tope — ACEPTADA por el Arquitecto el
# 2026-09-28 (DEC-155): una vez al mes, ~60 min; el disparo de las 06:00 de
# ese día corre apenas termina este ciclo.
DIA_MES="$(date +%-d)"
HORA_DIA="$(date +%-H)"
if [ "$DIA_MES" = "1" ] && [ "$HORA_DIA" = "5" ]; then
    echo "[mantenimiento] pasada mensual — dia 1" >> "$LOGFILE"
    paso mantenimiento "$PYEXE" scraper/scraper_principal.py --modo mantenimiento
fi

# Ejecutar el ETL después del scraper — como módulo (E-7, DEC-018: el
# paquete editable resuelve los imports, sin hack de sys.path)
paso etl "$PYEXE" -m etl.etl_principal

# DEC-146: respaldo diario verificado de pedidos.db y tareas.db. Va al final,
# con el ETL ya aplicado, y copia una sola vez por día (el script termina
# de inmediato si el respaldo de hoy ya existe). ~10 s el primer ciclo del día.
paso respaldo "$PYEXE" scripts/respaldar_db.py

# DEC-161: copia ese mismo respaldo fuera de esta máquina — best-effort,
# nunca marca el ciclo como fallido (el script siempre sale con 0), así que
# `paso` solo aporta el registro de tiempo. Sin RESPALDO_REMOTO_HOST/RUTA
# en .env queda en no-op explicado en el log.
paso respaldo_remoto "$PYEXE" scripts/respaldar_remoto.py

echo "[ciclo] fin — pasos con fallo:${PASOS_FALLIDOS:- ninguno}" >> "$LOGFILE"

# Aviso best-effort si hubo al menos un fallo (deuda de mediano plazo,
# CLAUDE.md). Dos vías, ninguna crítica: notify-send — CONFIRMADO ROTO sin
# sesión gráfica (DEC-120) — y email por SMTP (DEC-161), que queda en
# no-op explicado en el log si no hay credenciales en .env. Ver
# scripts/notificar_fallo_scheduler.sh y scripts/notificar_fallo_email.py.
if [ "$FALLO" = "1" ]; then
    "$(dirname "${BASH_SOURCE[0]}")/../scripts/notificar_fallo_scheduler.sh" "$(pwd)/${LOGFILE}" >> "$LOGFILE" 2>&1
    "$PYEXE" "$(dirname "${BASH_SOURCE[0]}")/../scripts/notificar_fallo_email.py" "$(pwd)/${LOGFILE}" >> "$LOGFILE" 2>&1
fi
