"""
workers.py — Selección de modo, scraping de pedido y worker con circuit
breaker (DEC-013).

scraper_worker() consume IDs de la cola; procesar_pedido() determina el
modo (completo / con_cantidades / solo_estado), extrae y publica en la
cola de resultados.
"""

import asyncio
import random
import time

import aiosqlite
from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from comun import ESTADOS_CERRADOS
from scraper.config import (
    CLAVE,
    CONFIG,
    LOGIN_LOCK,
    USUARIO,
    log_event,
    rate_limit_pendiente,
    registrar_rate_limit,
)
from scraper.extractores import (
    extraer_detalle_diferencias,
    extraer_estadisticas_monto,
    extraer_gestion_diferencias,
    extraer_info_entrega,
    extraer_info_general,
    extraer_operacion_pago,
    extraer_registro_operaciones,
    extraer_registros_pago,
    extraer_subpedidos,
    extraer_timeline,
    extraer_total_subpedidos,
    guardar_debug,
    login,
)

# ─────────────────────────────────────────────
# SELECCIÓN DE MODO
# ─────────────────────────────────────────────


def determinar_modo(
    es_nuevo: bool,
    subs_db: list[tuple[str, int]],
    scraping_completo: int = 1,
) -> str:
    """Determina el modo de extracción sin I/O.

    Args:
        es_nuevo: True si el pedido no existe en DB.
        subs_db: Lista de (estado, cantidades_definitivas)
                 de los subpedidos del pedido.
        scraping_completo: Valor de la columna scraping_completo en pedidos.
                           0 indica que la migración requiere re-extracción
                           completa aunque el pedido ya exista en DB.
                           Default 1 preserva el comportamiento previo.

    Returns:
        "completo"        — pedido nuevo o scraping_completo=0 (migración).
        "con_cantidades"  — subpedido en estado cerrado
                           con cantidades aún no registradas.
        "solo_estado"     — actualizar solo estado.
    """
    if scraping_completo == 0:
        return "completo"
    if es_nuevo:
        return "completo"
    # AUD-M9: (estado or "") — un estado NULL en un registro legado no debe
    # tumbar determinar_modo() con AttributeError; "" nunca matchea
    # ESTADOS_CERRADOS, así que el pedido cae a solo_estado, correcto.
    if any(cd == 0 and (estado or "").lower() in ESTADOS_CERRADOS for estado, cd in subs_db):
        return "con_cantidades"
    return "solo_estado"


# ─────────────────────────────────────────────
# SCRAPING — PEDIDO INDIVIDUAL
# ─────────────────────────────────────────────


