@echo off
REM Moverse a la raíz del proyecto (un nivel arriba de scraper/)
cd /d "%~dp0.."
REM DEC-015: producción clavada al python del venv (3.12) — ya no depende
REM del default del launcher global (py), que apunta a 3.14 alpha.
set PYEXE=.venv\Scripts\python.exe
REM Ejecutar el scraper en modo incremental desde la raíz
%PYEXE% scraper/scraper_principal.py --modo incremental >> logs\scraper_scheduler.log 2>&1
REM Ejecutar el ETL después del scraper — como módulo (E-7, DEC-018:
REM el paquete editable resuelve los imports, sin hack de sys.path)
%PYEXE% -m etl.etl_principal >> logs\scraper_scheduler.log 2>&1