@echo off
REM ============================================================================
REM Arranque del dashboard como tarea de Windows (DEC-105).
REM
REM Hasta acá el dashboard se lanzaba a mano: tras un reinicio o un corte de
REM luz quedaba abajo, y desde la oficina no hay forma de levantarlo. Esta
REM tarea lo deja corriendo al iniciar el equipo.
REM
REM Registro en el Programador de tareas:
REM   - Desencadenador: "Al iniciar el equipo"
REM   - "Ejecutar tanto si el usuario inició sesión como si no"
REM   - "Ejecutar con los privilegios más altos" NO hace falta: escucha en
REM     8501, que no es un puerto privilegiado.
REM   - En "Configuración": desmarcar "Detener la tarea si se ejecuta más de",
REM     porque este proceso está pensado para no terminar nunca.
REM ============================================================================

REM Raíz del proyecto (un nivel arriba de scripts/)
cd /d "%~dp0.."

REM DEC-015: clavado al python del venv (3.12). El launcher global `py` apunta
REM a 3.14 y tiene otra versión de streamlit instalada — invocar por ruta
REM absoluta elimina el riesgo de arrancar con la equivocada.
set PYEXE=.venv\Scripts\python.exe

REM Rotación diaria, mismo criterio que DEC-028 en el .bat del scraper: un
REM archivo único sin límite llegó a 54 GB una vez. Streamlit escribe poco,
REM pero un error en bucle llena disco igual.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set LOGDATE=%%i
set LOGFILE=logs\dashboard_%LOGDATE%.log

if not exist logs mkdir logs

echo. >> %LOGFILE%
echo ==================================================== >> %LOGFILE%
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" >> %LOGFILE%
echo [dashboard] arrancando >> %LOGFILE%

REM La configuración de red vive en .streamlit/config.toml (address, puerto,
REM headless, telemetría). Acá no se repite: una sola fuente de verdad.
%PYEXE% -m streamlit run dashboard\app.py >> %LOGFILE% 2>&1

REM Si el proceso termina, algo lo mató: queda registrado con la hora.
echo [dashboard] el proceso TERMINO — revisar el log de arriba >> %LOGFILE%
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" >> %LOGFILE%