async def procesar_pedido(
    worker_id: int,
    page: Page,
    id_pedido: str,
    resultados_queue: asyncio.Queue,
    db_path: str,
    max_reintentos: int | None = None,
) -> bool:
    """Determina el modo de extracción, navega al detalle y publica en la cola.

    Consulta la DB antes de navegar para elegir uno de tres modos:
      - completo:       pedido nuevo; extrae todo (info general, subpedidos, timeline).
      - con_cantidades: algún subpedido fija cantidades; actualiza estado y
                        cantidad_entregada + reemplaza timeline.
      - solo_estado:    solo actualiza estado de cada subpedido y timeline;
                        sin expansión, el más rápido.

    Reintenta hasta max_reintentos veces con backoff exponencial y jitter.
    Toma screenshot en cada fallo.

    Args:
        worker_id: ID del worker que invoca esta función.
        page: Página Playwright activa del worker.
        id_pedido: ID del pedido a procesar.
        resultados_queue: Cola donde publicar el resultado o el registro de error.
        db_path: Ruta al archivo SQLite para consultar el estado previo.
        max_reintentos: Tope de intentos. None usa CONFIG["MAX_REINTENTOS"]
                        (AUD-B4: el dead-letter pasa 2 en vez de mutar CONFIG).

    Returns:
        True si el pedido fue extraído y publicado con éxito, False si no.
    """
    t_inicio = time.monotonic()
    if max_reintentos is None:
        max_reintentos = CONFIG["MAX_REINTENTOS"]

    # ── Determinar modo antes del loop de reintentos ──────────────────────
    # AUD-M9: esta sección vivía fuera del try del loop de reintentos — un
    # OperationalError (DB bloqueada) u otra excepción acá mataba la task
    # del worker sin dejar rastro en `errores` ni en el log hasta el
    # resumen final del run. Mismo contrato de salida que el loop: log
    # ERROR + resultado "_error" en la cola + return False.
    try:
        async with aiosqlite.connect(db_path) as db_r:
            row = await (
                await db_r.execute(
                    "SELECT scraping_completo FROM pedidos WHERE id_pedido = ?", (id_pedido,)
                )
            ).fetchone()
            es_nuevo = row is None

            if not es_nuevo:
                subs_db = await (
                    await db_r.execute(
                        "SELECT estado, cantidades_definitivas FROM subpedidos WHERE id_pedido = ?",
                        (id_pedido,),
                    )
                ).fetchall()
            else:
                subs_db = []

        scraping_completo = row[0] if row is not None else 1
        modo = determinar_modo(es_nuevo, subs_db, scraping_completo)
    except Exception as exc:
        detalle = f"Determinación de modo falló: {exc}"
        log_event(
            "pedido_error",
            level="ERROR",
            worker_id=worker_id,
            id_pedido=id_pedido,
            msg=detalle,
        )
        await resultados_queue.put({"id_pedido": id_pedido, "_error": True, "detalle": detalle})
        return False

    # DEC-029: domcontentloaded unificado en los tres modos (antes solo
    # con_cantidades/solo_estado lo usaban, N-2) — el modo completo ya no
    # espera al evento 'load' completo (imágenes incluidas, DEC-029).
    nav_kwargs: dict = {"wait_until": "domcontentloaded"}

    for intento in range(1, max_reintentos + 1):
        try:
            t_nav_ini = time.monotonic()
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
            nav_ms = int((time.monotonic() - t_nav_ini) * 1000)
            # DEC-030 Fase 0: t_render_ini/t_extract_ini separan cuánto cuesta
            # esperar el render de Vue vs. extraer el DOM ya renderizado — los
            # dos componentes que nav_ms (DEC-029) demostró que dominan el
            # tiempo total, sin poder distinguirlos hasta ahora.
            t_render_ini = time.monotonic()

            if modo == "completo":
                await page.wait_for_selector("div.info-item", timeout=CONFIG["ELEM_TIMEOUT_MS"])
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    await page.wait_for_selector(
                        "div.order-steps-wrapper",
                        state="visible",
                        timeout=CONFIG["ELEM_TIMEOUT_MS"],
                    )
                except PlaywrightTimeoutError:
                    # El selector no apareció en el tiempo esperado.
                    # La extracción retornará [] — los Cambios 2/3 lo manejan.
                    pass
                render_ms = int((time.monotonic() - t_render_ini) * 1000)
                t_extract_ini = time.monotonic()

                info_general = await extraer_info_general(page)
                subpedidos = await extraer_subpedidos(page)
                # Seguimiento DEC-021: contador del origen para que FIX C-2
                # discrimine "0 subpedidos legítimo" de "no renderizó".
                total_subpedidos_origen = await extraer_total_subpedidos(page)
                timeline = await extraer_timeline(page, id_pedido)
                info_entrega = await extraer_info_entrega(page, id_pedido)
                estadisticas, hay_dif = await extraer_estadisticas_monto(page, id_pedido)
                gestion_dif = await extraer_gestion_diferencias(page, id_pedido)
                detalle_dif = await extraer_detalle_diferencias(page, id_pedido)
                registro_ops = await extraer_registro_operaciones(page, id_pedido)
                # DEC-087: secciones nuevas del origen (2026-07-16). Devuelven
                # None/[] si no renderizan, que es lo esperado en un pedido
                # anterior a esa fecha — la persistencia entonces no pisa nada.
                operacion_pago = await extraer_operacion_pago(page, id_pedido)
                registros_pago = await extraer_registros_pago(page, id_pedido)
                resultado = {
                    "tipo": "completo",
                    "id_pedido": id_pedido,
                    "info_general": info_general,
                    "subpedidos": subpedidos,
                    "total_subpedidos_origen": total_subpedidos_origen,
                    "timeline": timeline,
                    "info_entrega": info_entrega,
                    "estadisticas": estadisticas,
                    # FIX C-3: None se propaga como "no verificado"
                    "hay_diferencia": None if hay_dif is None else (1 if hay_dif else 0),
                    "gestion_dif": gestion_dif,
                    "detalle_dif": detalle_dif,
                    "registro_ops": registro_ops,
                    "operacion_pago": operacion_pago,
                    "registros_pago": registros_pago,
                }
                n_subs = len(subpedidos)

            elif modo == "con_cantidades":
                # AUD-M3 (auditoría 2026-07-01): esperar el renderizado de la
                # tabla de subpedidos igual que solo_estado. Sin esta espera,
                # extraer sobre DOM no renderizado retornaba [] y el pedido se
                # reportaba pedido_ok sin capturar cantidades (actualización
                # fantasma que además falsificaba "Días sin mov.").
                try:
                    await page.wait_for_selector(
                        "div.el-scrollbar__wrap--hidden-default table tbody tr",
                        state="visible",
                        timeout=CONFIG["ELEM_TIMEOUT_MS"],
                    )
                except PlaywrightTimeoutError:
                    log_event(
                        "con_cantidades_tabla_timeout",
                        level="WARNING",
                        worker_id=worker_id,
                        id_pedido=id_pedido,
                        msg="Tabla de subpedidos no renderizada — cantidades no actualizadas en esta pasada",
                    )
                render_ms = int((time.monotonic() - t_render_ini) * 1000)
                t_extract_ini = time.monotonic()
                subpedidos = await extraer_subpedidos(page)
                timeline = await extraer_timeline(page, id_pedido)
                info_entrega = await extraer_info_entrega(page, id_pedido)
                estadisticas, hay_dif = await extraer_estadisticas_monto(page, id_pedido)
                gestion_dif = await extraer_gestion_diferencias(page, id_pedido)
                detalle_dif = await extraer_detalle_diferencias(page, id_pedido)
                registro_ops = await extraer_registro_operaciones(page, id_pedido)
                resultado = {
                    "tipo": "con_cantidades",
                    "id_pedido": id_pedido,
                    "subpedidos": subpedidos,
                    "timeline": timeline,
                    "info_entrega": info_entrega,
                    "estadisticas": estadisticas,
                    # FIX C-3: None se propaga como "no verificado"
                    "hay_diferencia": None if hay_dif is None else (1 if hay_dif else 0),
                    "gestion_dif": gestion_dif,
                    "detalle_dif": detalle_dif,
                    "registro_ops": registro_ops,
                }
                n_subs = len(subpedidos)

            else:  # solo_estado
                try:
                    await page.wait_for_selector(
                        "div.el-scrollbar__wrap--hidden-default table tbody tr",
                        state="visible",
                        timeout=CONFIG["ELEM_TIMEOUT_MS"],
                    )
                except PlaywrightTimeoutError:
                    log_event(
                        "solo_estado_tabla_timeout",
                        level="WARNING",
                        worker_id=worker_id,
                        id_pedido=id_pedido,
                        msg="Tabla de subpedidos no renderizada — estados no actualizados en esta pasada",
                    )
                render_ms = int((time.monotonic() - t_render_ini) * 1000)
                t_extract_ini = time.monotonic()
                filas = await page.query_selector_all(
                    "div.el-scrollbar__wrap--hidden-default table tbody tr"
                )
                subs_estado: list[dict] = []
                for fila in filas:
                    if not await fila.query_selector("td.el-table__expand-column"):
                        continue
                    raw_el = await fila.query_selector("span.child-order-id")
                    raw = (await raw_el.inner_text()).strip() if raw_el else ""
                    num_sub = raw.split(" + ", 1)[1].strip() if " + " in raw else raw
                    celdas = await fila.query_selector_all("td")
                    if len(celdas) > 3:
                        estado_el = await celdas[3].query_selector(".el-tag__content")
                        estado = (await estado_el.inner_text()).strip() if estado_el else ""
                    else:
                        estado = ""
                    subs_estado.append({"numero_subpedido": num_sub, "estado": estado})

                # DEC-030 Fase 2 (fix, no optimización a negociar — integral.md
                # ya documentaba "fase activa: solo se actualiza su estado";
                # timeline y registro_ops son la excepción confirmada por el
                # Arquitecto: se consideran parte de "el estado" y sí se
                # actualizan en cada ciclo). estadísticas/gestión/detalle de
                # diferencias solo tienen sentido con cantidades definitivas
                # — se capturan al cerrar cada subpedido (con_cantidades), no
                # aquí. Antes, solo_estado las volvía a leer y persistir en
                # cada ciclo incremental sin que el diseño lo pidiera.
                timeline = await extraer_timeline(page, id_pedido)
                registro_ops = await extraer_registro_operaciones(page, id_pedido)
                resultado = {
                    "tipo": "solo_estado",
                    "id_pedido": id_pedido,
                    "subpedidos": subs_estado,
                    "timeline": timeline,
                    "info_entrega": None,
                    "estadisticas": [],
                    # No se verifica esta pasada — FIX C-3: None = "no verificado".
                    "hay_diferencia": None,
                    "gestion_dif": None,
                    "detalle_dif": None,
                    "registro_ops": registro_ops,
                }
                n_subs = len(subs_estado)

            duracion_ms = int((time.monotonic() - t_inicio) * 1000)
            extract_ms = int((time.monotonic() - t_extract_ini) * 1000)
            await resultados_queue.put(resultado)
            log_event(
                "pedido_ok",
                worker_id=worker_id,
                id_pedido=id_pedido,
                duracion_ms=duracion_ms,
                msg=(
                    f"modo={modo} | {n_subs} subpedidos | intento {intento} | "
                    f"nav_ms={nav_ms} | render_ms={render_ms} | extract_ms={extract_ms}"
                ),
            )
            await asyncio.sleep(CONFIG["PAUSA_ENTRE_PEDIDOS_S"])
            return True

        except Exception as exc:
            log_event(
                "pedido_error",
                level="WARNING",
                worker_id=worker_id,
                id_pedido=id_pedido,
                msg=f"Intento {intento}/{max_reintentos}: {exc}",
            )
            try:
                await guardar_debug(page, id_pedido)
            except Exception as ss_exc:
                log_event(
                    "screenshot_error",
                    level="WARNING",
                    worker_id=worker_id,
                    id_pedido=id_pedido,
                    msg=str(ss_exc),
                )
            if intento < max_reintentos:
                backoff = min(
                    CONFIG["BACKOFF_BASE_S"] ** intento + random.uniform(0, 1),
                    CONFIG["BACKOFF_MAX_S"],
                )
                await asyncio.sleep(backoff)

    detalle = f"Falló tras {max_reintentos} intentos"
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
# WORKER DE SCRAPING
# ─────────────────────────────────────────────


