"""
scraper_principal.py
====================
Scraper asíncrono de pedidos para sistema administrativo interno (SPA Vue.js + Element Plus).

Estimación de tiempo (modo incremental con 5 workers):
    Activos + errores: ~2-3 horas dependiendo del volumen
    Pedidos nuevos del día: ~2-5 minutos

Uso:
    # Carga histórica completa (primera vez)
    py scraper_principal.py --desde 2026-05-01 --hasta 2026-05-21 --modo completo

    # Actualización incremental (uso normal, cada 2 horas)
    py scraper_principal.py --modo incremental
"""

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
)
import aiosqlite
import asyncio
import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
load_dotenv()


# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

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
    NUM_WORKERS: int
    QUEUE_MAXSIZE: int
    MAX_SCREENSHOTS: int
    LOG_FILE: str
    ERRORS_DIR: str


CONFIG: ConfigDict = {
    # URLs (configuradas via variables de entorno)
    "url_login":      os.environ.get("SCRAPER_URL_LOGIN", ""),
    "url_post_login": os.environ.get("SCRAPER_URL_POST_LOGIN", ""),
    "url_pedidos":    os.environ.get("SCRAPER_URL_PEDIDOS", ""),
    "url_detalle":    os.environ.get("SCRAPER_URL_DETALLE", ""),

    # Credenciales (configuradas via variables de entorno)
    "usuario": os.environ.get("SCRAPER_USUARIO", ""),
    "clave":   os.environ.get("SCRAPER_PASSWORD", ""),

    # Timeouts (ms)
    "NAV_TIMEOUT_MS":  30_000,
    "ELEM_TIMEOUT_MS":  15_000,

    # Pausas (segundos)
    "PAUSA_ENTRE_PEDIDOS_S": 0.5,
    "PAUSA_PAGINACION_S":    2.0,

    # Retry
    "MAX_REINTENTOS":   3,
    "BACKOFF_BASE_S":   2,
    "BACKOFF_MAX_S":   30,

    # Circuit breaker
    "CIRCUIT_FAILURE_THRESHOLD": 5,
    "CIRCUIT_COOLDOWN_S":       60,
    "CIRCUIT_MAX_REOPENINGS":    3,

    # Rate limiting
    "RATE_LIMIT_WAIT_S": 30,

    # Paralelismo
    "NUM_WORKERS":   5,
    "QUEUE_MAXSIZE": 100,

    # Observabilidad
    "MAX_SCREENSHOTS": 50,
    "LOG_FILE":        "scraper.log",
    "ERRORS_DIR":      "errors",
}

USUARIO: str = os.environ.get("SCRAPER_USUARIO", CONFIG["usuario"])
CLAVE:   str = os.environ.get("SCRAPER_PASSWORD", CONFIG["clave"])

LOGIN_LOCK:      asyncio.Lock = asyncio.Lock()
SCREENSHOT_LOCK: asyncio.Lock = asyncio.Lock()

ESTADOS_CERRADOS: frozenset[str] = frozenset({
    "completado",
    "cancelado",
    "comentado",
})

ESTADOS_FIJAN_CANTIDADES: frozenset[str] = frozenset({
    "pendiente de confirmación",
    "pendiente de envío (pago inmediato)",
    "pendiente de envío (crédito)",
    "pendiente de envío (contra entrega)",
    "pendiente de entrega",
    "enviado",
    "período contable",
    "completado",
    "cancelado",
    "comentado",
})


# ─────────────────────────────────────────────
# LOGGING JSONL
# ─────────────────────────────────────────────

_file_handler = logging.FileHandler(CONFIG["LOG_FILE"], encoding="utf-8", mode="a")
_file_handler.setFormatter(logging.Formatter("%(message)s"))
_logger = logging.getLogger(__name__)
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
        "ts":          datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level":       level,
        "worker_id":   worker_id,
        "id_pedido":   id_pedido,
        "duracion_ms": duracion_ms,
        "event":       event,
        "msg":         msg,
    }
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    _logger.info(line)


# ─────────────────────────────────────────────
# HELPERS DE EXTRACCIÓN
# ─────────────────────────────────────────────

async def col_text(cols: list, i: int) -> str:
    """Retorna texto limpio de la columna i, o '' si el índice no existe.

    Args:
        cols: Lista de ElementHandle correspondientes a div.goods-col.
        i: Índice de la columna deseada.

    Returns:
        Texto interior de la celda con strip(), o cadena vacía.
    """
    if i < len(cols):
        return (await cols[i].inner_text()).strip()
    return ""


