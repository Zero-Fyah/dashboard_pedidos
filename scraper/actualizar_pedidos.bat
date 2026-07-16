@echo off
REM Moverse a la raíz del proyecto (un nivel arriba de scraper/)
cd /d "%~dp0.."
REM DEC-015: producción clavada al python del venv (3.12) — ya no depende
REM del default del launcher global (py), que apunta a 3.14 alpha.
set PYEXE=.venv\Scripts\python.exe
REM Ejecutar el scraper en modo incremental desde la raíz
%PYEXE% scraper/scraper_principal.py --modo incremental >> logs\scraper_scheduler.log 2>&1
REM Ejecutar el ETL después del scraper
%PYEXE% etl/etl_principal.py >> logs\scraper_scheduler.log 2>&1