async def scraper_worker(
    worker_id: int,
    context: BrowserContext,
    pedidos_queue: asyncio.Queue,
    resultados_queue: asyncio.Queue,
    db_path: str,
    max_reintentos: int | None = None,
) -> None:
    """Consume IDs de pedido de la cola y los procesa uno a uno.

    Mantiene un circuit breaker local: si hay CONFIG["CIRCUIT_FAILURE_THRESHOLD"]
    fallos consecutivos pausa CONFIG["CIRCUIT_COOLDOWN_S"] segundos. Si se
    superan CONFIG["CIRCUIT_MAX_REOPENINGS"] reaperturas el worker termina.

    El handler de rate limiting (HTTP 429) se define una vez y se reutiliza en
    todas las páginas del worker mediante una referencia mutable al pedido actual.
    El handler solo señaliza via registrar_rate_limit() (AUD-M2); la pausa real
    la ejecuta este loop consultando rate_limit_pendiente() antes de cada pedido.

    Args:
        worker_id: Identificador único (0 a NUM_WORKERS-1).
        context: BrowserContext independiente de Playwright.
        pedidos_queue: Cola compartida con IDs de pedido.
        resultados_queue: Cola donde publicar los resultados extraídos.
        db_path: Ruta al archivo SQLite, pasado a procesar_pedido.
        max_reintentos: Tope de intentos por pedido, pasado a
                        procesar_pedido (AUD-B4). None usa el de CONFIG.
    """
    consecutive_failures = 0
    circuit_reopenings = 0
    current_pedido: list[str] = [""]

    async def _response_handler(response: Response) -> None:
        # AUD-M2: no dormir aquí — el sleep en un handler fire-and-forget
        # no pausa al worker. Solo registrar la señal compartida.
        if response.status != 429:
            return
        wait_s = registrar_rate_limit(response.headers.get("retry-after", ""))
        log_event(
            "rate_limited",
            worker_id=worker_id,
            id_pedido=current_pedido[0],
            msg=f"HTTP 429 — pausa de {wait_s:.0f}s señalizada para los workers",
        )

    while True:
        id_pedido = await pedidos_queue.get()
        if id_pedido is None:
            break

        # AUD-M2: la pausa real por rate limit ocurre aquí, en el loop del
        # worker. Se re-consulta tras dormir porque otra respuesta 429
        # puede haber extendido la ventana mientras se esperaba.
        while (espera_rl := rate_limit_pendiente()) > 0:
            log_event(
                "rate_limit_espera",
                worker_id=worker_id,
                id_pedido=id_pedido,
                msg=f"Pausando {espera_rl:.0f}s por rate limit activo",
            )
            await asyncio.sleep(espera_rl)

        current_pedido[0] = id_pedido
        page = await context.new_page()
        page.on("response", _response_handler)

        try:
            exito = await procesar_pedido(
                worker_id,
                page,
                id_pedido,
                resultados_queue,
                db_path,
                max_reintentos=max_reintentos,
            )
        except Exception as exc:
            # AUD-M9: red de seguridad final — procesar_pedido() ya cubre su
            # propio contrato de salida (True/False + resultado en la cola),
            # pero cualquier excepción que igual se escape (bug futuro, caso
            # no previsto) no debe matar la task del worker: sin este except
            # el worker dejaba de consumir la cola para siempre, sin log
            # visible hasta el resumen final del run.
            log_event(
                "worker_excepcion_no_controlada",
                level="ERROR",
                worker_id=worker_id,
                id_pedido=id_pedido,
                msg=f"Excepción no controlada procesando el pedido — el worker sigue vivo: {exc}",
            )
            await resultados_queue.put(
                {"id_pedido": id_pedido, "_error": True, "detalle": str(exc)}
            )
            exito = False
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
