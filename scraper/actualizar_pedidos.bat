@echo off
REM Moverse a la raíz del proyecto (un nivel arriba de scraper/)
cd /d "%~dp0.."
REM DEC-015: producción clavada al python del venv (3.12) — ya no depende
REM del default del launcher global (py), que apunta a 3.14 alpha.
set PYEXE=.venv\Scripts\python.exe

REM DEC-028: rotación diaria de scraper_scheduler.log — el archivo único
REM sin límite llegó a 54 GB entre 2026-05-24 y 2026-07-18. Un archivo por
REM día acota el daño de una corrida ruidosa. Fecha vía PowerShell (evita
REM el parseo frágil de %date%, que depende del locale de Windows).
REM
REM Retención de logs (2026-07-19, a pedido expreso del Arquitecto):
REM política ampliada a TODO archivo *.log en logs/, sin excepción — 30
REM días desde su última modificación, incluido el respaldo histórico
REM combinado de DEC-028 (que hasta BUG-020 estaba protegido a propósito).
REM Se decidió así para conservar evidencia mientras se termina de validar
REM que el pipeline funciona de forma correcta; pasado ese punto, limpieza
REM total y automática. scraper.log queda fuera de este purgado — tiene su
REM propia rotación con retención de 30 días en scraper/config.py
REM (TimedRotatingFileHandler), sus archivos rotados no terminan en ".log"
REM así que este patrón no los alcanza (sin lógica duplicada).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set LOGDATE=%%i
set LOGFILE=logs\scraper_scheduler_%LOGDATE%.log
forfiles /p logs /m "*.log" /d -30 /c "cmd /c del @path" >nul 2>nul

REM Ejecutar el scraper en modo incremental desde la raíz
%PYEXE% scraper/scraper_principal.py --modo incremental >> %LOGFILE% 2>&1

REM DEC-039: descarga del inventario de los dos sistemas fuente, pegada
REM al final del scraper de pedidos — minimiza la ventana de desfase
REM entre el estado de subpedidos recién actualizado y la foto de
REM inventario (ver docs/decisions.md). Cada línea corre aunque la
REM anterior falle: una descarga de inventario caída no debe bloquear
REM ni el scraping de pedidos ni el ETL.
%PYEXE% -m scraper.inventario >> %LOGFILE% 2>&1
%PYEXE% -m scraper.bochica >> %LOGFILE% 2>&1

REM TASK-001: captura diaria de "Cambios de inventario". Sin condición de
REM fecha/hora acá a propósito — el propio módulo decide si ya capturó el
REM día anterior (ya_capturado()) y termina de inmediato si sí, así que
REM esta línea puede correr en cualquiera de los ciclos horarios sin
REM duplicar trabajo ni necesitar que el scheduler acierte una hora exacta.
%PYEXE% -m scraper.cambios_inventario >> %LOGFILE% 2>&1

REM DEC-043: cruce de inventario y persistencia en pedidos.db. Va después
REM de las dos descargas (necesita ambos Excel frescos) y con el mismo
REM criterio de aislamiento: si falla, no bloquea el ETL. El dashboard lee
REM el resultado por VIEW en vez de recalcularlo (14,19 s -> 44 ms).
REM Registra la antigüedad de cada fuente: si una descarga de arriba falló
REM y dejó el Excel viejo, la corrida se marca datos_desactualizados=1 y
REM el dashboard lo advierte en vez de mostrar un número que parece fresco.
%PYEXE% -m inventario.persistencia >> %LOGFILE% 2>&1

REM DEC-092: pasada mensual de mantenimiento — el día 1, una sola vez.
REM
REM Va DENTRO de este .bat y no como tarea aparte por una razón medida: el
REM ciclo horario ocupa 44-47 min de cada 60, así que no queda ventana libre
REM y dos procesos escribiendo pedidos.db a la vez se pelean el lock (una
REM corrida de prueba cayó al 71% de éxito por eso). Acá corre en secuencia,
REM después del scraper y ANTES del ETL, para que el mismo ciclo normalice a
REM _num lo que la pasada acaba de capturar.
REM
REM La ventana de fechas la calcula el propio modo: mes calendario recién
REM cerrado más 4 meses de retroceso (el 20-32% de los pedidos se entrega en
REM un mes posterior al de su fecha).
REM
REM IMPORTANTE: la tarea de Windows debe estar configurada como "No iniciar
REM una nueva instancia" si la anterior sigue corriendo. El día 1 este ciclo
REM dura ~60 min en vez de ~45 y se solaparía con el siguiente.
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).Day"') do set DIA_MES=%%i
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).Hour"') do set HORA_DIA=%%i
if "%DIA_MES%"=="1" if "%HORA_DIA%"=="5" (
    echo [mantenimiento] pasada mensual — dia 1 >> %LOGFILE%
    %PYEXE% scraper/scraper_principal.py --modo mantenimiento >> %LOGFILE% 2>&1
)

REM Ejecutar el ETL después del scraper — como módulo (E-7, DEC-018:
REM el paquete editable resuelve los imports, sin hack de sys.path)
%PYEXE% -m etl.etl_principal >> %LOGFILE% 2>&1