@echo off

REM Moverse al directorio del proyecto
cd /d %~dp0

REM Ejecutar el scraper en modo incremental
REM No requiere --desde ni --hasta: lee activos y errores desde la DB
REM y captura solo los pedidos nuevos del dia automaticamente
py scraper_principal.py --modo incremental >> scraper_scheduler.log 2>&1