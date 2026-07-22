"""
scraper_principal.py
====================
Entry point del scraper asíncrono de pedidos (SPA Vue.js + Element Plus).

Desde DEC-013 (Fase 5) el código vive en los módulos del paquete scraper/:
config.py, db.py, extractores.py, persistencia.py, workers.py y
orquestador.py. Este archivo conserva dos responsabilidades:

  1. Entry point ejecutable — el comando del Task Scheduler y de uso manual
     no cambia:

        # Carga histórica completa (primera vez)
        py scraper/scraper_principal.py --desde 2026-01-01 --hasta 2026-05-21 --modo completo

        # Actualización incremental (uso normal, cada 2 horas)
        py scraper/scraper_principal.py --modo incremental

  2. Facade de compatibilidad — re-exporta todos los nombres públicos del
     paquete para los importadores existentes (tests, migraciones):
     `from scraper.scraper_principal import X` sigue funcionando.

Configuración recomendada: NUM_WORKERS=5, PAUSA_ENTRE_PEDIDOS_S=1.2,
MAX_REINTENTOS=5, NAV_TIMEOUT_MS=45s

Estimación de tiempo (modo incremental con 5 workers):
    Activos + errores: ~3-5 horas dependiendo del volumen
    Pedidos nuevos del día: ~3-8 minutos
"""

import asyncio
import sys
from pathlib import Path

# El insert de sys.path permite ejecutar este archivo como script
# (py scraper/scraper_principal.py) además de importarlo como paquete:
# pone la raíz del proyecto en el path para resolver comun/ y scraper/.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Utilidades puras y constantes de dominio (AUD-M5/M8) — comun/ es el
# único origen de verdad; se re-exportan aquí por compatibilidad.
from comun import (
    ESTADOS_CERRADOS,
    ESTADOS_FIJAN_CANTIDADES,
    get_db_path,
    to_num,
)
from scraper.config import (
    _RATE_LIMIT,
    CLAVE,
    CONFIG,
    HTML_LOCK,
    LOGIN_LOCK,
    SCREENSHOT_LOCK,
    USUARIO,
    ConfigDict,
    log_event,
    rate_limit_pendiente,
    registrar_rate_limit,
    validar_config,
)
from scraper.db import (
    actualizar_watermark,
    init_db,
    limpiar_errores_resueltos,
)
from scraper.extractores import (
    SEL_BTN_BUSCAR,
    SEL_LISTA_FILAS,
    SEL_LISTA_ID,
    SEL_PAGER_ACTIVO,
    _leer_ids_pagina,
    col_text,
    extraer_detalle_diferencias,
    extraer_estadisticas_monto,
    extraer_gestion_diferencias,
    extraer_info_entrega,
    extraer_info_general,
    extraer_registro_operaciones,
    extraer_subpedidos,
    extraer_timeline,
    guardar_debug,
    login,
    obtener_lista_pedidos,
    obtener_lista_pedidos_con_retry,
    obtener_lista_pedidos_con_watchdog,
)
from scraper.orquestador import (
    _USER_AGENTS,
    _VIEWPORTS,
    build_arg_parser,
    calcular_desde_nuevos,
    construir_resumen,
    main,
)
from scraper.persistencia import (
    _actualizar_estado_subpedido,
    _persistir_secciones_satelite,
    persistencia_worker,
)
from scraper.workers import (
    determinar_modo,
    procesar_pedido,
    scraper_worker,
)

# API pública del facade (DEC-013): declarar el re-export como intencional
# — sin esto, ruff marcaría cada import de arriba como F401 (unused).
__all__ = [
    # comun/
    "ESTADOS_CERRADOS",
    "ESTADOS_FIJAN_CANTIDADES",
    "get_db_path",
    "to_num",
    # config
    "CLAVE",
    "CONFIG",
    "HTML_LOCK",
    "LOGIN_LOCK",
    "SCREENSHOT_LOCK",
    "USUARIO",
    "ConfigDict",
    "_RATE_LIMIT",
    "log_event",
    "rate_limit_pendiente",
    "registrar_rate_limit",
    "validar_config",
    # db
    "actualizar_watermark",
    "init_db",
    "limpiar_errores_resueltos",
    # extractores
    "SEL_BTN_BUSCAR",
    "SEL_LISTA_FILAS",
    "SEL_LISTA_ID",
    "SEL_PAGER_ACTIVO",
    "_leer_ids_pagina",
    "col_text",
    "extraer_detalle_diferencias",
    "extraer_estadisticas_monto",
    "extraer_gestion_diferencias",
    "extraer_info_entrega",
    "extraer_info_general",
    "extraer_registro_operaciones",
    "extraer_subpedidos",
    "extraer_timeline",
    "guardar_debug",
    "login",
    "obtener_lista_pedidos",
    "obtener_lista_pedidos_con_retry",
    "obtener_lista_pedidos_con_watchdog",
    # persistencia
    "_actualizar_estado_subpedido",
    "_persistir_secciones_satelite",
    "persistencia_worker",
    # workers
    "determinar_modo",
    "procesar_pedido",
    "scraper_worker",
    # orquestador
    "_USER_AGENTS",
    "_VIEWPORTS",
    "build_arg_parser",
    "calcular_desde_nuevos",
    "construir_resumen",
    "main",
]


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    log_event(
        "scraper_iniciado",
        msg=f"modo={args.modo} | {args.desde} -> {args.hasta}",
    )
    # AUD-B7: main() retorna el exit code; sys.exit() solo vive aquí.
    sys.exit(asyncio.run(main(args)))
