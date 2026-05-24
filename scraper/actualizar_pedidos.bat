@echo off
REM Moverse a la raíz del proyecto (un nivel arriba de scraper/)
cd /d "%~dp0.."
REM Ejecutar el scraper en modo incremental desde la raíz
py scraper/scraper_principal.py --modo incremental >> logs\scraper_scheduler.log 2>&1
REM Ejecutar el ETL después del scraper
py etl/etl_principal.py >> logs\scraper_scheduler.log 2>&1