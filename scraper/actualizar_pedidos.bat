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

REM Ejecutar el ETL después del scraper — como módulo (E-7, DEC-018:
REM el paquete editable resuelve los imports, sin hack de sys.path)
%PYEXE% -m etl.etl_principal >> %LOGFILE% 2>&1