def to_num(val: str) -> float | None:
    """Convierte un string numérico en formato español a float.

    Elimina puntos de separador de miles y reemplaza la coma decimal por
    punto. Retorna None si el valor no es convertible (nunca lanza excepción).

    Args:
        val: String a convertir, ej. "1.234,56" o "200".

    Returns:
        Valor float, o None si la conversión falla.
    """
    try:
        cleaned = val.strip().replace(".", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────

async def login(page: Page, usuario: str, clave: str) -> None:
    """Autentica en el panel administrativo y ajusta el idioma a español.

    Realiza hasta 2 intentos con backoff lineal entre ellos. El cambio de
    idioma se ejecuta después de cada login exitoso: si el botón de idioma
    muestra texto chino, lo cambia a español.

    Args:
        page: Página Playwright sobre la que operar.
        usuario: Nombre de usuario o correo registrado.
        clave: Contraseña de la cuenta.

    Raises:
        RuntimeError: Si la autenticación falla tras 2 intentos consecutivos.
    """
    for intento in range(1, 3):
        try:
            await page.goto(CONFIG["url_login"], timeout=CONFIG["NAV_TIMEOUT_MS"])
            await page.wait_for_load_state("networkidle")

            await page.locator("input[type='email'], input[type='text']").first.fill(usuario)
            await page.locator("input[type='password']").first.fill(clave)
            await page.locator("button[type='submit'], form button").first.click()
            await page.wait_for_url(CONFIG["url_post_login"], timeout=CONFIG["NAV_TIMEOUT_MS"])

            btn = await page.query_selector(".lang-btn")
            if btn and "中文" in await btn.inner_text():
                await btn.click()
                await asyncio.sleep(0.8)
                for op in await page.query_selector_all(".el-dropdown-menu__item"):
                    if "西班牙" in await op.inner_text():
                        await op.click()
                        await asyncio.sleep(1.0)
                        break

            log_event("login_ok", msg=f"Autenticación exitosa (intento {intento})")
            return

        except Exception as exc:
            log_event(
                "login_error",
                level="WARNING",
                msg=f"Intento {intento} fallido: {exc}",
            )
            if intento < 2:
                await asyncio.sleep(CONFIG["BACKOFF_BASE_S"])

    raise RuntimeError("Login fallido tras 2 intentos")


# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────

async def init_db(db_path: str) -> None:
    """Crea las tablas si no existen, aplica PRAGMAs y ejecuta migraciones.

    Las tablas base se crean con CREATE TABLE IF NOT EXISTS. Las columnas
    nuevas se agregan via ALTER TABLE con try/except individual por columna
    para compatibilidad con bases de datos existentes.

    Args:
        db_path: Ruta al archivo SQLite (se crea si no existe).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.commit()

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id_pedido           TEXT PRIMARY KEY,
                fecha               TEXT,
                servicio_cliente    TEXT,
                vendedor            TEXT,
                forma_pago          TEXT,
                comprobante         TEXT,
                nombre_empresa      TEXT,
                nit                 TEXT,
                metodo_entrega      TEXT,
                destinatario        TEXT,
                telefono            TEXT,
                direccion_envio     TEXT,
                observaciones       TEXT,
                scraping_completo   INTEGER DEFAULT 0,
                actualizado_en      TEXT
            );

            CREATE TABLE IF NOT EXISTS subpedidos (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido               TEXT,
                numero_subpedido        TEXT,
                tipo_subpedido          TEXT,
                estado                  TEXT,
                inicio_alistamiento     TEXT,
                alistamiento_completado TEXT,
                alistador               TEXT,
                inicio_inspeccion       TEXT,
                inspeccion_completada   TEXT,
                inspector               TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS lineas_pedido (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido           TEXT,
                numero_subpedido    TEXT,
                tipo_subpedido      TEXT,
                nombre_producto     TEXT,
                referencia          TEXT,
                codigo_barras       TEXT,
                presentacion        TEXT,
                almacen             TEXT,
                cantidad_comprada   REAL,
                cantidad_entregada  REAL,
                precio_unitario     TEXT,
                descuento           TEXT,
                precio_descuento    TEXT,
                monto_pagar         TEXT,
                monto_final         TEXT,
                iva                 TEXT,
                peso_total          TEXT,
                observaciones       TEXT,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );

            CREATE TABLE IF NOT EXISTS errores (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido TEXT,
                momento   TEXT,
                detalle   TEXT
            );

            CREATE TABLE IF NOT EXISTS timeline_pedido (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pedido        TEXT,
                numero_subpedido TEXT,
                paso             INTEGER,
                titulo           TEXT,
                fecha_hora       TEXT,
                completado       INTEGER DEFAULT 0,
                FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
            );
        """)

        for ddl in (
            "ALTER TABLE lineas_pedido ADD COLUMN numero_caja TEXT DEFAULT NULL",
            "ALTER TABLE lineas_pedido ADD COLUMN tipo        TEXT DEFAULT NULL",
        ):
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception:
                pass

        try:
            await db.execute(
                "ALTER TABLE pedidos ADD COLUMN hora TEXT DEFAULT NULL"
            )
            await db.commit()
        except Exception:
            pass

        try:
            await db.execute(
                "ALTER TABLE subpedidos ADD COLUMN "
                "cantidades_definitivas INTEGER DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass

    log_event("db_init", msg=f"Base de datos lista: {db_path}")


# ─────────────────────────────────────────────
# EXTRACCIÓN — LISTA DE PEDIDOS
# ─────────────────────────────────────────────

async def obtener_lista_pedidos(
    page: Page,
    fecha_desde: str,
    fecha_hasta: str,
) -> list[str]:
    """Navega a la lista de pedidos, aplica filtro de fechas y extrae todos los IDs.

    Recorre todas las páginas de resultados hasta detectar la última por la
    presencia de la clase 'disabled' o el atributo disabled en el botón
    de siguiente página.

    Args:
        page: Página Playwright autenticada y activa.
        fecha_desde: Fecha de inicio del filtro en formato YYYY-MM-DD.
        fecha_hasta: Fecha de fin del filtro en formato YYYY-MM-DD.

    Returns:
        Lista de IDs de pedido (strings) en el orden devuelto por el servidor.
    """
    await page.goto(CONFIG["url_pedidos"], timeout=CONFIG["NAV_TIMEOUT_MS"])
    await page.wait_for_load_state("networkidle")

    await page.click("button.el-button.is-link.expand-toggle span")
    await asyncio.sleep(1)

    await page.wait_for_selector(".el-range-input", timeout=CONFIG["ELEM_TIMEOUT_MS"])
    await asyncio.sleep(1)

    inputs = await page.query_selector_all(".el-range-input")
    await inputs[0].click()
    await inputs[0].fill(fecha_desde)
    await page.keyboard.press("Tab")
    await inputs[1].click()
    await inputs[1].fill(fecha_hasta)
    await page.keyboard.press("Enter")

    await page.click(
        "#app > div > div > div > main > div > div > "
        "div.hq-search-form > div > div > "
        "button.el-button.el-button--primary > span"
    )
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])

    todos_los_ids: list[str] = []
    pagina_actual = 1

    while True:
        filas = await page.query_selector_all(
            "#app > div > div > div > main > div > div > "
            "div:nth-child(4) > div > div > table > tbody > tr"
        )
        ids_pagina: list[str] = []
        for fila in filas:
            el = await fila.query_selector(
                "td:nth-child(2) > div > div:nth-child(1) > span.value"
            )
            if el:
                ids_pagina.append((await el.inner_text()).strip())

        todos_los_ids.extend(ids_pagina)
        log_event(
            "pagina_extraida",
            msg=f"Página {pagina_actual} — {len(ids_pagina)} pedidos",
        )

        btn_next      = await page.query_selector("button.btn-next.is-last")
        if btn_next:
            clases        = (await btn_next.get_attribute("class")) or ""
            disabled_attr = await btn_next.get_attribute("disabled")
            if "disabled" in clases or disabled_attr is not None:
                break
            await btn_next.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])
            pagina_actual += 1
        else:
            break

    log_event("lista_completa", msg=f"Total IDs obtenidos: {len(todos_los_ids)}")
    return todos_los_ids


# ─────────────────────────────────────────────
# EXTRACCIÓN — DETALLE DE PEDIDO
# ─────────────────────────────────────────────

