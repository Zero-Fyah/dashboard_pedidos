"""
extractores.py — Todo lo que lee la SPA con Playwright (DEC-013).

Login, listado de pedidos (con retry y re-login, AUD-B1), los 8 extractores
del detalle de pedido y el volcado de debug (screenshot + HTML).
"""

import asyncio
import json
import re
import time
from pathlib import Path

from playwright.async_api import (
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from comun import to_num
from scraper.config import (
    CONFIG,
    HTML_LOCK,
    SCREENSHOT_LOCK,
    log_event,
)

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


# to_num() vive en comun/ (AUD-M5) y se importa arriba.


def _reclasificar_descuento(
    etiquetas: list[str], monto_crudo: str, texto_completo: str
) -> tuple[str, str]:
    """Lógica pura de DEC-024 — separa (monto, tipos) a partir de piezas ya leídas.

    Extraída de `leer_celda_descuento()` (DRY, DEC-022) para que tanto la
    lectura por ElementHandle como el batch de `page.evaluate()` (DEC-030
    Fase 3, `extraer_subpedidos`) compartan un único origen de verdad —
    ninguna lógica de negocio se duplica en JS.

    Args:
        etiquetas: Textos de `.el-tag__content` en la celda, en orden.
        monto_crudo: Texto del primer `<span>` que no es un el-tag
            (ranura de monto), o "" si no hay ninguno.
        texto_completo: `inner_text`/`textContent` completo de la celda,
            usado como fallback cuando no hay ningún span.

    Returns:
        Tupla (monto, tipos) — ver `leer_celda_descuento()`.
    """
    etiquetas = [e for e in etiquetas if e]
    # de-duplicar preservando orden
    vistas: set[str] = set()
    etiquetas = [e for e in etiquetas if not (e in vistas or vistas.add(e))]

    monto = monto_crudo
    if not monto:
        monto = texto_completo.split("\n")[0].strip()

    # Si la ranura trae una etiqueta en vez de un monto, se reclasifica.
    if monto and monto != "-" and to_num(monto) is None:
        if monto not in etiquetas:
            etiquetas.insert(0, monto)
        monto = "-"

    return (monto or "-", " | ".join(etiquetas))


async def leer_celda_descuento(celda) -> tuple[str, str]:
    """Separa la celda de descuento en (monto, tipos) — DEC-024.

    La celda combina una ranura de monto y una o más etiquetas de tipo:

        <span>-</span>
        <span class="el-tag"><span class="el-tag__content">Tipo de cambio9%</span></span>

    El texto de la ranura solo se considera monto si `to_num()` lo puede
    convertir o si es un placeholder; cualquier otra cosa (p. ej.
    "Promoción3%", que en algunas filas ocupa esa posición) es una
    etiqueta y se acumula con las demás.

    Args:
        celda: ElementHandle de la celda de descuento.

    Returns:
        Tupla (monto, tipos). `monto` es "-" cuando no hay valor numérico;
        `tipos` une las etiquetas con " | " y es "" si no hay ninguna.
    """
    etiquetas: list[str] = []
    for tag in await celda.query_selector_all(".el-tag__content"):
        texto = (await tag.inner_text()).strip()
        etiquetas.append(texto)

    # Primer span que no forme parte de un el-tag: la ranura del monto.
    monto_crudo = ""
    for span in await celda.query_selector_all("span"):
        clases = (await span.get_attribute("class")) or ""
        if "el-tag" not in clases:
            monto_crudo = (await span.inner_text()).strip()
            break

    texto_completo = (await celda.inner_text()).strip()
    return _reclasificar_descuento(etiquetas, monto_crudo, texto_completo)


def _unir_presentacion(specs: list[str]) -> str:
    """Lógica pura de DEC-026 — une textos no vacíos con " | ".

    Extraída de `leer_presentacion()` (DRY, DEC-022) por el mismo motivo
    que `_reclasificar_descuento()`.
    """
    return " | ".join(t for t in specs if t)


async def leer_presentacion(info_col) -> str:
    """Une todos los <span> de .goods-specs — DEC-026.

    Un producto puede traer varios atributos (Tamaño, Color,
    Presentación...) en spans separados dentro de .goods-specs. Antes se
    leía con `query_selector` (singular) y se perdía todo menos el
    primer span.

    Args:
        info_col: Columna .goods-info-col del producto (puede ser None).

    Returns:
        Los textos no vacíos unidos con " | " (mismo separador que
        descuento_tipo, DEC-024), o "" si no hay info_col ni specs.
    """
    if not info_col:
        return ""
    spans = await info_col.query_selector_all(".goods-specs span")
    textos = [(await span.inner_text()).strip() for span in spans]
    return _unir_presentacion(textos)


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────


async def login(page: Page, usuario: str, clave: str) -> None:
    """Autentica en el panel administrativo y ajusta el idioma a español.

    Realiza hasta 3 intentos con backoff lineal entre ellos. El cambio de
    idioma se ejecuta después de cada login exitoso: si el botón de idioma
    muestra texto chino, lo cambia a español. Usa esperas semánticas en lugar
    de networkidle para compatibilidad con Vue.js SPA con polling continuo.

    Args:
        page: Página Playwright sobre la que operar.
        usuario: Nombre de usuario o correo registrado.
        clave: Contraseña de la cuenta.

    Raises:
        RuntimeError: Si la autenticación falla tras 3 intentos consecutivos.
    """
    for intento in range(1, 4):
        try:
            await page.goto(CONFIG["url_login"], timeout=CONFIG["NAV_TIMEOUT_MS"])
            await page.wait_for_selector(
                "input[type='password'], input[type='email'], input[type='text']",
                timeout=CONFIG["ELEM_TIMEOUT_MS"],
            )

            await page.locator("input[type='email'], input[type='text']").first.fill(usuario)
            await page.locator("input[type='password']").first.fill(clave)
            await page.locator("button[type='submit'], form button").first.click()
            await page.wait_for_function(
                f"() => window.location.href.startsWith({json.dumps(CONFIG['url_post_login'])})",
                timeout=CONFIG["NAV_TIMEOUT_MS"],
            )
            await page.wait_for_selector(
                "#app, .el-container, .el-header, .el-main, main, "
                "[class*='layout'], [class*='container']",
                timeout=CONFIG["ELEM_TIMEOUT_MS"],
            )

            btn = await page.query_selector(".lang-btn")
            if btn and "中文" in await btn.inner_text():
                await btn.click()
                await asyncio.sleep(0.8)
                opciones = (
                    await page.locator(".el-dropdown-menu__item")
                    .filter(has_text=re.compile(r"西班牙|español", re.IGNORECASE))
                    .all()
                )
                for op in opciones:
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
            if intento < 3:
                await asyncio.sleep(CONFIG["BACKOFF_BASE_S"])

    raise RuntimeError("Login fallido tras 3 intentos")


# ─────────────────────────────────────────────
# EXTRACCIÓN — LISTA DE PEDIDOS
# ─────────────────────────────────────────────

# AUD-B1: selectores semánticos de la lista de pedidos — anclados a clases
# estables de Element Plus y del formulario, no a la ruta absoluta del DOM
# (#app > div > ... > div:nth-child(4)) que se rompía con cualquier div nuevo.
SEL_LISTA_FILAS = "main table tbody tr"
SEL_LISTA_ID = "td:nth-child(2) span.value"
SEL_BTN_BUSCAR = "div.hq-search-form button.el-button--primary"
SEL_PAGER_ACTIVO = ".el-pager li.is-active, .el-pager li.active"


# DEC-030 Fase 1: recolección cruda en un solo page.evaluate — con páginas de
# 100 filas, el par query_selector+inner_text por fila costaba ~200 round-trips
# Playwright↔browser por página. El JS solo recolecta (texto o null por fila);
# la normalización queda en Python, donde los tests unitarios la cubren.
_JS_LEER_IDS = """([selFilas, selId]) =>
    Array.from(document.querySelectorAll(selFilas)).map((fila) => {
        const el = fila.querySelector(selId);
        return el ? el.textContent : null;
    })
"""


async def _leer_ids_pagina(page: Page) -> list[str]:
    """Extrae los IDs de pedido de las filas visibles de la página actual.

    Helper de obtener_lista_pedidos() (N-5): una sola implementación para la
    lectura normal y para la relectura del bloque de retry de BUG-016.

    Args:
        page: Página Playwright con la lista de pedidos cargada.

    Returns:
        Lista de IDs (strings) de la página visible; vacía si la tabla no
        tiene filas o aún no renderizó.
    """
    crudos: list[str | None] = await page.evaluate(_JS_LEER_IDS, [SEL_LISTA_FILAS, SEL_LISTA_ID])
    return [t.strip() for t in crudos if t and t.strip()]


async def _leer_total_listado(page: Page) -> int | None:
    """Lee el conteo real del rango filtrado desde `.el-pagination__total`.

    DEC-030 Fase 1: el listado expone "Total N registros" tras aplicar el
    filtro. Ese N permite loggear progreso real ("página 3 de 21"), cortar
    el loop sin depender solo del botón next, y validar al final que la
    cantidad de IDs recogidos coincide con lo que el servidor declaró.

    Returns:
        El total como entero, o None si el elemento no está o no parsea —
        None nunca es error: el paginado funciona igual que antes sin él.
    """
    try:
        el = await page.query_selector(".el-pagination__total")
        if not el:
            return None
        m = re.search(r"(\d[\d.,]*)", (await el.inner_text()).strip())
        return int(re.sub(r"[.,]", "", m.group(1))) if m else None
    except Exception:
        return None


async def _seleccionar_max_page_size(page: Page) -> int:
    """Selecciona la mayor opción del dropdown de tamaño de página del listado.

    DEC-030 Fase 1: el listado pagina de a 10 (el mínimo de Element Plus).
    Este helper abre el dropdown `.el-pagination__sizes` y elige la opción
    numérica más alta disponible (p. ej. "100/página"), reduciendo ~10× la
    cantidad de páginas a recorrer.

    Best-effort deliberado: cualquier fallo (dropdown ausente, opciones con
    otro formato, click fallido) se loggea y retorna 10 — el paginado sigue
    con el tamaño default, nunca se rompe por esta optimización.

    Returns:
        El tamaño de página efectivo tras el intento (10 si no se cambió).
    """
    try:
        trigger = page.locator(".el-pagination__sizes .el-select__wrapper")
        if await trigger.count() == 0:
            log_event(
                "pagesize_no_disponible",
                level="WARNING",
                msg="Dropdown de tamaño de página no encontrado — se pagina de a 10",
            )
            return 10
        await trigger.click()
        opciones = page.locator(".el-select-dropdown__item:visible")
        await opciones.first.wait_for(state="visible", timeout=CONFIG["ELEM_TIMEOUT_MS"])
        textos = await opciones.all_inner_texts()

        tams: list[tuple[int, int]] = []  # (tamaño, índice de la opción)
        for i, t in enumerate(textos):
            m = re.match(r"\s*(\d+)", t)
            if m:
                tams.append((int(m.group(1)), i))
        if not tams:
            log_event(
                "pagesize_no_disponible",
                level="WARNING",
                msg=f"Opciones de tamaño sin número reconocible: {textos!r} — se pagina de a 10",
            )
            return 10

        mejor, idx = max(tams)
        await opciones.nth(idx).click()
        log_event(
            "pagesize_seleccionado",
            msg=f"Tamaño de página: {mejor} (opciones: {[t for t, _ in tams]})",
        )
        return mejor
    except Exception as exc:
        log_event(
            "pagesize_no_disponible",
            level="WARNING",
            msg=f"No se pudo cambiar el tamaño de página ({type(exc).__name__}: {exc}) — se pagina de a 10",
        )
        return 10


async def _configurar_listado(page: Page, fecha_desde: str, fecha_hasta: str) -> int:
    """Navega al listado, aplica el filtro de fechas y fija el tamaño de página.

    DEC-030 Fase 1b: extraído de obtener_lista_pedidos() para poder repetir
    la misma secuencia de setup en cada refresco periódico, sin duplicar
    lógica (ver _REFRESH_CADA_N_PAGINAS más abajo).

    Args:
        page: Página Playwright autenticada y activa.
        fecha_desde: Fecha de inicio del filtro en formato YYYY-MM-DD.
        fecha_hasta: Fecha de fin del filtro en formato YYYY-MM-DD.

    Returns:
        El tamaño de página efectivo tras el intento (10 si no se pudo cambiar).
    """
    await page.goto(CONFIG["url_pedidos"], timeout=CONFIG["NAV_TIMEOUT_MS"])
    # N-2: la señal de página lista es el propio botón que se va a clickear.
    await page.wait_for_selector(
        "button.el-button.is-link.expand-toggle",
        timeout=CONFIG["ELEM_TIMEOUT_MS"],
    )

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

    await page.locator(SEL_BTN_BUSCAR).first.click()
    # N-2: sin networkidle — la espera semántica de resultados es el
    # wait_for_selector de filas al entrar al loop; esta pausa solo
    # amortigua el arranque de la consulta (agent.md: sleep como pausa
    # adicional, nunca como única espera).
    await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])

    # DEC-030 Fase 1: máximo tamaño de página disponible. El cambio de
    # tamaño re-consulta la página 1; la pausa cubre ese re-fetch.
    tam_pagina = await _seleccionar_max_page_size(page)
    if tam_pagina != 10:
        await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])
    return tam_pagina


