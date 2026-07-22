"""
config.py — Configuración, credenciales, locks y logging JSONL del scraper.

Raíz del grafo de dependencias del paquete (DEC-013): todos los demás
módulos importan de aquí. No importa ningún otro módulo del paquete.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────


def _env_bool(nombre: str, default: bool) -> bool:
    """Lee un booleano de una variable de entorno (AUD-M11).

    Acepta 1/true/yes/si/sí y 0/false/no (sin distinguir mayúsculas).
    Un valor ausente, vacío o no reconocido retorna el default — nunca
    lanza: CONFIG se construye en import y un .env malformado no debe
    tumbar el proceso antes de poder loggear.
    """
    valor = os.environ.get(nombre, "").strip().lower()
    if valor in ("1", "true", "yes", "si", "sí"):
        return True
    if valor in ("0", "false", "no"):
        return False
    return default


def _env_int(nombre: str, default: int) -> int:
    """Lee un entero de una variable de entorno (AUD-M11).

    Un valor ausente o no numérico retorna el default — mismo criterio
    de no fallar en import que _env_bool().
    """
    valor = os.environ.get(nombre, "").strip()
    try:
        return int(valor)
    except ValueError:
        return default


class ConfigDict(TypedDict):
    url_login: str
    url_post_login: str
    url_pedidos: str
    url_detalle: str
    usuario: str
    clave: str
    NAV_TIMEOUT_MS: int
    ELEM_TIMEOUT_MS: int
    PAUSA_ENTRE_PEDIDOS_S: float
    PAUSA_PAGINACION_S: float
    MAX_REINTENTOS: int
    BACKOFF_BASE_S: int
    BACKOFF_MAX_S: int
    CIRCUIT_FAILURE_THRESHOLD: int
    CIRCUIT_COOLDOWN_S: int
    CIRCUIT_MAX_REOPENINGS: int
    RATE_LIMIT_WAIT_S: int
    INCREMENTAL_LOOKBACK_MAX_DIAS: int
    NUM_WORKERS: int
    RUN_TIMEOUT_S: int
    MAX_SCREENSHOTS: int
    MAX_HTML_DEBUG: int
    LOG_FILE: str
    ERRORS_DIR: str
    DEBUG_DIR: str
    HEADLESS: bool
    SLOW_MO: int


CONFIG: ConfigDict = {
    # URLs (configuradas via variables de entorno)
    "url_login": os.environ.get("SCRAPER_URL_LOGIN", ""),
    "url_post_login": os.environ.get("SCRAPER_URL_POST_LOGIN", ""),
    "url_pedidos": os.environ.get("SCRAPER_URL_PEDIDOS", ""),
    "url_detalle": os.environ.get("SCRAPER_URL_DETALLE", ""),
    # Credenciales (configuradas via variables de entorno)
    "usuario": os.environ.get("SCRAPER_USUARIO", ""),
    "clave": os.environ.get("SCRAPER_PASSWORD", ""),
    # Timeouts (ms)
    "NAV_TIMEOUT_MS": 45_000,
    "ELEM_TIMEOUT_MS": 20_000,
    # Pausas (segundos)
    "PAUSA_ENTRE_PEDIDOS_S": 1.2,
    "PAUSA_PAGINACION_S": 2.0,
    # Retry
    "MAX_REINTENTOS": 5,
    "BACKOFF_BASE_S": 2,
    "BACKOFF_MAX_S": 60,
    # Circuit breaker
    "CIRCUIT_FAILURE_THRESHOLD": 5,
    "CIRCUIT_COOLDOWN_S": 90,
    "CIRCUIT_MAX_REOPENINGS": 3,
    # Rate limiting
    "RATE_LIMIT_WAIT_S": 45,
    # AUD-M4 (DEC-012): tope en días de la ventana del carril 3 del
    # incremental. Acota el lookback cuando el watermark quedó atrás;
    # outages mayores requieren recuperación manual con --desde.
    "INCREMENTAL_LOOKBACK_MAX_DIAS": 7,
    # Paralelismo. DEC-030 Fase 4: configurable via .env (mismo patrón
    # AUD-M11 que HEADLESS/SLOW_MO) para poder correr el experimento de
    # contención con NUM_WORKERS reducido sin editar código. Default 5
    # preserva el comportamiento histórico.
    "NUM_WORKERS": _env_int("SCRAPER_NUM_WORKERS", 5),
    # FIX C-1 (auditoría 2026-07-01): timeout global del run como red de
    # seguridad de última instancia. Debe superar el peor caso legítimo:
    # la carga histórica más larga registrada tomó ~5.5h (DEC-010) y el
    # incremental estimado es 3-5h.
    "RUN_TIMEOUT_S": 8 * 3600,
    # Observabilidad
    "MAX_SCREENSHOTS": 50,
    "MAX_HTML_DEBUG": 50,
    "LOG_FILE": str(Path(__file__).parent.parent / "logs" / "scraper.log"),
    "ERRORS_DIR": str(Path(__file__).parent.parent / "data" / "errors"),
    "DEBUG_DIR": str(Path(__file__).parent.parent / "data" / "debug"),
    # AUD-M11: configurables via .env. Los defaults preservan el
    # comportamiento histórico (headed + 50ms) — el modo headless se activa
    # explícitamente con SCRAPER_HEADLESS=true tras validarlo contra la SPA
    # (posible detección de headless; ver decisions.md).
    "HEADLESS": _env_bool("SCRAPER_HEADLESS", False),
    "SLOW_MO": _env_int("SCRAPER_SLOW_MO", 50),
}

# AUD-B8: CONFIG ya leyó SCRAPER_USUARIO/SCRAPER_PASSWORD — no releer env.
USUARIO: str = CONFIG["usuario"]
CLAVE: str = CONFIG["clave"]

LOGIN_LOCK: asyncio.Lock = asyncio.Lock()
SCREENSHOT_LOCK: asyncio.Lock = asyncio.Lock()
HTML_LOCK: asyncio.Lock = asyncio.Lock()

# AUD-M2 (auditoría 2026-07-01): estado compartido de rate limiting.
# Playwright ejecuta los handlers de respuesta como tasks fire-and-forget:
# un sleep dentro del handler duerme a esa task, no al worker. El handler
# de 429 solo registra la señal aquí; cada worker consulta la señal en su
# propio loop y duerme él mismo antes de procesar el siguiente pedido.
# Sin lock: el event loop es único y ambas operaciones son atómicas a
# nivel de bytecode sobre un dict.
_RATE_LIMIT: dict[str, float] = {"hasta": 0.0}


def registrar_rate_limit(retry_after: str, *, ahora: float | None = None) -> float:
    """Registra una señal de rate limit compartida entre todos los workers.

    Extiende la ventana de pausa hasta ahora + wait_s. Las señales
    solapadas nunca acortan una ventana ya registrada (se toma el máximo).

    Args:
        retry_after: Valor del header Retry-After de la respuesta 429.
                     Si no es un entero, se usa CONFIG["RATE_LIMIT_WAIT_S"].
        ahora: Reloj monotónico a usar (inyectable para tests).
               Default: time.monotonic().

    Returns:
        Segundos de espera registrados para esta señal.
    """
    if ahora is None:
        ahora = time.monotonic()
    wait_s = float(retry_after) if retry_after.isdigit() else float(CONFIG["RATE_LIMIT_WAIT_S"])
    _RATE_LIMIT["hasta"] = max(_RATE_LIMIT["hasta"], ahora + wait_s)
    return wait_s


def rate_limit_pendiente(*, ahora: float | None = None) -> float:
    """Retorna los segundos restantes de la pausa por rate limit.

    Args:
        ahora: Reloj monotónico a usar (inyectable para tests).
               Default: time.monotonic().

    Returns:
        Segundos restantes de pausa, o 0.0 si no hay señal activa.
    """
    if ahora is None:
        ahora = time.monotonic()
    return max(0.0, _RATE_LIMIT["hasta"] - ahora)


# ESTADOS_CERRADOS y ESTADOS_FIJAN_CANTIDADES viven en comun/ (AUD-M5/M8)
# y se importan arriba — único origen de verdad para las listas de estados.


# ─────────────────────────────────────────────
# LOGGING JSONL
# ─────────────────────────────────────────────

Path(CONFIG["LOG_FILE"]).parent.mkdir(parents=True, exist_ok=True)
_logger = logging.getLogger(__name__)
# N-4: guard de handlers — un re-import del módulo (importlib.reload,
# doble bootstrap) no debe duplicar el handler: los loggers de `logging`
# son globales al proceso y sobreviven al reload del módulo.
if not _logger.handlers:
    # Retención de logs (2026-07-19, a pedido del Arquitecto): scraper.log
    # crecía sin límite (llegó a 280 MB) porque era un solo FileHandler sin
    # rotación — ya estaba anotado como pendiente en DEC-028. Con
    # TimedRotatingFileHandler, cada medianoche local se renombra a
    # "scraper.log.YYYY-MM-DD" y se purgan automáticamente los que superan
    # backupCount=30 (30 días de retención, sin excepción, mismo criterio
    # que el purgado de scraper_scheduler_*.log en actualizar_pedidos.bat).
    # Los archivos rotados no terminan en ".log", así que el purgado de
    # forfiles del .bat no los toca — cada uno gestiona su propia retención,
    # sin lógica duplicada.
    _file_handler = TimedRotatingFileHandler(
        CONFIG["LOG_FILE"],
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.setLevel(logging.DEBUG)
    _logger.addHandler(_file_handler)
    _logger.propagate = False


def log_event(
    event: str,
    *,
    level: str = "INFO",
    worker_id: int | None = None,
    id_pedido: str | None = None,
    duracion_ms: int | None = None,
    msg: str = "",
) -> None:
    """Emite una línea JSONL a stdout y al archivo de log.

    Args:
        event: Categoría semántica (pedido_ok, session_expired, etc.).
        level: Nivel de log: DEBUG, INFO, WARNING o ERROR.
        worker_id: ID del worker, o None si es el proceso principal.
        id_pedido: ID del pedido en proceso, o None.
        duracion_ms: Duración de la operación en milisegundos, o None.
        msg: Descripción textual del evento.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "worker_id": worker_id,
        "id_pedido": id_pedido,
        "duracion_ms": duracion_ms,
        "event": event,
        "msg": msg,
    }
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    # N-4: fallback a INFO — un nivel desconocido no debe tirar
    # AttributeError y perder el evento que se intentaba registrar.
    # El isinstance cubre el caso "warning" en minúsculas, donde getattr
    # devolvería la función logging.warning en lugar de un entero.
    nivel = getattr(logging, level, None)
    _logger.log(nivel if isinstance(nivel, int) else logging.INFO, line)


# ─────────────────────────────────────────────
# VALIDACIÓN DE CONFIGURACIÓN
# ─────────────────────────────────────────────


def validar_config() -> None:
    """Falla rápido si faltan variables críticas de entorno.

    Raises:
        RuntimeError: Con la lista completa de variables faltantes.
    """
    faltantes: list[str] = []
    checks = {
        "SCRAPER_URL_LOGIN": CONFIG["url_login"],
        "SCRAPER_URL_POST_LOGIN": CONFIG["url_post_login"],
        "SCRAPER_URL_PEDIDOS": CONFIG["url_pedidos"],
        "SCRAPER_URL_DETALLE": CONFIG["url_detalle"],
        "SCRAPER_USUARIO": USUARIO,
        "SCRAPER_PASSWORD": CLAVE,
    }
    for env_key, value in checks.items():
        if not value:
            faltantes.append(env_key)
    if faltantes:
        raise RuntimeError(
            f"Variables de entorno faltantes: {', '.join(faltantes)}. "
            "Defínalas en un archivo .env antes de ejecutar."
        )