async def extraer_info_general(page: Page) -> dict:
    """Extrae los campos del card de información general del pedido.

    Mapea las etiquetas visibles de div.info-item a los nombres de columna
    de la tabla pedidos. Si id_pedido queda vacío (la página no cargó o
    redirigió al login), lanza ValueError para que el caller pueda reintentar.

    Args:
        page: Página Playwright con el detalle del pedido ya cargado.

    Returns:
        Diccionario con todos los campos de cabecera del pedido.

    Raises:
        ValueError: Si el campo id_pedido está vacío tras el scraping.
    """
    datos: dict[str, str] = {
        "id_pedido":        "",
        "fecha":            "",
        "servicio_cliente": "",
        "vendedor":         "",
        "forma_pago":       "",
        "comprobante":      "",
        "nombre_empresa":   "",
        "nit":              "",
        "metodo_entrega":   "",
        "destinatario":     "",
        "telefono":         "",
        "direccion_envio":  "",
        "observaciones":    "",
    }

    mapa: dict[str, str] = {
        "número de pedido":         "id_pedido",
        "fecha del pedido":         "fecha",
        "servicio al cliente":      "servicio_cliente",
        "vendedor":                 "vendedor",
        "forma de pago":            "forma_pago",
        "comprobante":              "comprobante",
        "nombre de la empresa":     "nombre_empresa",
        "nit":                      "nit",
        "método de entrega":        "metodo_entrega",
        "destinatario":             "destinatario",
        "teléfono de contacto":     "telefono",
        "dirección de envío":       "direccion_envio",
        "observaciones del pedido": "observaciones",
    }

    items = await page.query_selector_all("div.info-item")
    for item in items:
        label_el = await item.query_selector(".info-label")
        value_el = await item.query_selector(".info-value")
        if not label_el or not value_el:
            continue
        label = (await label_el.inner_text()).strip().lower().rstrip("：:")
        value = (await value_el.inner_text()).strip()
        if label in mapa:
            datos[mapa[label]] = value

    if not datos["id_pedido"]:
        raise ValueError(
            "id_pedido vacío — la página de detalle no cargó correctamente"
        )

    return datos


async def extraer_subpedidos(page: Page) -> list[dict]:
    """Expande todos los subpedidos y extrae sus datos y líneas de productos.

    El primer subpedido siempre aparece pre-expandido al abrir el detalle.
    Antes de cada clic en el ícono de expansión se verifica la clase para
    no cerrar lo que ya está abierto. Las filas del tbody se leen DESPUÉS
    de expandir todos los subpedidos.

    Si cantidad_comprada o cantidad_entregada no son numéricas, se almacena
    None (NULL en SQLite) y se emite un WARNING en el log. El pedido nunca
    falla por valores no numéricos en cantidades.

    Args:
        page: Página Playwright con el detalle del pedido ya cargado.

    Returns:
        Lista de dicts de subpedido. Cada dict incluye la clave 'lineas'
        con la lista de productos del subpedido.
    """

    async def td_txt(cells: list, i: int, sel: str | None = None) -> str:
        """Texto de una celda td por índice, con sub-selector opcional."""
        if i >= len(cells):
            return ""
        if sel:
            el = await cells[i].query_selector(sel)
            return (await el.inner_text()).strip() if el else ""
        return (await cells[i].inner_text()).strip()

    async def ic_txt(info_col, sel: str) -> str:
        """Texto de un sub-elemento dentro del bloque info de producto."""
        if not info_col:
            return ""
        el = await info_col.query_selector(sel)
        return (await el.inner_text()).strip() if el else ""

    # 1 — Expandir subpedidos que aún no están expandidos
    iconos = await page.query_selector_all("div.el-table__expand-icon")
    for icono in iconos:
        clases = (await icono.get_attribute("class")) or ""
        if "el-table__expand-icon--expanded" not in clases:
            await icono.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
            await icono.click(force=True)
            try:
                await page.wait_for_selector(
                    "td.el-table__expanded-cell",
                    timeout=CONFIG["ELEM_TIMEOUT_MS"],
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)

    # 2 — Leer filas DESPUÉS de haber expandido todo
    filas = await page.query_selector_all(
        "div.el-scrollbar__wrap--hidden-default table tbody tr"
    )

    subpedidos: list[dict] = []

    for fila in filas:

        # — Fila cabecera de subpedido (contiene celda expand) —
        if await fila.query_selector("td.el-table__expand-column"):
            raw_el = await fila.query_selector("span.child-order-id")
            raw    = (await raw_el.inner_text()).strip() if raw_el else ""

            if " + " in raw:
                partes   = raw.split(" + ", 1)
                tipo_sub = partes[0].strip()
                num_sub  = partes[1].strip()
            else:
                tipo_sub = "desconocido"
                num_sub  = raw

            celdas = await fila.query_selector_all("td")
            subpedidos.append({
                "numero_subpedido":        num_sub,
                "tipo_subpedido":          tipo_sub,
                "estado":                  await td_txt(celdas, 3, ".el-tag__content"),
                "inicio_alistamiento":     await td_txt(celdas, 4),
                "alistamiento_completado": await td_txt(celdas, 5),
                "alistador":               await td_txt(celdas, 6),
                "inicio_inspeccion":       await td_txt(celdas, 7),
                "inspeccion_completada":   await td_txt(celdas, 8),
                "inspector":               await td_txt(celdas, 9),
                "lineas":                  [],
            })

        # — Fila de contenido expandido —
        elif await fila.query_selector("td.el-table__expanded-cell") and subpedidos:
            for prod_row in await fila.query_selector_all("div.goods-table-row"):
                cols     = await prod_row.query_selector_all("div.goods-col")
                info_col = cols[1] if len(cols) > 1 else None

                nombre     = await ic_txt(info_col, ".goods-name")
                referencia = await ic_txt(info_col, ".sn-tag")
                cod_raw    = await ic_txt(info_col, ".goods-barcode")
                cod_barras = cod_raw.replace("Código de barras:", "").strip()
                presentac  = await ic_txt(info_col, ".goods-specs span")

                cant_c_str = await col_text(cols, 3)
                cant_e_str = await col_text(cols, 4)
                cant_c     = to_num(cant_c_str)
                cant_e     = to_num(cant_e_str)

                if cant_c is None and cant_c_str:
                    log_event(
                        "cantidad_no_numerica",
                        level="WARNING",
                        msg=f"cantidad_comprada no numérica: '{cant_c_str}'",
                    )
                if cant_e is None and cant_e_str:
                    log_event(
                        "cantidad_no_numerica",
                        level="WARNING",
                        msg=f"cantidad_entregada no numérica: '{cant_e_str}'",
                    )

                tipo_el  = await cols[5].query_selector(".el-tag__content") if len(cols) > 5 else None
                tipo_val = (await tipo_el.inner_text()).strip() if tipo_el else await col_text(cols, 5)

                subpedidos[-1]["lineas"].append({
                    "numero_caja":        await col_text(cols, 0),
                    "nombre_producto":    nombre,
                    "referencia":         referencia,
                    "codigo_barras":      cod_barras,
                    "presentacion":       presentac,
                    "almacen":            await col_text(cols, 2),
                    "cantidad_comprada":  cant_c,
                    "cantidad_entregada": cant_e,
                    "tipo":               tipo_val,
                    "precio_unitario":    await col_text(cols, 6),
                    "descuento":          await col_text(cols, 7),
                    "precio_descuento":   await col_text(cols, 8),
                    "monto_pagar":        await col_text(cols, 9),
                    "monto_final":        await col_text(cols, 10),
                    "iva":                await col_text(cols, 11),
                    "peso_total":         await col_text(cols, 12),
                    "observaciones":      await col_text(cols, 13),
                })

    return subpedidos