async def _esperar_cambio_primer_id(
    page: Page, anterior: str | None, evento_timeout: str, contexto: str
) -> None:
    """Espera a que el primer ID visible de la tabla cambie respecto a `anterior`.

    Señal semántica compartida por el avance normal (click "siguiente") y el
    salto directo de página (DEC-030 Fase 1b) — Element Plus actualiza el
    número del paginador antes de que lleguen las filas nuevas del servidor,
    así que el número activo no sirve como confirmación real (ver DEC-030).
    """
    try:
        await page.wait_for_function(
            """([selFilas, selId, anterior]) => {
                const fila = document.querySelector(selFilas);
                const el = fila && fila.querySelector(selId);
                const id = el ? el.textContent.trim() : null;
                return id !== null && id !== anterior;
            }""",
            arg=[SEL_LISTA_FILAS, SEL_LISTA_ID, anterior],
            timeout=CONFIG["ELEM_TIMEOUT_MS"],
        )
    except PlaywrightTimeoutError:
        log_event(
            evento_timeout,
            level="WARNING",
            msg=f"La tabla no cambió de contenido {contexto} en {CONFIG['ELEM_TIMEOUT_MS']}ms",
        )
    # Settle corto para el patch reactivo de Vue — la relectura BUG-016
    # sigue cubriendo cualquier render tardío residual.
    await asyncio.sleep(0.25)


async def _saltar_a_pagina(page: Page, num_pagina: int) -> None:
    """Salta directamente a `num_pagina` con el input "Ir a" del paginador.

    DEC-030 Fase 1b: se usa tras cada refresco periódico para retomar en la
    página correcta sin re-clickear "siguiente" num_pagina-1 veces.
    """
    ids_antes = await _leer_ids_pagina(page)
    anterior = ids_antes[0] if ids_antes else None
    campo = page.locator(".el-pagination__jump .el-pagination__editor input")
    await campo.click()
    await campo.fill(str(num_pagina))
    await campo.press("Enter")
    await _esperar_cambio_primer_id(
        page, anterior, "paginador_salto_timeout", f"tras saltar a la página {num_pagina}"
    )


# DEC-030 Fase 1b: refrescar la sesión del listado cada N páginas. Medido
# 2026-07-19: sin refresco, el paginado degrada progresivamente y puede
# colgarse sin que ningún timeout lo capture — visto tras ~50 páginas de
# 100 filas (~5.000 filas acumuladas en la misma página del navegador).
# N=30 validado limpio en un piloto de exactamente 30 páginas (incluyó y
# se recuperó de un pico transitorio en las páginas 18-21).
_REFRESH_CADA_N_PAGINAS = 30