async def extraer_timeline(page: Page, id_pedido: str) -> list[dict]:
    """Extrae la línea de tiempo de pasos del pedido.

    Cada step-item contiene un título y una fecha. La clase
    is-completed indica que el paso ya fue completado.

    Args:
        page: Página Playwright con el detalle del pedido cargado.
        id_pedido: ID del pedido en proceso.

    Returns:
        Lista de dicts con los pasos de la línea de tiempo.
    """
    timeline: list[dict] = []
    try:
        wrapper = await page.query_selector("div.order-steps-wrapper")
        if not wrapper:
            return []
        pasos = await wrapper.query_selector_all("div.step-item")
        for i, paso in enumerate(pasos):
            titulo_el  = await paso.query_selector("div.step-title")
            fecha_el   = await paso.query_selector("div.step-time")
            clases     = (await paso.get_attribute("class")) or ""
            completado = 1 if "is-completed" in clases else 0
            titulo     = (await titulo_el.inner_text()).strip() if titulo_el else ""
            fecha_hora = (await fecha_el.inner_text()).strip() if fecha_el else ""
            timeline.append({
                "id_pedido":  id_pedido,
                "paso":       i + 1,
                "titulo":     titulo,
                "fecha_hora": fecha_hora,
                "completado": completado,
            })
    except Exception as exc:
        log_event(
            "timeline_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return timeline


# ─────────────────────────────────────────────
# OBSERVABILIDAD — SCREENSHOTS
# ─────────────────────────────────────────────

async def guardar_screenshot(page: Page, id_pedido: str) -> None:
    """Toma screenshot del estado actual y lo guarda en el directorio de errores.

    Usa SCREENSHOT_LOCK para serializar la rotación: si el directorio supera
    CONFIG["MAX_SCREENSHOTS"] archivos, elimina el más antiguo antes de guardar.

    Args:
        page: Página Playwright activa.
        id_pedido: ID del pedido en proceso (incluido en el nombre del archivo).
    """
    errors_dir = Path(CONFIG["ERRORS_DIR"])
    errors_dir.mkdir(parents=True, exist_ok=True)
    ruta = errors_dir / f"error_{id_pedido}_{int(time.time())}.png"

    async with SCREENSHOT_LOCK:
        archivos = sorted(
            errors_dir.glob("*.png"),
            key=lambda f: f.stat().st_mtime,
        )
        while len(archivos) >= CONFIG["MAX_SCREENSHOTS"]:
            archivos.pop(0).unlink()
        await page.screenshot(path=str(ruta))

    log_event("screenshot_guardado", id_pedido=id_pedido, msg=str(ruta))


# ─────────────────────────────────────────────
# SCRAPING — PEDIDO INDIVIDUAL
# ─────────────────────────────────────────────

async def procesar_pedido(
    worker_id: int,
    page: Page,
    id_pedido: str,
    resultados_queue: asyncio.Queue,
    db_path: str,
) -> bool:
    """Determina el modo de extracción, navega al detalle y publica en la cola.

    Consulta la DB antes de navegar para elegir uno de tres modos:
      - completo:       pedido nuevo; extrae todo (info general, subpedidos, timeline).
      - con_cantidades: algún subpedido fija cantidades; actualiza estado y
                        cantidad_entregada + reemplaza timeline.
      - solo_estado:    solo actualiza estado de cada subpedido y timeline;
                        sin expansión, el más rápido.

    Reintenta hasta CONFIG["MAX_REINTENTOS"] veces con backoff exponencial y
    jitter. Toma screenshot en cada fallo.

    Args:
        worker_id: ID del worker que invoca esta función.
        page: Página Playwright activa del worker.
        id_pedido: ID del pedido a procesar.
        resultados_queue: Cola donde publicar el resultado o el registro de error.
        db_path: Ruta al archivo SQLite para consultar el estado previo.

    Returns:
        True si el pedido fue extraído y publicado con éxito, False si no.
    """
    t_inicio = time.monotonic()

    # ── Determinar modo antes del loop de reintentos ──────────────────────
    async with aiosqlite.connect(db_path) as db_r:
        row = await (await db_r.execute(
            "SELECT scraping_completo FROM pedidos WHERE id_pedido = ?",
            (id_pedido,)
        )).fetchone()
        es_nuevo = row is None

        if not es_nuevo:
            subs_db = await (await db_r.execute(
                "SELECT estado, cantidades_definitivas "
                "FROM subpedidos WHERE id_pedido = ?",
                (id_pedido,)
            )).fetchall()
        else:
            subs_db = []

    if es_nuevo:
        modo = "completo"
    elif any(
        cd == 0 and estado.lower() in ESTADOS_FIJAN_CANTIDADES
        for estado, cd in subs_db
    ):
        modo = "con_cantidades"
    else:
        modo = "solo_estado"

    nav_kwargs: dict = {} if modo == "completo" else {"wait_until": "domcontentloaded"}

    for intento in range(1, CONFIG["MAX_REINTENTOS"] + 1):
        try:
            await page.goto(
                CONFIG["url_detalle"] + id_pedido,
                timeout=CONFIG["NAV_TIMEOUT_MS"],
                **nav_kwargs,
            )

            if "/login" in page.url:
                async with LOGIN_LOCK:
                    log_event(
                        "session_expired",
                        worker_id=worker_id,
                        id_pedido=id_pedido,
                        msg="Sesión expirada — re-login",
                    )
                    await login(page, USUARIO, CLAVE)
                await page.goto(
                    CONFIG["url_detalle"] + id_pedido,
                    timeout=CONFIG["NAV_TIMEOUT_MS"],
                    **nav_kwargs,
                )

            if modo == "completo":
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_selector("div.info-item", timeout=CONFIG["ELEM_TIMEOUT_MS"])
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)

                info_general = await extraer_info_general(page)
                subpedidos   = await extraer_subpedidos(page)
                timeline     = await extraer_timeline(page, id_pedido)
                resultado = {
                    "tipo":         "completo",
                    "id_pedido":    id_pedido,
                    "info_general": info_general,
                    "subpedidos":   subpedidos,
                    "timeline":     timeline,
                }
                n_subs = len(subpedidos)

            elif modo == "con_cantidades":
                subpedidos = await extraer_subpedidos(page)
                timeline   = await extraer_timeline(page, id_pedido)
                resultado = {
                    "tipo":       "con_cantidades",
                    "id_pedido":  id_pedido,
                    "subpedidos": subpedidos,
                    "timeline":   timeline,
                }
                n_subs = len(subpedidos)

            else:  # solo_estado
                filas = await page.query_selector_all(
                    "div.el-scrollbar__wrap--hidden-default table tbody tr"
                )
                subs_estado: list[dict] = []
                for fila in filas:
                    if not await fila.query_selector("td.el-table__expand-column"):
                        continue
                    raw_el  = await fila.query_selector("span.child-order-id")
                    raw     = (await raw_el.inner_text()).strip() if raw_el else ""
                    num_sub = raw.split(" + ", 1)[1].strip() if " + " in raw else raw
                    celdas  = await fila.query_selector_all("td")
                    if len(celdas) > 3:
                        estado_el = await celdas[3].query_selector(".el-tag__content")
                        estado    = (await estado_el.inner_text()).strip() if estado_el else ""
                    else:
                        estado = ""
                    subs_estado.append({"numero_subpedido": num_sub, "estado": estado})

                timeline = await extraer_timeline(page, id_pedido)
                resultado = {
                    "tipo":       "solo_estado",
                    "id_pedido":  id_pedido,
                    "subpedidos": subs_estado,
                    "timeline":   timeline,
                }
                n_subs = len(subs_estado)

            duracion_ms = int((time.monotonic() - t_inicio) * 1000)
            await resultados_queue.put(resultado)
            log_event(
                "pedido_ok",
                worker_id=worker_id,
                id_pedido=id_pedido,
                duracion_ms=duracion_ms,
                msg=f"modo={modo} | {n_subs} subpedidos | intento {intento}",
            )
            await asyncio.sleep(CONFIG["PAUSA_ENTRE_PEDIDOS_S"])
            return True

        except Exception as exc:
            log_event(
                "pedido_error",
                level="WARNING",
                worker_id=worker_id,
                id_pedido=id_pedido,
                msg=f"Intento {intento}/{CONFIG['MAX_REINTENTOS']}: {exc}",
            )
            try:
                await guardar_screenshot(page, id_pedido)
            except Exception as ss_exc:
                log_event(
                    "screenshot_error",
                    level="WARNING",
                    worker_id=worker_id,
                    id_pedido=id_pedido,
                    msg=str(ss_exc),
                )
            if intento < CONFIG["MAX_REINTENTOS"]:
                backoff = min(
                    CONFIG["BACKOFF_BASE_S"] ** intento + random.uniform(0, 1),
                    CONFIG["BACKOFF_MAX_S"],
                )
                await asyncio.sleep(backoff)

    detalle = f"Falló tras {CONFIG['MAX_REINTENTOS']} intentos"
    log_event(
        "pedido_error",
        level="ERROR",
        worker_id=worker_id,
        id_pedido=id_pedido,
        msg=detalle,
    )
    await resultados_queue.put({"id_pedido": id_pedido, "_error": True, "detalle": detalle})
    return False