async def obtener_lista_pedidos(
    page: Page,
    fecha_desde: str,
    fecha_hasta: str,
) -> list[str]:
    """Navega a la lista de pedidos, aplica filtro de fechas y extrae todos los IDs.

    Recorre todas las páginas de resultados hasta detectar la última por la
    presencia de la clase 'disabled' o el atributo disabled en el botón
    de siguiente página, con un freno de seguridad adicional por el total
    declarado del listado (DEC-030 Fase 1). Cada _REFRESH_CADA_N_PAGINAS
    páginas, refresca la sesión del listado (Fase 1b) en vez de clickear
    "siguiente", para evitar la degradación por estado acumulado en una
    sesión de página continua. Antes de recorrer, selecciona el mayor
    tamaño de página disponible y valida al final el conteo de IDs únicos
    contra el total declarado.

    Usa esperas semánticas con timeout explícito en lugar de networkidle
    (N-2): con una SPA Vue.js de polling continuo, networkidle resolvía antes
    del re-renderizado de la tabla (causa raíz de BUG-016) o tardaba de más.
    Para reintentos con re-login usar obtener_lista_pedidos_con_retry().

    Args:
        page: Página Playwright autenticada y activa.
        fecha_desde: Fecha de inicio del filtro en formato YYYY-MM-DD.
        fecha_hasta: Fecha de fin del filtro en formato YYYY-MM-DD.

    Returns:
        Lista de IDs de pedido (strings) en el orden devuelto por el servidor.
    """
    tam_pagina = await _configurar_listado(page, fecha_desde, fecha_hasta)
    total_declarado = await _leer_total_listado(page)
    paginas_estimadas: int | None = None
    if total_declarado is not None:
        paginas_estimadas = max(1, -(-total_declarado // tam_pagina))
        log_event(
            "lista_total_declarado",
            msg=(
                f"Total declarado por el listado: {total_declarado} — "
                f"~{paginas_estimadas} páginas de {tam_pagina}"
            ),
        )

    todos_los_ids: list[str] = []
    pagina_actual = 1
    t_pagina = time.monotonic()

    while True:
        try:
            await page.wait_for_selector(
                SEL_LISTA_FILAS,
                state="visible",
                timeout=CONFIG["ELEM_TIMEOUT_MS"],
            )
        except PlaywrightTimeoutError:
            # AUD-B1: el timeout ya no se silencia — puede ser un rango sin
            # pedidos (benigno) o un selector roto por cambio de la SPA.
            log_event(
                "lista_tabla_timeout",
                level="WARNING",
                msg=(
                    f"Tabla de lista sin filas visibles tras "
                    f"{CONFIG['ELEM_TIMEOUT_MS']}ms en página {pagina_actual} — "
                    "rango sin pedidos o selector desactualizado"
                ),
            )
        ids_pagina = await _leer_ids_pagina(page)

        if not ids_pagina and pagina_actual > 1:
            # BUG-016: Vue puede re-renderizar después del wait — una
            # relectura tras pausa antes de evaluar el botón Next.
            await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])
            ids_pagina = await _leer_ids_pagina(page)

        todos_los_ids.extend(ids_pagina)

        _progreso = f" de ~{paginas_estimadas}" if paginas_estimadas else ""
        log_event(
            "pagina_extraida",
            duracion_ms=int((time.monotonic() - t_pagina) * 1000),
            msg=f"Página {pagina_actual}{_progreso} — {len(ids_pagina)} pedidos",
        )

        # DEC-030 Fase 1: freno de seguridad por total declarado — si el
        # botón next nunca se deshabilita (DOM cambiado), el loop no puede
        # superar las páginas esperadas por más de un margen de cortesía.
        if paginas_estimadas is not None and pagina_actual >= paginas_estimadas + 2:
            log_event(
                "lista_paginas_excedidas",
                level="WARNING",
                msg=(
                    f"Página {pagina_actual} supera las ~{paginas_estimadas} "
                    "esperadas — se corta el paginado (revisar botón next/total)"
                ),
            )
            break

        btn_next = await page.query_selector("button.btn-next")
        if btn_next:
            aria_disabled = await btn_next.get_attribute("aria-disabled")
            if aria_disabled == "true":
                break
            t_pagina = time.monotonic()

            if pagina_actual % _REFRESH_CADA_N_PAGINAS == 0:
                # DEC-030 Fase 1b: en vez de clickear "siguiente", refrescar
                # la sesión del listado y saltar a la página correcta.
                log_event(
                    "paginado_refresco",
                    msg=f"Refrescando la sesión del listado tras la página {pagina_actual}",
                )
                tam_refrescado = await _configurar_listado(page, fecha_desde, fecha_hasta)
                if tam_refrescado != tam_pagina:
                    log_event(
                        "paginado_refresco_tam_distinto",
                        level="WARNING",
                        msg=(
                            f"Tamaño de página tras refresco ({tam_refrescado}) "
                            f"distinto del original ({tam_pagina}) — la numeración "
                            "de páginas puede desalinearse; el dedupe de IDs cubre el residual"
                        ),
                    )
                await _saltar_a_pagina(page, pagina_actual + 1)
            else:
                primer_id = ids_pagina[0] if ids_pagina else None
                await btn_next.click()
                # DEC-030 Fase 1: espera semántica real — el primer ID visible
                # de la tabla debe cambiar. La espera anterior (número activo
                # del paginador) confirmaba algo que Element Plus actualiza
                # antes de que lleguen las filas nuevas del servidor, y por eso
                # convivía con un sleep fijo de 2 s por página que era el 60%
                # del tiempo de paginado medido (DEC-030).
                await _esperar_cambio_primer_id(
                    page,
                    primer_id,
                    "paginador_espera_timeout",
                    f"tras el click a la página {pagina_actual + 1}",
                )
            pagina_actual += 1
        else:
            break

    if total_declarado is not None:
        unicos = len(dict.fromkeys(todos_los_ids))
        if unicos != total_declarado:
            log_event(
                "lista_total_discrepancia",
                level="WARNING",
                msg=(
                    f"IDs únicos recogidos: {unicos} vs total declarado: "
                    f"{total_declarado} — revisar si el listado cambió durante el escaneo"
                ),
            )

    log_event("lista_completa", msg=f"Total IDs obtenidos: {len(todos_los_ids)}")
    return todos_los_ids


async def obtener_lista_pedidos_con_retry(
    page: Page,
    fecha_desde: str,
    fecha_hasta: str,
    usuario: str,
    clave: str,
    max_intentos: int = 2,
) -> list[str]:
    """Envuelve obtener_lista_pedidos() con reintento y re-login (AUD-B1).

    Si el listado falla (sesión expirada, DOM cambiado a mitad de recorrido,
    timeout de navegación), re-autentica y reintenta desde cero. El resultado
    de un intento fallido se descarta completo: el upsert por id_pedido hace
    inocuo volver a listar desde la primera página.

    Args:
        page: Página Playwright sobre la que operar.
        fecha_desde: Fecha de inicio del filtro en formato YYYY-MM-DD.
        fecha_hasta: Fecha de fin del filtro en formato YYYY-MM-DD.
        usuario: Credencial para el re-login entre intentos.
        clave: Credencial para el re-login entre intentos.
        max_intentos: Total de intentos (default 2 = un reintento).

    Returns:
        Lista de IDs de pedido del intento exitoso.

    Raises:
        Exception: La del último intento, si todos fallaron.
    """
    for intento in range(1, max_intentos + 1):
        try:
            return await obtener_lista_pedidos(page, fecha_desde, fecha_hasta)
        except Exception as exc:
            if intento >= max_intentos:
                log_event(
                    "lista_error",
                    level="ERROR",
                    msg=f"Listado fallido tras {max_intentos} intentos: {exc}",
                )
                raise
            log_event(
                "lista_reintento",
                level="WARNING",
                msg=f"Intento {intento} de listado fallido ({exc}) — re-login y reintento",
            )
            await login(page, usuario, clave)
    raise RuntimeError("unreachable")  # pragma: no cover