# ─────────────────────────────────────────────
# PERSISTENCIA
# ─────────────────────────────────────────────

async def persistencia_worker(
    resultados_queue: asyncio.Queue,
    db_path: str,
) -> None:
    """Task única que escribe en SQLite. Termina al recibir el sentinel None.

    Procesa cuatro tipos de registros desde resultados_queue:
      - completo:       upsert en pedidos, DELETE + INSERT en subpedidos y
                        lineas_pedido, scraping_completo=1.
      - con_cantidades: UPDATE cantidad_entregada + estado + cantidades_definitivas=1
                        en subpedidos; reemplaza timeline.
      - solo_estado:    UPDATE estado en subpedidos; reemplaza timeline; marca
                        scraping_completo=1 si todos los subpedidos están cerrados.
      - error (_error=True): INSERT en errores.
    Cada pedido se persiste en una sola transacción atómica (BEGIN/COMMIT).

    Args:
        resultados_queue: Cola de resultados producidos por los scraper_workers.
        db_path: Ruta al archivo SQLite.
    """
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")

        while True:
            resultado = await resultados_queue.get()
            if resultado is None:
                break

            id_pedido = resultado["id_pedido"]

            # — Registro de error —
            if resultado.get("_error"):
                try:
                    await db.execute("BEGIN")
                    await db.execute(
                        "INSERT INTO errores (id_pedido, momento, detalle) VALUES (?, ?, ?)",
                        (
                            id_pedido,
                            datetime.now(timezone.utc).isoformat(),
                            resultado["detalle"],
                        ),
                    )
                    await db.execute("COMMIT")
                except Exception as exc:
                    await db.execute("ROLLBACK")
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error guardando en errores: {exc}",
                    )
                continue

            tipo = resultado.get("tipo", "completo")

            # ── Modo completo ──────────────────────────────────────────────
            if tipo == "completo":
                info   = resultado["info_general"]
                subped = resultado["subpedidos"]

                lineas_rows: list[dict] = []
                for sp in subped:
                    for linea in sp["lineas"]:
                        lineas_rows.append({
                            "id_pedido":         id_pedido,
                            "numero_subpedido":  sp["numero_subpedido"],
                            "tipo_subpedido":    sp["tipo_subpedido"],
                            "nombre_producto":   linea["nombre_producto"],
                            "referencia":        linea["referencia"],
                            "codigo_barras":     linea["codigo_barras"],
                            "presentacion":      linea["presentacion"],
                            "almacen":           linea["almacen"],
                            "cantidad_comprada": linea["cantidad_comprada"],
                            "cantidad_entregada":linea["cantidad_entregada"],
                            "precio_unitario":   linea["precio_unitario"],
                            "descuento":         linea["descuento"],
                            "precio_descuento":  linea["precio_descuento"],
                            "monto_pagar":       linea["monto_pagar"],
                            "monto_final":       linea["monto_final"],
                            "iva":               linea["iva"],
                            "peso_total":        linea["peso_total"],
                            "observaciones":     linea["observaciones"],
                            "numero_caja":       linea["numero_caja"],
                            "tipo":              linea["tipo"],
                        })

                fecha_completa = info.get("fecha", "")
                partes_fecha   = fecha_completa.split(" ")
                fecha_val      = partes_fecha[0] if partes_fecha else ""
                hora_val       = partes_fecha[1] if len(partes_fecha) > 1 else ""

                try:
                    await db.execute("BEGIN")

                    await db.execute(
                        """
                        INSERT INTO pedidos (
                            id_pedido, fecha, hora, servicio_cliente, vendedor, forma_pago,
                            comprobante, nombre_empresa, nit, metodo_entrega,
                            destinatario, telefono, direccion_envio, observaciones,
                            scraping_completo, actualizado_en
                        ) VALUES (
                            :id_pedido, :fecha, :hora, :servicio_cliente, :vendedor, :forma_pago,
                            :comprobante, :nombre_empresa, :nit, :metodo_entrega,
                            :destinatario, :telefono, :direccion_envio, :observaciones,
                            0, :actualizado_en
                        )
                        ON CONFLICT(id_pedido) DO UPDATE SET
                            fecha               = excluded.fecha,
                            hora                = excluded.hora,
                            servicio_cliente    = excluded.servicio_cliente,
                            vendedor            = excluded.vendedor,
                            forma_pago          = excluded.forma_pago,
                            comprobante         = excluded.comprobante,
                            nombre_empresa      = excluded.nombre_empresa,
                            nit                 = excluded.nit,
                            metodo_entrega      = excluded.metodo_entrega,
                            destinatario        = excluded.destinatario,
                            telefono            = excluded.telefono,
                            direccion_envio     = excluded.direccion_envio,
                            observaciones       = excluded.observaciones,
                            actualizado_en      = excluded.actualizado_en
                        """,
                        {
                            **info,
                            "fecha":          fecha_val,
                            "hora":           hora_val,
                            "actualizado_en": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                    await db.execute("DELETE FROM subpedidos     WHERE id_pedido = ?", (id_pedido,))
                    await db.execute("DELETE FROM lineas_pedido  WHERE id_pedido = ?", (id_pedido,))
                    await db.execute("DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,))

                    await db.executemany(
                        """
                        INSERT INTO subpedidos (
                            id_pedido, numero_subpedido, tipo_subpedido, estado,
                            inicio_alistamiento, alistamiento_completado, alistador,
                            inicio_inspeccion, inspeccion_completada, inspector
                        ) VALUES (
                            :id_pedido, :numero_subpedido, :tipo_subpedido, :estado,
                            :inicio_alistamiento, :alistamiento_completado, :alistador,
                            :inicio_inspeccion, :inspeccion_completada, :inspector
                        )
                        """,
                        [
                            {
                                "id_pedido":               id_pedido,
                                "numero_subpedido":        sp["numero_subpedido"],
                                "tipo_subpedido":          sp["tipo_subpedido"],
                                "estado":                  sp["estado"],
                                "inicio_alistamiento":     sp["inicio_alistamiento"],
                                "alistamiento_completado": sp["alistamiento_completado"],
                                "alistador":               sp["alistador"],
                                "inicio_inspeccion":       sp["inicio_inspeccion"],
                                "inspeccion_completada":   sp["inspeccion_completada"],
                                "inspector":               sp["inspector"],
                            }
                            for sp in subped
                        ],
                    )

                    if lineas_rows:
                        await db.executemany(
                            """
                            INSERT INTO lineas_pedido (
                                id_pedido, numero_subpedido, tipo_subpedido,
                                nombre_producto, referencia, codigo_barras, presentacion,
                                almacen, cantidad_comprada, cantidad_entregada,
                                precio_unitario, descuento, precio_descuento,
                                monto_pagar, monto_final, iva, peso_total, observaciones,
                                numero_caja, tipo
                            ) VALUES (
                                :id_pedido, :numero_subpedido, :tipo_subpedido,
                                :nombre_producto, :referencia, :codigo_barras, :presentacion,
                                :almacen, :cantidad_comprada, :cantidad_entregada,
                                :precio_unitario, :descuento, :precio_descuento,
                                :monto_pagar, :monto_final, :iva, :peso_total, :observaciones,
                                :numero_caja, :tipo
                            )
                            """,
                            lineas_rows,
                        )

                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )

                    await db.execute(
                        "UPDATE pedidos SET scraping_completo = 1, actualizado_en = ? WHERE id_pedido = ?",
                        (datetime.now(timezone.utc).isoformat(), id_pedido),
                    )
                    await db.execute("COMMIT")
                    log_event("db_guardado", id_pedido=id_pedido, msg="Pedido persistido")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo pedido: {exc}",
                    )

            # ── Modo con_cantidades ────────────────────────────────────────
            elif tipo == "con_cantidades":
                try:
                    await db.execute("BEGIN")

                    for sp in resultado["subpedidos"]:
                        num_sub = sp["numero_subpedido"]
                        for linea in sp["lineas"]:
                            await db.execute(
                                "UPDATE lineas_pedido SET cantidad_entregada = ? "
                                "WHERE id_pedido = ? AND numero_subpedido = ? "
                                "AND codigo_barras = ?",
                                (linea["cantidad_entregada"], id_pedido, num_sub, linea["codigo_barras"]),
                            )
                        await db.execute(
                            "UPDATE subpedidos SET estado = ?, cantidades_definitivas = 1 "
                            "WHERE id_pedido = ? AND numero_subpedido = ?",
                            (sp["estado"], id_pedido, num_sub),
                        )

                    await db.execute("DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,))
                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )

                    await db.execute("COMMIT")
                    log_event("db_guardado", id_pedido=id_pedido, msg="Cantidades actualizadas")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo con_cantidades: {exc}",
                    )

            # ── Modo solo_estado ───────────────────────────────────────────
            elif tipo == "solo_estado":
                try:
                    await db.execute("BEGIN")

                    for sp in resultado["subpedidos"]:
                        await db.execute(
                            "UPDATE subpedidos SET estado = ? "
                            "WHERE id_pedido = ? AND numero_subpedido = ?",
                            (sp["estado"], id_pedido, sp["numero_subpedido"]),
                        )

                    await db.execute("DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,))
                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )

                    _closed_ph = ",".join("?" * len(ESTADOS_CERRADOS))
                    open_count_row = await (await db.execute(
                        f"SELECT COUNT(*) FROM subpedidos "
                        f"WHERE id_pedido = ? "
                        f"AND LOWER(estado) NOT IN ({_closed_ph})",
                        (id_pedido, *ESTADOS_CERRADOS),
                    )).fetchone()
                    if open_count_row and open_count_row[0] == 0:
                        await db.execute(
                            "UPDATE pedidos SET scraping_completo = 1, actualizado_en = ? "
                            "WHERE id_pedido = ?",
                            (datetime.now(timezone.utc).isoformat(), id_pedido),
                        )

                    await db.execute("COMMIT")
                    log_event("db_guardado", id_pedido=id_pedido, msg="Estado actualizado")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo solo_estado: {exc}",
                    )


# ─────────────────────────────────────────────
# WORKER DE SCRAPING
# ─────────────────────────────────────────────

async def scraper_worker(
    worker_id: int,
    context: BrowserContext,
    pedidos_queue: asyncio.Queue,
    resultados_queue: asyncio.Queue,
    db_path: str,
) -> None:
    """Consume IDs de pedido de la cola y los procesa uno a uno.

    Mantiene un circuit breaker local: si hay CONFIG["CIRCUIT_FAILURE_THRESHOLD"]
    fallos consecutivos pausa CONFIG["CIRCUIT_COOLDOWN_S"] segundos. Si se
    superan CONFIG["CIRCUIT_MAX_REOPENINGS"] reaperturas el worker termina.

    El handler de rate limiting (HTTP 429) se define una vez y se reutiliza en
    todas las páginas del worker mediante una referencia mutable al pedido actual.

    Args:
        worker_id: Identificador único (0 a NUM_WORKERS-1).
        context: BrowserContext independiente de Playwright.
        pedidos_queue: Cola con IDs de pedido. None es el sentinel de fin.
        resultados_queue: Cola donde publicar los resultados extraídos.
        db_path: Ruta al archivo SQLite, pasado a procesar_pedido.
    """
    consecutive_failures = 0
    circuit_reopenings   = 0
    current_pedido: list[str] = [""]

    async def _response_handler(response: Response) -> None:
        if response.status != 429:
            return
        header = response.headers.get("retry-after", "")
        wait_s = int(header) if header.isdigit() else CONFIG["RATE_LIMIT_WAIT_S"]
        log_event(
            "rate_limited",
            worker_id=worker_id,
            id_pedido=current_pedido[0],
            msg=f"HTTP 429 — esperando {wait_s}s",
        )
        await asyncio.sleep(wait_s)

    while True:
        id_pedido = await pedidos_queue.get()
        if id_pedido is None:
            break

        current_pedido[0] = id_pedido
        page = await context.new_page()
        page.on("response", _response_handler)

        try:
            exito = await procesar_pedido(worker_id, page, id_pedido, resultados_queue, db_path)
        finally:
            await page.close()

        if exito:
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        if consecutive_failures >= CONFIG["CIRCUIT_FAILURE_THRESHOLD"]:
            log_event(
                "circuit_open",
                worker_id=worker_id,
                msg=(
                    f"{consecutive_failures} fallos consecutivos — "
                    f"cooldown {CONFIG['CIRCUIT_COOLDOWN_S']}s"
                ),
            )
            await asyncio.sleep(CONFIG["CIRCUIT_COOLDOWN_S"])
            circuit_reopenings += 1

            if circuit_reopenings > CONFIG["CIRCUIT_MAX_REOPENINGS"]:
                log_event(
                    "worker_terminated",
                    worker_id=worker_id,
                    msg=f"Máximo de reaperturas ({CONFIG['CIRCUIT_MAX_REOPENINGS']}) alcanzado",
                )
                return

            consecutive_failures = 0
            log_event(
                "circuit_closed",
                worker_id=worker_id,
                msg=f"Circuit cerrado — reanudando (reapertura {circuit_reopenings})",
            )