async def obtener_lista_pedidos_con_watchdog(
    page: Page,
    fecha_desde: str,
    fecha_hasta: str,
    usuario: str,
    clave: str,
) -> list[str]:
    """Envuelve obtener_lista_pedidos_con_retry() con un timeout global (DEC-034).

    Ningún timeout local (click, wait_for_selector) protege contra un
    navegador que se congela a nivel de protocolo DevTools — si el proceso
    del navegador deja de responder, Playwright no tiene nada que reportar
    y la corrida queda colgada en silencio indefinidamente. Observado 2
    veces durante el backfill DEC-027 (rangos de ~20+ páginas, 8-17 min de
    silencio total, proceso vivo pero sin progreso); causa raíz exacta no
    confirmada a nivel de código, mitigado aquí con un límite de tiempo
    duro (mismo patrón que FIX C-1 para la fase de workers).

    Args:
        page, fecha_desde, fecha_hasta, usuario, clave: pasados tal cual a
            obtener_lista_pedidos_con_retry().

    Returns:
        Lista de IDs de pedido.

    Raises:
        asyncio.TimeoutError: si el listado no termina dentro de
            CONFIG["LISTADO_TIMEOUT_S"] — la corrida termina en error en
            vez de quedar colgada.
    """
    try:
        return await asyncio.wait_for(
            obtener_lista_pedidos_con_retry(page, fecha_desde, fecha_hasta, usuario, clave),
            timeout=CONFIG["LISTADO_TIMEOUT_S"],
        )
    except asyncio.TimeoutError:
        log_event(
            "listado_watchdog_timeout",
            level="ERROR",
            msg=(
                f"El listado de pedidos no terminó en "
                f"{CONFIG['LISTADO_TIMEOUT_S']}s — posible cuelgue del "
                "navegador a nivel de protocolo (DEC-034); la corrida "
                "termina en error en vez de quedar colgada indefinidamente"
            ),
        )
        raise


# ─────────────────────────────────────────────
# EXTRACCIÓN — DETALLE DE PEDIDO
# ─────────────────────────────────────────────


# DEC-030 Fase 3: un solo evaluate en vez de hasta 4 round-trips por
# info-item (~16 items → hasta 64 llamadas). El JS solo recolecta
# etiqueta→valor en bruto; el mapeo etiqueta→columna (`mapa`) y la
# validación de id_pedido se quedan en Python, sin cambios.
_JS_INFO_GENERAL = """
() => {
    const out = {};
    document.querySelectorAll('div.info-item').forEach((item) => {
        const l = item.querySelector('.info-label');
        const v = item.querySelector('.info-value');
        if (!l || !v) return;
        const label = l.textContent.trim().toLowerCase().replace(/[：:]+$/, '');
        out[label] = v.textContent.trim();
    });
    return out;
}
"""


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
        "id_pedido": "",
        "fecha": "",
        "servicio_cliente": "",
        "vendedor": "",
        "forma_pago": "",
        "comprobante": "",
        "nombre_empresa": "",
        "nit": "",
        "metodo_entrega": "",
        "destinatario": "",
        "telefono": "",
        "direccion_envio": "",
        "observaciones": "",
        "alistador_pedido": "",
        "inspector_pedido": "",
        "movil_cliente": "",
        # DEC-032: solo se renderizan con metodo_entrega='Almacen'
        # (autorrecogida) — reemplazan destinatario/teléfono en ese caso.
        "persona_recogida": "",
        "movil_recogida": "",
        # DEC-033: solo se renderizan con forma_pago='Pago a crédito'.
        "dias_credito": "",
        "inicio_credito": "",
        "vencimiento_credito": "",
    }

    mapa: dict[str, str] = {
        "número de pedido": "id_pedido",
        "fecha del pedido": "fecha",
        "servicio al cliente": "servicio_cliente",
        "vendedor": "vendedor",
        "forma de pago": "forma_pago",
        "comprobante": "comprobante",
        "nombre de la empresa": "nombre_empresa",
        "nit": "nit",
        "método de entrega": "metodo_entrega",
        "destinatario": "destinatario",
        "teléfono de contacto": "telefono",
        "dirección de envío": "direccion_envio",
        "observaciones del pedido": "observaciones",
        "alistador": "alistador_pedido",
        "inspector": "inspector_pedido",
        "móvil del cliente": "movil_cliente",
        "persona de recogida": "persona_recogida",
        "móvil de recogida": "movil_recogida",
        "días de crédito": "dias_credito",
        "inicio de crédito": "inicio_credito",
        "vencimiento de crédito": "vencimiento_credito",
    }

    crudo = await page.evaluate(_JS_INFO_GENERAL)
    # DEC-032: aviso de etiquetas no mapeadas — mismo criterio que
    # extraer_info_entrega() (DEC-023). "Persona de recogida"/"Móvil de
    # recogida" se perdían en silencio antes de esta entrada porque acá
    # no existía ningún mecanismo de aviso.
    desconocidas = [label for label in crudo if label and label not in mapa]
    for label, value in crudo.items():
        if label in mapa:
            datos[mapa[label]] = value
    if desconocidas:
        log_event(
            "info_general_etiqueta_desconocida",
            id_pedido=crudo.get("número de pedido", ""),
            level="WARNING",
            msg=(
                "Etiquetas nuevas en información básica del pedido — campo "
                f"no capturado (DEC-032): {sorted(set(desconocidas))}"
            ),
        )

    if not datos["id_pedido"]:
        raise ValueError("id_pedido vacío — la página de detalle no cargó correctamente")

    pid = datos["id_pedido"]
    if len(pid) < 3 or pid.lower() in ("n/a", "null", "-"):
        raise ValueError(f"id_pedido inválido: '{pid}'")

    return datos


# DEC-023: mapeo etiqueta de la SPA → clave del dict de resultado. La
# tabla de entrega cambia de layout según el método (Ruta agrega Conductor
# y Vehículo de entrega), así que se lee por etiqueta, nunca por posición.
_ETIQUETAS_ENTREGA: dict[str, str] = {
    "Método de entrega": "entrega_metodo_texto",
    "Despachador": "despachador",
    "Conductor": "conductor",
    # DEC-093: es la franja PACTADA con el cliente, no el momento en que se
    # entregó. Medido: nunca es anterior a la fecha del pedido (mediana +4
    # días), 331 pedidos la tienen en el futuro ahora mismo, y cuando
    # `obs_entrega` trae una fecha coincide con ella —373/373 en formato ISO
    # y 4.172/4.275 escrita en español—.
    "Hora de entrega": "hora_entrega",
    "Vehículo de entrega": "vehiculo_entrega",
    "Observaciones": "obs_entrega",
}

# Etiquetas presentes en la tabla que no se extraen a propósito (su valor
# es un botón que abre un modal, no un dato).
_ETIQUETAS_ENTREGA_IGNORADAS: frozenset[str] = frozenset({"Número de estante de inspección"})