# ─────────────────────────────────────────────
# ORQUESTADOR PRINCIPAL
# ─────────────────────────────────────────────

_VIEWPORTS: list[dict] = [
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]


async def main(args: argparse.Namespace) -> None:
    """Orquesta el scraping completo: DB, lista de IDs, workers y persistencia.

    Flujo incremental (3 procesos, sin recorrer el rango completo):
        1. Pedidos activos  — lee DB: scraping_completo=1 con subpedidos abiertos.
        2. Pedidos con error — lee DB: ids en errores que no están completos/cerrados.
        3. Pedidos nuevos   — consulta servidor solo para ayer-hoy y descarta los ya en DB.
    Flujo completo: recorre todas las páginas del rango dado (sin cambios).

    Args:
        args: Namespace de argparse con atributos desde, hasta y modo.
    """
    t_inicio = time.monotonic()
    db_path  = "pedidos.db"

    await init_db(db_path)
    Path(CONFIG["ERRORS_DIR"]).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=False, slow_mo=50)

        # ── Obtener lista de IDs ──────────────────────────────────────────
        if args.modo == "incremental":

            # Proceso 1 — Actualizar pedidos activos (sin recorrer páginas)
            async with aiosqlite.connect(db_path) as db_r:
                rows = await (await db_r.execute("""
                    SELECT DISTINCT p.id_pedido
                    FROM pedidos p
                    JOIN subpedidos s ON p.id_pedido = s.id_pedido
                    WHERE p.scraping_completo = 1
                      AND LOWER(s.estado) NOT IN (
                          'completado','cancelado','comentado'
                      )
                """)).fetchall()
            ids_activos = [r[0] for r in rows]

            # Proceso 2 — Reintentar pedidos con error
            async with aiosqlite.connect(db_path) as db_r:
                rows = await (await db_r.execute("""
                    SELECT DISTINCT id_pedido FROM errores
                    WHERE id_pedido NOT IN (
                        SELECT id_pedido FROM pedidos
                        WHERE scraping_completo = 1
                          AND id_pedido NOT IN (
                            SELECT DISTINCT id_pedido FROM subpedidos
                            WHERE LOWER(estado) NOT IN (
                                'completado','cancelado','comentado'
                            )
                          )
                    )
                """)).fetchall()
            ids_error = [r[0] for r in rows]

            # Proceso 3 — Capturar pedidos nuevos (solo ayer-hoy)
            fecha_ayer = (date.today() - timedelta(days=1)).isoformat()
            fecha_hoy  = date.today().isoformat()

            ctx_0  = await browser.new_context(viewport=_VIEWPORTS[0], locale="es-CO")
            page_0 = await ctx_0.new_page()
            try:
                await login(page_0, USUARIO, CLAVE)
                ids_nuevos_servidor = await obtener_lista_pedidos(
                    page_0, fecha_ayer, fecha_hoy
                )
            finally:
                await page_0.close()
                await ctx_0.close()

            async with aiosqlite.connect(db_path) as db_r:
                rows = await (await db_r.execute(
                    "SELECT id_pedido FROM pedidos"
                )).fetchall()
            ids_en_db  = {r[0] for r in rows}
            ids_nuevos = [i for i in ids_nuevos_servidor if i not in ids_en_db]

            # Unión final sin duplicados
            ids_pendientes: list[str] = list(dict.fromkeys(
                ids_activos + ids_error + ids_nuevos
            ))
            log_event("ids_filtrados", msg=(
                f"Activos: {len(ids_activos)} | "
                f"Errores: {len(ids_error)} | "
                f"Nuevos: {len(ids_nuevos)} | "
                f"Total: {len(ids_pendientes)}"
            ))

        else:
            # Modo completo — recorre todas las páginas del rango dado
            ctx_0  = await browser.new_context(viewport=_VIEWPORTS[0], locale="es-CO")
            page_0 = await ctx_0.new_page()
            try:
                await login(page_0, USUARIO, CLAVE)
                todos_ids = await obtener_lista_pedidos(page_0, args.desde, args.hasta)
            finally:
                await page_0.close()
                await ctx_0.close()

            ids_pendientes = list(todos_ids)
            log_event(
                "ids_filtrados",
                msg=(
                    f"Total servidor: {len(todos_ids)} | "
                    f"Pendientes: {len(ids_pendientes)} | "
                    f"Modo: {args.modo}"
                ),
            )

        if not ids_pendientes:
            log_event("scraper_finalizado", msg="Sin pedidos pendientes")
            await browser.close()
            t_min = (time.monotonic() - t_inicio) / 60
            resumen = {
                "tiempo_total_min":   round(t_min, 2),
                "pedidos_procesados": 0,
                "pedidos_error":      0,
                "tasa_exito_pct":     100.0,
                "pedidos_por_minuto": 0.0,
            }
            print("\n>>>RESUMEN<<<")
            print(json.dumps(resumen, indent=4, ensure_ascii=False))
            sys.exit(0)

        # — Paso 3: crear contextos y login de cada worker —
        contexts: list[BrowserContext] = []
        for wid in range(CONFIG["NUM_WORKERS"]):
            ctx = await browser.new_context(
                viewport=_VIEWPORTS[wid % len(_VIEWPORTS)],
                user_agent=_USER_AGENTS[wid % len(_USER_AGENTS)],
                locale="es-CO",
            )
            p = await ctx.new_page()
            await login(p, USUARIO, CLAVE)
            await p.close()
            contexts.append(ctx)

        # — Paso 4: colas y tasks —
        pedidos_queue:    asyncio.Queue = asyncio.Queue(maxsize=CONFIG["QUEUE_MAXSIZE"])
        resultados_queue: asyncio.Queue = asyncio.Queue()

        persist_task = asyncio.create_task(
            persistencia_worker(resultados_queue, db_path)
        )
        worker_tasks = [
            asyncio.create_task(
                scraper_worker(wid, contexts[wid], pedidos_queue, resultados_queue, db_path)
            )
            for wid in range(CONFIG["NUM_WORKERS"])
        ]

        # — Paso 5: llenar la cola concurrentemente con los workers —
        async def _fill() -> None:
            for pid in ids_pendientes:
                await pedidos_queue.put(pid)
            for _ in range(CONFIG["NUM_WORKERS"]):
                await pedidos_queue.put(None)

        try:
            await asyncio.gather(_fill(), *worker_tasks)
        except Exception as exc:
            log_event("critical_error", level="ERROR", msg=str(exc))
            for t in worker_tasks:
                t.cancel()
        finally:
            await resultados_queue.put(None)
            await persist_task

        # — Paso 6: cerrar contextos y browser —
        for ctx in contexts:
            await ctx.close()
        await browser.close()

    # — Paso 7: resumen final —
    t_min  = (time.monotonic() - t_inicio) / 60
    n_ids  = len(ids_pendientes)

    async with aiosqlite.connect(db_path) as db_r:
        ph    = ",".join("?" * n_ids)
        n_ok  = (await (await db_r.execute(
            f"SELECT COUNT(*) FROM pedidos "
            f"WHERE scraping_completo = 1 AND id_pedido IN ({ph})",
            ids_pendientes,
        )).fetchone())[0]
        n_err = (await (await db_r.execute(
            f"SELECT COUNT(*) FROM errores WHERE id_pedido IN ({ph})",
            ids_pendientes,
        )).fetchone())[0]

    tasa = (n_ok / n_ids * 100) if n_ids else 100.0
    resumen = {
        "tiempo_total_min":   round(t_min, 2),
        "pedidos_procesados": n_ok,
        "pedidos_error":      n_err,
        "tasa_exito_pct":     round(tasa, 2),
        "pedidos_por_minuto": round(n_ids / t_min, 2) if t_min > 0 else 0.0,
    }

    log_event(
        "scraper_finalizado",
        msg=f"Completado en {t_min:.1f} min | OK={n_ok} | ERR={n_err} | Tasa={tasa:.1f}%",
    )
    print("\n>>>RESUMEN<<<")
    print(json.dumps(resumen, indent=4, ensure_ascii=False))
    sys.exit(0 if tasa >= 95.0 else 1)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper de pedidos Calabaza Pets")
    parser.add_argument(
        "--desde",
        default="2025-05-01",
        help="Fecha inicio YYYY-MM-DD",
    )
    parser.add_argument(
        "--hasta",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Fecha fin YYYY-MM-DD",
    )
    parser.add_argument(
        "--modo",
        choices=["completo", "incremental"],
        default="completo",
        help="completo: encola todos los IDs | incremental: omite scraping_completo=1",
    )
    args = parser.parse_args()
    log_event(
        "scraper_iniciado",
        msg=f"modo={args.modo} | {args.desde} -> {args.hasta}",
    )
    asyncio.run(main(args))