async def extraer_info_entrega(page: Page, id_pedido: str) -> dict:
    """Extrae el card 'Información de entrega' (tabla el-descriptions).

    Lee **por etiqueta** (DEC-023): empareja cada celda
    `el-descriptions__label` con la celda `el-descriptions__content`
    siguiente. El layout de la tabla depende del método de entrega —
    'Ruta' agrega las filas Conductor y Vehículo de entrega — por lo que
    el indexado posicional anterior guardaba el conductor en
    `hora_entrega` y el vehículo en `obs_entrega` (BUG-019).

    Una etiqueta desconocida emite `entrega_etiqueta_desconocida`
    (WARNING) en lugar de perderse en silencio.

    Args:
        page: Página Playwright con el detalle del pedido ya cargado.
        id_pedido: ID del pedido, solo para los logs.

    Returns:
        Dict con 8 claves. Todas vacías si el card no existe.
    """
    resultado = {
        "entrega_metodo_texto": "",
        "entrega_ruta_tag": "",
        "entrega_descuento_tag": "",
        "despachador": "",
        "conductor": "",
        "hora_entrega": "",
        "vehiculo_entrega": "",
        "obs_entrega": "",
    }
    try:
        tabla = await page.query_selector(".el-descriptions__table.is-bordered")
        if not tabla:
            return resultado

        celdas = await tabla.query_selector_all("td")
        desconocidas: list[str] = []

        for i, celda in enumerate(celdas):
            clases = (await celda.get_attribute("class")) or ""
            if "el-descriptions__label" not in clases:
                continue
            etiqueta = (await celda.inner_text()).strip()
            if etiqueta in _ETIQUETAS_ENTREGA_IGNORADAS:
                continue
            clave = _ETIQUETAS_ENTREGA.get(etiqueta)
            if clave is None:
                if etiqueta:
                    desconocidas.append(etiqueta)
                continue
            if i + 1 >= len(celdas):
                continue
            celda_valor = celdas[i + 1]

            if clave == "entrega_metodo_texto":
                # La celda del método incluye tags (ruta/descuento) que se
                # extraen aparte y se quitan del texto plano.
                texto_raw = (await celda_valor.inner_text()).strip()
                for tag_el in await celda_valor.query_selector_all(".el-tag"):
                    tag_txt = (await tag_el.inner_text()).strip()
                    texto_raw = texto_raw.replace(tag_txt, "").strip()
                resultado["entrega_metodo_texto"] = texto_raw

                tag_ruta = await celda_valor.query_selector(".el-tag--primary .el-tag__content")
                if tag_ruta:
                    resultado["entrega_ruta_tag"] = (await tag_ruta.inner_text()).strip()

                tag_dto = await celda_valor.query_selector(".el-tag--warning .el-tag__content")
                if tag_dto:
                    resultado["entrega_descuento_tag"] = (await tag_dto.inner_text()).strip()
            else:
                resultado[clave] = (await celda_valor.inner_text()).strip()

        if desconocidas:
            log_event(
                "entrega_etiqueta_desconocida",
                id_pedido=id_pedido,
                level="WARNING",
                msg=(
                    "Etiquetas nuevas en la tabla de entrega — campo no "
                    f"capturado (DEC-023): {sorted(set(desconocidas))}"
                ),
            )

    except Exception as exc:
        log_event(
            "entrega_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return resultado


# DEC-030 Fase 3: un solo evaluate en vez de hasta ~5 round-trips por
# fila (~10 filas → hasta 50 llamadas). Misma lógica exacta: primer
# <span> de la celda de concepto que NO sea un el-tag; JS solo recolecta,
# ninguna decisión de negocio se mueve de Python.
_JS_ESTADISTICAS = """
() => {
    const card = document.querySelector('.amount-statistics-card');
    if (!card) return null;
    const tagDif = card.querySelector(
        '.statistics-header .el-tag--warning, .statistics-header .el-tag--dark'
    );
    const filas = Array.from(card.querySelectorAll('.amount-statistics-table tbody tr'))
        .map((fila) => {
            const celdas = Array.from(fila.querySelectorAll('td'));
            if (celdas.length === 0) return null;
            const celdaConcepto = celdas[0];
            let conceptoTxt = '';
            for (const span of celdaConcepto.querySelectorAll('.cell > span')) {
                if (!(span.className || '').includes('el-tag')) {
                    conceptoTxt = span.textContent.trim();
                    break;
                }
            }
            if (!conceptoTxt) conceptoTxt = celdaConcepto.textContent.trim();
            const tagEl = celdaConcepto.querySelector('.el-tag .el-tag__content');
            return {
                concepto: conceptoTxt,
                concepto_tag: tagEl ? tagEl.textContent.trim() : '',
                monto_pagar: celdas.length > 1 ? celdas[1].textContent.trim() : '',
                monto_final: celdas.length > 2 ? celdas[2].textContent.trim() : '',
                diferencia: celdas.length > 3 ? celdas[3].textContent.trim() : '',
            };
        })
        .filter((f) => f !== null);
    return { hay_diferencia: tagDif !== null, filas };
}
"""


async def extraer_estadisticas_monto(page: Page, id_pedido: str) -> tuple[list[dict], bool | None]:
    """Extrae la tabla 'Estadísticas de monto del pedido'.

    Las filas son dinámicas (N variable según el pedido y los descuentos
    aplicables). Detecta si hay diferencia en el envío por el tag warning
    en el header del card.

    Returns:
        Tupla (lista_filas, hay_diferencia).
        hay_diferencia es True/False si el card se pudo leer, o None si
        no se pudo verificar (card ausente o excepción).
    """
    filas_data: list[dict] = []
    # FIX C-3 (auditoría 2026-07-01): None = no verificado (card ausente
    # o excepción). Distingue "sin diferencia" de "no se pudo comprobar".
    hay_diferencia: bool | None = None
    try:
        r = await page.evaluate(_JS_ESTADISTICAS)
        if r is None:
            return filas_data, hay_diferencia

        # FIX C-3: card presente → estado verificado (True o False)
        hay_diferencia = r["hay_diferencia"]

        for orden, f in enumerate(r["filas"], start=1):
            filas_data.append(
                {
                    "id_pedido": id_pedido,
                    "orden": orden,
                    "concepto": f["concepto"],
                    "concepto_tag": f["concepto_tag"],
                    "monto_pagar": f["monto_pagar"],
                    "monto_final": f["monto_final"],
                    "diferencia": f["diferencia"],
                }
            )

    except Exception as exc:
        log_event(
            "estadisticas_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
        hay_diferencia = None  # FIX C-3: excepción → estado no verificado
    return filas_data, hay_diferencia


# DEC-030 Fase 3: un solo evaluate en vez de hasta 8 round-trips (4 items
# × label+valor). Card condicional (26% de los pedidos), pero cuando
# existe, la lectura ahora es de un solo golpe.
_JS_GESTION_DIF = """
() => {
    const card = document.querySelector('.difference-card-wrapper');
    if (!card) return null;
    return Array.from(card.querySelectorAll('.difference-content .difference-item')).map((item) => {
        const v = item.querySelector('.item-value');
        return v ? v.textContent.trim() : '';
    });
}
"""


async def extraer_gestion_diferencias(page: Page, id_pedido: str) -> dict | None:
    """Extrae el card 'Gestión de diferencias en el envío'.

    Card condicional — solo aparece cuando hay_diferencia=True.
    Contiene 4 valores fijos en .difference-item en este orden:
      Total a pagar del pedido, Monto final a pagar,
      Monto pagado, Monto de diferencia.

    Returns:
        Dict con los 4 valores, o None si el card no existe.
    """
    try:
        valores = await page.evaluate(_JS_GESTION_DIF)
        if valores is None:
            return None

        return {
            "id_pedido": id_pedido,
            "total_pagar_pedido": valores[0] if len(valores) > 0 else "",
            "monto_final_pagar": valores[1] if len(valores) > 1 else "",
            "monto_pagado": valores[2] if len(valores) > 2 else "",
            "monto_diferencia": valores[3] if len(valores) > 3 else "",
        }

    except Exception as exc:
        log_event(
            "gestion_dif_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return None


# DEC-030 Fase 3 (híbrido): batch de 12 de las 13 columnas en un solo
# evaluate. La celda de descuento (índice 4) se queda FUERA de este JS a
# propósito — sigue pasando por leer_celda_descuento(), que ya tiene su
# propia suite de tests (DEC-024) y no vale la pena duplicar en JS. El
# array conserva `null` para filas con <13 celdas (sin filtrar) para que
# el índice siga alineado 1:1 con los ElementHandle de fila en Python.
_JS_DETALLE_DIF = """
() => {
    const card = document.querySelector('.diff-items-card');
    if (!card) return null;
    return Array.from(card.querySelectorAll('tbody tr')).map((fila) => {
        const celdas = Array.from(fila.querySelectorAll('td'));
        if (celdas.length < 13) return null;
        const tipoEl = celdas[2].querySelector('.el-tag__content');
        return {
            nombre_producto: celdas[0].textContent.trim(),
            especificacion: celdas[1].textContent.trim(),
            tipo: tipoEl ? tipoEl.textContent.trim() : celdas[2].textContent.trim(),
            precio_unitario: celdas[3].textContent.trim(),
            precio_descuento: celdas[5].textContent.trim(),
            cantidad_pedido: celdas[6].textContent.trim(),
            cantidad_entregada: celdas[7].textContent.trim(),
            diferencia_cantidad: celdas[8].textContent.trim(),
            monto_pagar_pedido: celdas[9].textContent.trim(),
            monto_final_pagar: celdas[10].textContent.trim(),
            iva: celdas[11].textContent.trim(),
            monto_diferencia: celdas[12].textContent.trim(),
        };
    });
}
"""


async def extraer_detalle_diferencias(page: Page, id_pedido: str) -> list[dict]:
    """Extrae el card 'Detalle de diferencias'.

    Card condicional — solo aparece cuando hay_diferencia=True.
    Tabla con 13 columnas en orden fijo (verificado contra HTML real):
      0  Nombre del producto      6  Cantidad del pedido
      1  Especificación            7  Cantidad real entregada
      2  Tipo (tag)                8  Diferencia de cantidad
      3  Precio unitario           9  Monto a pagar del pedido
      4  Descuento (tag o '-')    10  Monto final a pagar
      5  Precio con descuento     11  IVA
                                  12  Monto de diferencia

    Returns:
        Lista de dicts, vacía si el card no existe.
    """
    resultado: list[dict] = []
    try:
        card = await page.query_selector(".diff-items-card")
        if not card:
            return resultado

        datos_bulk = await page.evaluate(_JS_DETALLE_DIF)
        if datos_bulk is None:
            return resultado
        filas = await card.query_selector_all("tbody tr")

        for fila, datos in zip(filas, datos_bulk, strict=False):
            if datos is None:
                continue
            celdas = await fila.query_selector_all("td")
            if len(celdas) < 13:
                continue

            # DEC-024: misma separación monto/tipo que en lineas_pedido —
            # antes esta celda guardaba el tag (el tipo) dentro de descuento.
            dto_val, dto_tipo = await leer_celda_descuento(celdas[4])

            resultado.append(
                {
                    "id_pedido": id_pedido,
                    "descuento": dto_val,
                    "descuento_tipo": dto_tipo,
                    **datos,
                }
            )

    except Exception as exc:
        log_event(
            "detalle_dif_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return resultado


# DEC-030 Fase 3: mismo patrón que timeline — un solo evaluate en vez de
# hasta 3 round-trips por item (tiempo/usuario/contenido). El split de
# "accion - referencia" y el mapeo de clase→tipo_usuario son texto crudo
# desde JS; la interpretación (accion/referencia) se queda en Python.
_JS_REGISTRO_OPS = """
() => {
    return Array.from(document.querySelectorAll('.operate-log-content .log-item')).map((item) => {
        const t = item.querySelector('.log-time');
        const u = item.querySelector('.log-user');
        const c = item.querySelector('.log-content');
        let tipo_usuario = '';
        if (u) {
            const cls = u.className || '';
            if (cls.includes('user-type-member')) tipo_usuario = 'member';
            else if (cls.includes('user-type-staff')) tipo_usuario = 'staff';
            else if (cls.includes('user-type-system')) tipo_usuario = 'system';
        }
        return {
            momento: t ? t.textContent.trim() : '',
            usuario: u ? u.textContent.trim() : '',
            tipo_usuario: tipo_usuario,
            contenido: c ? c.textContent.trim() : '',
        };
    });
}
"""


async def extraer_registro_operaciones(page: Page, id_pedido: str) -> list[dict]:
    """Extrae el card 'Registro de operaciones'.

    Cada .log-item contiene:
      .log-time    → momento (datetime string)
      .log-user    → nombre de usuario; clase CSS indica tipo:
                     user-type-member | user-type-system | user-type-staff
      .log-content → texto de la acción, a veces con sufijo ' - referencia'

    Returns:
        Lista de dicts, vacía si no hay registros o el card no existe.
    """
    resultado: list[dict] = []
    try:
        items = await page.evaluate(_JS_REGISTRO_OPS)
        for item in items:
            partes = item["contenido"].split(" - ", 1)
            accion = partes[0].strip()
            referencia = partes[1].strip() if len(partes) > 1 else ""
            resultado.append(
                {
                    "id_pedido": id_pedido,
                    "momento": item["momento"],
                    "usuario": item["usuario"],
                    "tipo_usuario": item["tipo_usuario"],
                    "accion": accion,
                    "referencia": referencia,
                }
            )

    except Exception as exc:
        log_event(
            "registro_ops_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return resultado


# DEC-030 Fase 3 (híbrido): la lectura de filas + líneas de producto se
# colapsa a un solo evaluate — antes era el mayor contribuyente de
# round-trips del scraper (hasta 15-20 llamadas por línea de producto).
# Descuento y presentación quedan FUERA del JS a propósito: el JS solo
# recolecta las piezas crudas (etiquetas, candidato a monto, texto
# completo / lista de specs) y Python las procesa con
# _reclasificar_descuento()/_unir_presentacion() — el mismo origen de
# verdad que usan leer_celda_descuento()/leer_presentacion() y sus tests
# (DEC-022, DRY). cantidad_comprada/entregada se quedan como texto crudo:
# to_num() y el WARNING de "no numérica" siguen en Python, sin cambios.
_JS_SUBPEDIDOS = """
() => {
    const filas = Array.from(document.querySelectorAll(
        'div.el-scrollbar__wrap--hidden-default table tbody tr'
    ));
    const subpedidos = [];
    for (const fila of filas) {
        const expandCol = fila.querySelector('td.el-table__expand-column');
        if (expandCol) {
            const rawEl = fila.querySelector('span.child-order-id');
            const raw = rawEl ? rawEl.textContent.trim() : '';
            const celdas = Array.from(fila.querySelectorAll('td'));
            const txt = (i, sel) => {
                if (i >= celdas.length) return '';
                if (sel) {
                    const el = celdas[i].querySelector(sel);
                    return el ? el.textContent.trim() : '';
                }
                return celdas[i].textContent.trim();
            };
            subpedidos.push({
                raw_child_order_id: raw,
                estado: txt(3, '.el-tag__content'),
                inicio_alistamiento: txt(4),
                alistamiento_completado: txt(5),
                alistador: txt(6),
                inicio_inspeccion: txt(7),
                inspeccion_completada: txt(8),
                inspector: txt(9),
                lineas: [],
            });
        } else if (fila.querySelector('td.el-table__expanded-cell') && subpedidos.length > 0) {
            const prodRows = Array.from(fila.querySelectorAll('div.goods-table-row'));
            for (const prodRow of prodRows) {
                const cols = Array.from(prodRow.querySelectorAll('div.goods-col'));
                const colTxt = (i) => (i < cols.length ? cols[i].textContent.trim() : '');
                const infoCol = cols.length > 1 ? cols[1] : null;
                let nombre = '', referencia = '', codRaw = '', specs = [];
                if (infoCol) {
                    const n = infoCol.querySelector('.goods-name');
                    nombre = n ? n.textContent.trim() : '';
                    const r = infoCol.querySelector('.sn-tag');
                    referencia = r ? r.textContent.trim() : '';
                    const b = infoCol.querySelector('.goods-barcode');
                    codRaw = b ? b.textContent.trim() : '';
                    specs = Array.from(infoCol.querySelectorAll('.goods-specs span'))
                        .map((s) => s.textContent.trim());
                }
                let tipoVal = colTxt(5);
                if (cols.length > 5) {
                    const tEl = cols[5].querySelector('.el-tag__content');
                    if (tEl) tipoVal = tEl.textContent.trim();
                }
                let dtoEtiquetas = [], dtoMontoCrudo = '', dtoTextoCompleto = '';
                if (cols.length > 7) {
                    const celdaDto = cols[7];
                    dtoEtiquetas = Array.from(celdaDto.querySelectorAll('.el-tag__content'))
                        .map((t) => t.textContent.trim());
                    for (const span of celdaDto.querySelectorAll('span')) {
                        if (!(span.className || '').includes('el-tag')) {
                            dtoMontoCrudo = span.textContent.trim();
                            break;
                        }
                    }
                    dtoTextoCompleto = celdaDto.textContent.trim();
                }
                subpedidos[subpedidos.length - 1].lineas.push({
                    numero_caja: colTxt(0),
                    nombre_producto: nombre,
                    referencia: referencia,
                    codigo_barras_raw: codRaw,
                    presentacion_specs: specs,
                    almacen: colTxt(2),
                    cantidad_comprada_raw: colTxt(3),
                    cantidad_entregada_raw: colTxt(4),
                    tipo: tipoVal,
                    precio_unitario: colTxt(6),
                    descuento_etiquetas: dtoEtiquetas,
                    descuento_monto_crudo: dtoMontoCrudo,
                    descuento_texto_completo: dtoTextoCompleto,
                    precio_descuento: colTxt(8),
                    monto_pagar: colTxt(9),
                    monto_final: colTxt(10),
                    iva: colTxt(11),
                    peso_total: colTxt(12),
                    observaciones: colTxt(13),
                });
            }
        }
    }
    return subpedidos;
}
"""


_PATRON_TOTAL_SUBPEDIDOS = re.compile(r"Total\s+(\d+)\s+subpedidos", re.IGNORECASE)


# ── DEC-087: «Operación de pago» y «Registros de pago» ────────────────────────
#
# Ambas secciones se leen POR NOMBRE, nunca por posición. Es la lección de
# DEC-023, que costó 10.696 pedidos mal poblados: `extraer_info_entrega`
# indexaba columnas por índice y la SPA insertó filas nuevas, así que la hora
# de entrega terminó guardando el nombre del conductor. Una columna nueva del
# origen desplaza índices; no desplaza nombres.

_JS_OPERACION_PAGO = """
() => {
    const card = document.querySelector('.payment-progress-card');
    if (!card) return null;
    const campos = {};
    for (const item of card.querySelectorAll('.pay-info-item')) {
        const l = item.querySelector('.pay-info-label');
        const v = item.querySelector('.pay-info-value');
        if (l && v) campos[l.textContent.trim()] = v.textContent.trim();
    }
    // El estado vive en un el-tag suelto del card, fuera de .pay-info-row.
    const tag = card.querySelector('.el-tag__content');
    return { estado: tag ? tag.textContent.trim() : '', campos: campos };
}
"""

# Etiqueta del origen → columna de `pedidos`. Dict explícito y no derivación
# de nombres: si el origen renombra una etiqueta, el campo queda NULL y el
# WARNING lo dice, en vez de que una heurística lo mapee a otra columna.
_ETIQUETAS_PAGO = {
    "Monto total del pedido": "pago_total",
    "Monto pagado": "pago_pagado",
    "Saldo pendiente": "pago_saldo",
    "Progreso de pago": "pago_progreso",
}

_JS_REGISTROS_PAGO = """
() => {
    const card = document.querySelector('.payment-records-card');
    if (!card) return null;
    const tabla = card.querySelector('.el-table');
    if (!tabla) return [];
    const encabezados = Array.from(
        tabla.querySelectorAll('.el-table__header thead th')
    ).map((th) => {
        const c = th.querySelector('.cell');
        return c ? c.textContent.trim() : '';
    });
    // La tabla intercala filas de SUBTOTAL por envío ('Subtotal del envío 1:'),
    // que el origen marca con `subtotal-row`. Tienen 4 celdas contra las 12 de
    // un comprobante, así que su monto cae en la posición de otra columna: sin
    // este filtro se guardarían como comprobantes con la cuenta receptora
    // pisada por un total.
    const filas = Array.from(
        tabla.querySelectorAll('.el-table__body tbody tr')
    ).filter((fila) => !fila.classList.contains('subtotal-row'));
    return filas.map((fila) => {
        const celdas = Array.from(fila.querySelectorAll('td'));
        const reg = {};
        celdas.forEach((td, i) => {
            const nombre = encabezados[i];
            if (!nombre) return;
            const c = td.querySelector('.cell');
            reg[nombre] = (c ? c.textContent : td.textContent).trim();
        });
        return reg;
    });
}
"""

# La secuencia de un comprobante real es `envío-consecutivo` (`1-1`, `2-3`).
# Las filas de subtotal traen texto en su lugar.
_SECUENCIA_COMPROBANTE = re.compile(r"^\d+-\d+$")

# Encabezado del origen → columna de `registros_pago`.
_COLUMNAS_REGISTRO_PAGO = {
    "Secuencia": "secuencia",
    "Método de pago": "metodo_pago",
    "Cuenta receptora": "cuenta_receptora",
    "Monto del comprobante": "monto_comprobante",
    "Monto de pago": "monto_pago",
    "Hora de pago": "hora_pago",
    "Comprobante": "comprobante",
    "Fecha de envío": "fecha_envio",
    "Estado de revisión": "estado_revision",
    "Fecha de revisión": "fecha_revision",
    "Revisor": "revisor",
    "Observaciones": "observaciones",
}


async def extraer_operacion_pago(page: Page, id_pedido: str) -> dict | None:
    """Extrae la tarjeta «Operación de pago» (DEC-087).

    Trae el estado y el **saldo pendiente calculados por el origen**. Hasta
    ahora había que derivarlos de `estadisticas_monto`, y esa derivación es
    la que dejó tres cifras de cartera sin publicar (DEC-082/084/086).

    La tarjeta apareció en la ola de release del 2026-07-16, así que en
    pedidos viejos puede no renderizar. Eso NO es un error: devuelve `None` y
    el llamador deja las columnas como están.

    Returns:
        Dict con `pago_estado` y las columnas de monto, o `None` si la
        tarjeta no existe en la página.
    """
    try:
        datos = await page.evaluate(_JS_OPERACION_PAGO)
    except Exception as exc:
        log_event(
            "operacion_pago_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
        return None

    if not datos:
        return None

    resultado: dict = {"pago_estado": datos.get("estado", "")}
    campos = datos.get("campos") or {}
    for etiqueta, valor in campos.items():
        columna = _ETIQUETAS_PAGO.get(etiqueta)
        if columna:
            resultado[columna] = valor
        else:
            # Mismo criterio que DEC-032: una etiqueta que no se reconoce se
            # avisa en vez de descartarse en silencio. Así se enteró DEC-033
            # de los campos de crédito.
            log_event(
                "pago_etiqueta_desconocida",
                id_pedido=id_pedido,
                level="WARNING",
                msg=f"Etiqueta nueva en «Operación de pago» — sin capturar: {etiqueta!r}",
            )
    return resultado


async def extraer_registros_pago(page: Page, id_pedido: str) -> list[dict]:
    """Extrae la tabla «Registros de pago» (DEC-087).

    Una fila por comprobante, con banco, cuenta, revisor y estado de
    revisión. `Monto del comprobante` y `Monto de pago` son columnas
    **distintas**: la hipótesis registrada en DEC-087 es que la segunda es lo
    imputado a este pedido y la primera el valor total del comprobante, lo
    que explicaría los pagos de 107 veces el pedido que DEC-084 tuvo que
    apartar. Se guardan las dos para poder falsarlo.

    Returns:
        Lista de dicts lista para insertar; vacía si la tabla no existe o no
        tiene filas.
    """
    try:
        filas = await page.evaluate(_JS_REGISTROS_PAGO)
    except Exception as exc:
        log_event(
            "registros_pago_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
        return []

    if not filas:
        return []

    desconocidos: set[str] = set()
    descartadas = 0
    resultado: list[dict] = []
    for fila in filas:
        # Segunda guarda, deliberadamente redundante con el filtro por clase
        # del JS: la secuencia de un comprobante real tiene forma `envío-N`.
        # El origen cambió 18 veces entre enero y julio (DEC-081), así que un
        # renombre de `subtotal-row` no debe volver a meter totales como si
        # fueran pagos.
        if not _SECUENCIA_COMPROBANTE.match(str(fila.get("Secuencia", "")).strip()):
            descartadas += 1
            continue
        reg: dict = {"id_pedido": id_pedido}
        for encabezado, valor in fila.items():
            columna = _COLUMNAS_REGISTRO_PAGO.get(encabezado)
            if columna:
                reg[columna] = valor
            elif encabezado:
                desconocidos.add(encabezado)
        resultado.append(reg)

    if descartadas:
        log_event(
            "registros_pago_fila_descartada",
            id_pedido=id_pedido,
            level="INFO",
            msg=f"{descartadas} fila(s) sin secuencia de comprobante (subtotales) descartadas",
        )

    if desconocidos:
        log_event(
            "registros_pago_columna_desconocida",
            id_pedido=id_pedido,
            level="WARNING",
            msg=f"Columnas nuevas en «Registros de pago» — sin capturar: {sorted(desconocidos)}",
        )
    return resultado


async def extraer_total_subpedidos(page: Page) -> int | None:
    """Lee el contador de subpedidos que declara el origen (seguimiento DEC-021).

    El DOM expone `<span class="section-count">Total N subpedidos</span>`
    independientemente de si la tabla de subpedidos renderizó. Permite
    discriminar con certeza "0 subpedidos legítimo" (contador en 0) de
    "no renderizó" (contador ausente o no parseable) — antes FIX C-2 no
    podía distinguir los dos casos y optaba por el guard conservador
    ante cualquier duda.

    Nunca lanza excepción: ante cualquier problema retorna None y el
    llamador conserva el comportamiento conservador previo.

    Args:
        page: Página Playwright con el detalle del pedido cargado.

    Returns:
        El total de subpedidos declarado por el origen, o None si el
        contador no está presente o no se pudo interpretar.
    """
    try:
        elemento = await page.query_selector("span.section-count")
        if elemento is None:
            return None
        texto = await elemento.inner_text()
        match = _PATRON_TOTAL_SUBPEDIDOS.search(texto)
        return int(match.group(1)) if match else None
    except Exception:
        return None


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
    # 1 — Expandir subpedidos que aún no están expandidos. Interacción de
    # UI real (click + espera de render) — no se puede batchear.
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
            except Exception as exc:
                log_event(
                    "subpedido_expand_error",
                    level="WARNING",
                    msg=f"td.el-table__expanded-cell no expandió — líneas de este subpedido vacías: {type(exc).__name__}: {exc}",
                )
            await asyncio.sleep(0.5)

    # 2 — Leer filas DESPUÉS de haber expandido todo (DEC-030 Fase 3)
    crudo = await page.evaluate(_JS_SUBPEDIDOS)

    subpedidos: list[dict] = []
    for sp in crudo:
        raw = sp["raw_child_order_id"]
        if " + " in raw:
            partes = raw.split(" + ", 1)
            tipo_sub = partes[0].strip()
            num_sub = partes[1].strip()
        else:
            tipo_sub = "desconocido"
            num_sub = raw

        lineas: list[dict] = []
        for ln in sp["lineas"]:
            cod_barras = ln["codigo_barras_raw"].replace("Código de barras:", "").strip()
            presentac = _unir_presentacion(ln["presentacion_specs"])

            cant_c_str = ln["cantidad_comprada_raw"]
            cant_e_str = ln["cantidad_entregada_raw"]
            cant_c = to_num(cant_c_str)
            cant_e = to_num(cant_e_str)

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

            # DEC-024: la columna descuento combina la ranura del monto y
            # las etiquetas de tipo; se separan en dos campos.
            descuento_val, descuento_tipo = _reclasificar_descuento(
                ln["descuento_etiquetas"],
                ln["descuento_monto_crudo"],
                ln["descuento_texto_completo"],
            )

            lineas.append(
                {
                    "numero_caja": ln["numero_caja"],
                    "nombre_producto": ln["nombre_producto"],
                    "referencia": ln["referencia"],
                    "codigo_barras": cod_barras,
                    "presentacion": presentac,
                    "almacen": ln["almacen"],
                    "cantidad_comprada": cant_c,
                    "cantidad_entregada": cant_e,
                    "tipo": ln["tipo"],
                    "precio_unitario": ln["precio_unitario"],
                    "descuento": descuento_val,
                    "descuento_tipo": descuento_tipo,
                    "precio_descuento": ln["precio_descuento"],
                    "monto_pagar": ln["monto_pagar"],
                    "monto_final": ln["monto_final"],
                    "iva": ln["iva"],
                    "peso_total": ln["peso_total"],
                    "observaciones": ln["observaciones"],
                }
            )

        subpedidos.append(
            {
                "numero_subpedido": num_sub,
                "tipo_subpedido": tipo_sub,
                "estado": sp["estado"],
                "inicio_alistamiento": sp["inicio_alistamiento"],
                "alistamiento_completado": sp["alistamiento_completado"],
                "alistador": sp["alistador"],
                "inicio_inspeccion": sp["inicio_inspeccion"],
                "inspeccion_completada": sp["inspeccion_completada"],
                "inspector": sp["inspector"],
                "lineas": lineas,
            }
        )

    return subpedidos


# DEC-030 Fase 3: la lectura del timeline se colapsa a un solo
# page.evaluate() — antes eran hasta 2 round-trips Playwright↔navegador
# por paso (título + fecha). Sin lógica de negocio en el JS: solo
# recolecta texto/clase crudos, igual que antes.
_JS_TIMELINE = """
() => {
    const wrapper = document.querySelector('div.order-steps-wrapper');
    if (!wrapper) return null;
    return Array.from(wrapper.querySelectorAll('div.step-item')).map((paso) => {
        const t = paso.querySelector('div.step-title');
        const f = paso.querySelector('div.step-time');
        return {
            titulo: t ? t.textContent.trim() : '',
            fecha_hora: f ? f.textContent.trim() : '',
            completado: (paso.className || '').includes('is-completed') ? 1 : 0,
        };
    });
}
"""


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
        pasos = await page.evaluate(_JS_TIMELINE)
        if pasos is None:
            return []
        for i, paso in enumerate(pasos):
            timeline.append(
                {
                    "id_pedido": id_pedido,
                    "paso": i + 1,
                    "titulo": paso["titulo"],
                    "fecha_hora": paso["fecha_hora"],
                    "completado": paso["completado"],
                }
            )
    except Exception as exc:
        log_event(
            "timeline_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return timeline


# ─────────────────────────────────────────────
# OBSERVABILIDAD — DEBUG
# ─────────────────────────────────────────────


def _rotar_archivos(directorio: Path, patron: str, maximo: int) -> None:
    """Elimina los archivos más antiguos hasta quedar por debajo del tope.

    Síncrona a propósito: guardar_debug() la ejecuta via asyncio.to_thread()
    (AUD-B3) para que el glob + stat + unlink no bloqueen el event loop.
    """
    archivos = sorted(directorio.glob(patron), key=lambda f: f.stat().st_mtime)
    while len(archivos) >= maximo:
        archivos.pop(0).unlink()


async def guardar_debug(page: Page, id_pedido: str) -> None:
    """Guarda screenshot PNG y HTML del estado actual para diagnóstico de errores.

    Respeta los límites MAX_SCREENSHOTS y MAX_HTML_DEBUG eliminando los
    archivos más antiguos cuando se superan. El I/O de disco síncrono corre
    en asyncio.to_thread() (AUD-B3).
    """
    # N-5: milisegundos — dos fallos del mismo pedido en el mismo segundo
    # (reintentos con backoff corto) ya no pisan el archivo anterior.
    ts = int(time.time() * 1000)
    errors_dir = Path(CONFIG["ERRORS_DIR"])
    debug_dir = Path(CONFIG["DEBUG_DIR"])
    errors_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    ruta_png = errors_dir / f"error_{id_pedido}_{ts}.png"
    ruta_html = debug_dir / f"debug_{id_pedido}_{ts}.html"

    async with SCREENSHOT_LOCK:
        await asyncio.to_thread(_rotar_archivos, errors_dir, "*.png", CONFIG["MAX_SCREENSHOTS"])
        await page.screenshot(path=str(ruta_png))

    async with HTML_LOCK:
        await asyncio.to_thread(_rotar_archivos, debug_dir, "*.html", CONFIG["MAX_HTML_DEBUG"])
        try:
            html = await page.content()
            await asyncio.to_thread(ruta_html.write_text, html, encoding="utf-8")
        except Exception as exc:
            log_event(
                "html_debug_error",
                id_pedido=id_pedido,
                level="WARNING",
                msg=str(exc),
            )
            ruta_html = None

    log_event(
        "debug_guardado",
        id_pedido=id_pedido,
        msg=(f"screenshot={ruta_png.name}, html={ruta_html.name if ruta_html else 'N/A'}"),
    )
