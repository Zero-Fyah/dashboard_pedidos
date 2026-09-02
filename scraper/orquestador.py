"""
orquestador.py — main(): flujo completo del run (DEC-013).

Prepara DB y browser, arma la lista de IDs (3 carriles en incremental,
rango completo en modo completo), lanza workers + persistencia, corre los
pases dead-letter y emite el resumen final (FIX N-1) con exit code para el
Task Scheduler. Incluye el CLI (build_arg_parser).
"""

import argparse
import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
from playwright.async_api import (
    Browser,
    BrowserContext,
    Route,
    async_playwright,
)

from comun import ESTADOS_CERRADOS, get_db_path
from scraper.config import (
    CLAVE,
    CONFIG,
    USUARIO,
    log_event,
    validar_config,
)
from scraper.db import (
    actualizar_watermark,
    init_db,
    limpiar_errores_resueltos,
)
from scraper.extractores import login, obtener_lista_pedidos_con_watchdog
from scraper.persistencia import persistencia_worker
from scraper.workers import scraper_worker

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


# DEC-029: ninguna extracción lee imágenes de producto (goods-image) —
# bloquearlas a nivel de contexto ahorra la espera al evento 'load'
# completo en modo completo y el ancho de banda compartido entre workers.
# No se bloquea 'font': puede afectar layout/visibilidad y el ahorro es
# marginal frente al peso real (fotos de varios MB).
_RECURSOS_BLOQUEADOS = frozenset({"image", "media"})


async def _bloquear_recursos_pesados(context: BrowserContext) -> None:
    """Aborta descargas de imagen/media en todas las páginas del contexto.

    Se registra una sola vez por contexto (no por página): Playwright
    aplica la ruta a cualquier page.new_page() posterior del mismo context.
    """

    async def _handler(route: Route) -> None:
        if route.request.resource_type in _RECURSOS_BLOQUEADOS:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", _handler)


async def _preparar_contexto_worker(browser: Browser, wid: int) -> BrowserContext:
    """Crea el contexto de un worker y lo deja autenticado, listo para usar.

    Auditoría de rendimiento 2026-08-26: antes se creaban y autenticaban los
    `NUM_WORKERS` contextos uno por uno en un `for` — medido en producción:
    ~31s/ciclo en 6 logins secuenciales de ~5,1-5,3s cada uno. Cada contexto
    es independiente (cookies y almacenamiento propios) y `login()` no toca
    ningún estado compartido entre contextos, así que no hay condición de
    carrera al correrlos concurrentemente — `main()` los lanza con
    `asyncio.gather()`.

    Args:
        browser: Browser de Playwright ya lanzado.
        wid: Índice del worker (0 a NUM_WORKERS-1) — determina qué viewport
            y user agent de `_VIEWPORTS`/`_USER_AGENTS` le tocan.

    Returns:
        El BrowserContext ya autenticado, con la página de login cerrada.
    """
    ctx = await browser.new_context(
        viewport=_VIEWPORTS[wid % len(_VIEWPORTS)],
        user_agent=_USER_AGENTS[wid % len(_USER_AGENTS)],
        locale="es-CO",
    )
    await _bloquear_recursos_pesados(ctx)
    p = await ctx.new_page()
    await login(p, USUARIO, CLAVE)
    await p.close()
    return ctx


# get_db_path() vive en comun/ (AUD-M5) y se importa arriba.


async def obtener_ids_activos(db_path: str) -> list[str]:
    """IDs de pedidos que necesitan una pasada más en el incremental.

    BUG-024: un subpedido necesita atención si está abierto, o si ya cerró
    pero `cantidades_definitivas` sigue en 0 (con_cantidades todavía no lo
    visitó). La versión original solo miraba "abierto" — un pedido cuyos
    subpedidos cierran todos en el mismo incremento (el caso común, no el
    de varios subpedidos cerrando en momentos distintos) se quedaba sin
    ningún subpedido abierto y desaparecía de este carril para siempre,
    sin que con_cantidades llegara a fijar la cantidad definitiva.

    Args:
        db_path: Ruta a la base de datos SQLite.

    Returns:
        Lista de id_pedido distintos que requieren revisión.
    """
    _cerr_ph = ",".join("?" * len(ESTADOS_CERRADOS))
    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                f"""
                SELECT DISTINCT p.id_pedido
                FROM pedidos p
                JOIN subpedidos s ON p.id_pedido = s.id_pedido
                WHERE p.scraping_completo = 1
                  AND (
                    LOWER(s.estado) NOT IN ({_cerr_ph})
                    OR s.cantidades_definitivas = 0
                  )
            """,
                tuple(ESTADOS_CERRADOS),
            )
        ).fetchall()
    return [r[0] for r in rows]


# DEC-092: la tarjeta de entrega se da por NO LEÍDA solo si faltan sus tres
# campos. Un solo campo vacío no alcanza, y las dos corridas del 2026-08-01 lo
# demostraron probando los dos atajos:
#
# - Con `despachador` como testigo: 504 pedidos elegidos, 100% de éxito,
#   **2 recuperados**. Ese campo está al 100% de marzo a junio pero cae al 44%
#   en julio porque **el origen dejó de poblarlo** (lo había marcado el
#   detector `cobertura_de_campos_cayendo`). Confunde "no se leyó la tarjeta"
#   con "el origen ya no lo trae".
# - Con `hora_entrega`: 12 elegidos, 100% de éxito, **0 recuperados**. Los 12
#   tienen despachador y observaciones, o sea que la tarjeta SÍ se leyó y solo
#   les falta la hora — 12 de ~17.800 en la ventana, 0,07%.
#
# Los tres vacíos a la vez es la única señal que no se confunde con un campo
# que el origen dejó de traer ni con un vacío legítimo. Hoy devuelve 0 en
# producción, y eso es correcto: no queda nada recuperable.
_CAMPOS_ENTREGA = ("despachador", "hora_entrega", "obs_entrega")

# Cuántos meses hacia atrás mira la pasada, además del mes recién cerrado.
#
# El Arquitecto la fijó para el día 1 de cada mes, cubriendo el mes anterior
# completo. Ese corte solo no alcanza: **el 20-32% de los pedidos se entrega
# en un mes posterior al de su fecha** (media 9,2 días, máximo 79), así que el
# día 1 una cuarta parte del mes recién cerrado todavía no se entregó. El
# selector los saltaría con razón —no hay tarjeta que leer— y sin el
# retroceso no volverían a mirarse nunca.
#
# Cuatro meses deja ~5 de cobertura total, dentro de los ~6 que el origen
# conserva la tarjeta (DEC-091). Barrer de más no cuesta: el selector filtra
# por "le faltan los tres campos", así que un mes ya completo no aporta IDs.
MANTENIMIENTO_MESES_ATRAS = 4


def ventana_mantenimiento(hoy: date | None = None) -> tuple[str, str]:
    """Rango de fechas de pedido que cubre la pasada de mantenimiento.

    Va desde `MANTENIMIENTO_MESES_ATRAS` meses antes del inicio del mes
    anterior, hasta el último día de ese mes anterior. Corriendo el día 1, el
    mes recién cerrado queda cubierto de punta a punta —que es el requisito—
    y los pedidos que se entregaron tarde entran por el retroceso.

    El borde superior es el fin del mes anterior y no "hoy": los pedidos del
    mes en curso son demasiado nuevos para haberse entregado, y mirarlos sería
    gastar navegador para no encontrar nada.

    Args:
        hoy: Fecha de referencia; por defecto la de hoy. Inyectable para test.

    Returns:
        `(desde, hasta)` en formato YYYY-MM-DD, ambos inclusive.
    """
    hoy = hoy or date.today()
    fin_mes_anterior = hoy.replace(day=1) - timedelta(days=1)
    inicio_mes_anterior = fin_mes_anterior.replace(day=1)
    inicio = inicio_mes_anterior
    for _ in range(MANTENIMIENTO_MESES_ATRAS):
        inicio = (inicio - timedelta(days=1)).replace(day=1)
    return inicio.isoformat(), fin_mes_anterior.isoformat()


async def obtener_ids_para_recuperar(
    db_path: str,
    *,
    desde: str | None = None,
    hasta: str | None = None,
) -> list[str]:
    """IDs de pedidos entregados a los que les falta la información de entrega.

    Es el insumo de `--modo mantenimiento` (DEC-092). El origen deja de
    renderizar esa tarjeta para pedidos viejos, así que existe una ventana
    —desde que el pedido se entrega hasta que el origen lo esconde— y lo que
    no se capture ahí **no se recupera nunca** (DEC-091: 3.517 pedidos ya
    perdidos).

    Cuatro condiciones, cada una descartando un falso positivo distinto:

    1. **Entregado**: un pedido sin entregar tiene la tarjeta vacía con razón.
    2. **Le faltan los tres campos de entrega**: es la única señal de que la
       tarjeta no se leyó. Usar uno solo confunde eso con un campo que el
       origen dejó de traer — se probó y se midió (ver el comentario de
       `_CAMPOS_ENTREGA`).
    3. **Cerrado**: los abiertos ya los cubre el carril de activos.
    4. **Dentro de la ventana**: ni tan nuevo que no se haya entregado, ni tan
       viejo que el origen ya lo haya escondido.

    Args:
        db_path: Ruta a la base de datos SQLite.
        desde: Fecha inicial inclusive; por defecto la de
            `ventana_mantenimiento()`.
        hasta: Fecha final inclusive; por defecto la de
            `ventana_mantenimiento()`.

    Returns:
        Lista de id_pedido a re-extraer en modo completo.
    """
    _defecto = ventana_mantenimiento()
    _desde = desde or _defecto[0]
    _hasta = hasta or _defecto[1]
    _cerr_ph = ",".join("?" * len(ESTADOS_CERRADOS))
    falta = " AND ".join(f"(p.{c} IS NULL OR TRIM(p.{c}) IN ('', '-'))" for c in _CAMPOS_ENTREGA)
    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                f"""
                SELECT DISTINCT p.id_pedido
                  FROM pedidos p
                 WHERE p.fecha BETWEEN ? AND ?
                   AND ({falta})
                   AND EXISTS (SELECT 1 FROM timeline_pedido t
                                WHERE t.id_pedido = p.id_pedido
                                  AND t.titulo = 'Recibido y recibido')
                   AND NOT EXISTS (SELECT 1 FROM subpedidos s
                                    WHERE s.id_pedido = p.id_pedido
                                      AND LOWER(s.estado) NOT IN ({_cerr_ph}))
              ORDER BY p.fecha
            """,  # noqa: S608
                (_desde, _hasta, *tuple(ESTADOS_CERRADOS)),
            )
        ).fetchall()
    return [r[0] for r in rows]


async def marcar_para_recuperacion(db_path: str, ids: list[str]) -> None:
    """Pone `scraping_completo = 0` para que determinar_modo() mande a completo.

    Es el mismo mecanismo que usó el backfill de DEC-027; no se inventa una
    vía nueva para forzar el modo.
    """
    if not ids:
        return
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executemany(
            "UPDATE pedidos SET scraping_completo = 0 WHERE id_pedido = ?",
            [(i,) for i in ids],
        )
        await db.commit()


def calcular_desde_nuevos(
    ultima_ok: str | None,
    *,
    hoy: date | None = None,
) -> str:
    """Calcula la fecha 'desde' del carril 3 del incremental (AUD-M4, DEC-012).

    desde = max(ultima_ok − 1 día, hoy − INCREMENTAL_LOOKBACK_MAX_DIAS).
    Sin watermark (tabla meta vacía) o con valor no parseable, retorna la
    ventana más amplia permitida (hoy − tope) para no perder pedidos por
    una marca ausente o corrupta.

    Args:
        ultima_ok: Valor de meta.ultima_corrida_ok (YYYY-MM-DD), o None.
        hoy: Fecha a usar como "hoy" (inyectable para tests).
             Default: date.today().

    Returns:
        Fecha 'desde' en formato YYYY-MM-DD.
    """
    if hoy is None:
        hoy = date.today()
    piso = hoy - timedelta(days=CONFIG["INCREMENTAL_LOOKBACK_MAX_DIAS"])
    if ultima_ok:
        try:
            marca = date.fromisoformat(ultima_ok)
        except ValueError:
            return piso.isoformat()
        return max(marca - timedelta(days=1), piso).isoformat()
    return piso.isoformat()


def construir_resumen(
    n_ids: int,
    run_stats: dict[str, set[str]],
    t_min: float,
    modo: str,
    carriles: dict[str, int] | None = None,
) -> dict:
    """Construye el resumen final del run a partir de los resultados publicados.

    FIX N-1 (revisión 2026-07-14): el éxito se mide por los resultados del
    propio run (pedidos persistidos con COMMIT exitoso), no por
    scraping_completo en DB — un pedido activo que ya estaba completo no
    cuenta como éxito si su procesamiento falló en este run. Un pedido que
    falló y luego se recuperó en un pase dead-letter cuenta como éxito y
    no como error. Los sets deduplican por construcción (AUD-B2: pedidos
    distintos, no filas de la tabla errores).

    Args:
        n_ids: Total de pedidos encolados en el run.
        run_stats: Sets "ok" y "error" con los ids de pedido según el
                   resultado final de su persistencia en este run.
        t_min: Duración del run en minutos.
        modo: Modo de ejecución ("completo" o "incremental").
        carriles: Conteos por carril incremental ("activos", "errores",
                  "nuevos"), o None si el modo no tiene carriles (HAL-005).

    Returns:
        Dict serializable a JSON con las métricas del run. Los campos de
        carril son None cuando carriles es None (modo completo).
    """
    n_ok = len(run_stats["ok"])
    n_err = len(run_stats["error"] - run_stats["ok"])
    tasa = (n_ok / n_ids * 100) if n_ids else 100.0
    return {
        "tiempo_total_min": round(t_min, 2),
        "modo": modo,
        "pedidos_procesados": n_ok,
        "pedidos_error": n_err,
        "tasa_exito_pct": round(tasa, 2),
        "pedidos_por_minuto": round(n_ids / t_min, 2) if t_min > 0 else 0.0,
        "pedidos_activos": carriles["activos"] if carriles is not None else None,
        "pedidos_error_reintentos": carriles["errores"] if carriles is not None else None,
        "pedidos_nuevos": carriles["nuevos"] if carriles is not None else None,
    }


async def main(args: argparse.Namespace) -> int:
    """Orquesta el scraping completo: DB, lista de IDs, workers y persistencia.

    Flujo incremental (3 procesos, sin recorrer el rango completo):
        1. Pedidos activos  — lee DB: scraping_completo=1 con subpedidos abiertos.
        2. Pedidos con error — lee DB: ids en errores que no están completos/cerrados.
        3. Pedidos nuevos   — consulta servidor desde el watermark y descarta los ya en DB.
    Flujo completo: recorre todas las páginas del rango dado (sin cambios).

    Args:
        args: Namespace de argparse con atributos desde, hasta y modo.

    Returns:
        Exit code del run: 0 si tasa_exito_pct >= 95, 1 si no (AUD-B7:
        el sys.exit vive en __main__, no dentro de la corrutina).
    """
    t_inicio = time.monotonic()
    db_path = get_db_path()

    validar_config()
    await init_db(db_path)
    Path(CONFIG["ERRORS_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(CONFIG["DEBUG_DIR"]).mkdir(parents=True, exist_ok=True)

    eliminados = await limpiar_errores_resueltos(db_path)
    if eliminados:
        log_event(
            "errores_limpios",
            msg=f"Eliminados {eliminados} errores de pedidos ya completos",
        )

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=CONFIG["HEADLESS"],
            slow_mo=CONFIG["SLOW_MO"],
        )

        # ── Obtener lista de IDs ──────────────────────────────────────────
        if args.modo == "incremental":
            # AUD-M8 (auditoría 2026-07-01): estados cerrados parametrizados
            # desde la constante del módulo común — sin literales inline.
            _cerr_ph = ",".join("?" * len(ESTADOS_CERRADOS))

            # Proceso 1 — Actualizar pedidos activos (sin recorrer páginas)
            # BUG-024: ver obtener_ids_activos().
            ids_activos = await obtener_ids_activos(db_path)

            # Proceso 2 — Reintentar pedidos con error
            # BUG-024: mismo criterio de "necesita atención" que Proceso 1,
            # para no excluir de los reintentos un pedido que ya cerró pero
            # aún no tiene cantidades_definitivas.
            async with aiosqlite.connect(db_path) as db_r:
                rows = await (
                    await db_r.execute(
                        f"""
                    SELECT DISTINCT id_pedido FROM errores
                    WHERE id_pedido NOT IN (
                        SELECT id_pedido FROM pedidos
                        WHERE scraping_completo = 1
                          AND id_pedido NOT IN (
                            SELECT DISTINCT id_pedido FROM subpedidos
                            WHERE LOWER(estado) NOT IN ({_cerr_ph})
                               OR cantidades_definitivas = 0
                          )
                    )
                """,
                        tuple(ESTADOS_CERRADOS),
                    )
                ).fetchall()
            ids_error = [r[0] for r in rows]

            # Proceso 3 — Capturar pedidos nuevos. AUD-M4 (DEC-012): la
            # ventana parte del watermark de última corrida OK en vez de la
            # ventana fija ayer-hoy — un outage del scheduler >1 día ya no
            # pierde los pedidos creados en el hueco.
            async with aiosqlite.connect(db_path) as db_r:
                row_w = await (
                    await db_r.execute("SELECT ultima_corrida_ok FROM meta WHERE id = 1")
                ).fetchone()
            fecha_desde_nuevos = calcular_desde_nuevos(row_w[0] if row_w else None)
            fecha_hoy = date.today().isoformat()
            # AUD-M4: hasta dónde cubre este run la captura de nuevos.
            fecha_cobertura = fecha_hoy

            ctx_0 = await browser.new_context(viewport=_VIEWPORTS[0], locale="es-CO")
            await _bloquear_recursos_pesados(ctx_0)
            page_0 = await ctx_0.new_page()
            try:
                await login(page_0, USUARIO, CLAVE)
                ids_nuevos_servidor = await obtener_lista_pedidos_con_watchdog(
                    page_0, fecha_desde_nuevos, fecha_hoy, USUARIO, CLAVE
                )
            finally:
                await page_0.close()
                await ctx_0.close()

            async with aiosqlite.connect(db_path) as db_r:
                rows = await (await db_r.execute("SELECT id_pedido FROM pedidos")).fetchall()
            ids_en_db = {r[0] for r in rows}
            ids_nuevos = [i for i in ids_nuevos_servidor if i not in ids_en_db]

            # Unión final sin duplicados
            ids_pendientes: list[str] = list(dict.fromkeys(ids_activos + ids_error + ids_nuevos))
            # HAL-005: desglose por carril para el resumen JSON final.
            carriles: dict[str, int] | None = {
                "activos": len(ids_activos),
                "errores": len(ids_error),
                "nuevos": len(ids_nuevos),
            }
            log_event(
                "ids_filtrados",
                msg=(
                    f"Activos: {len(ids_activos)} | "
                    f"Errores: {len(ids_error)} | "
                    f"Nuevos: {len(ids_nuevos)} | "
                    f"Ventana nuevos: {fecha_desde_nuevos}..{fecha_hoy} | "
                    f"Total: {len(ids_pendientes)}"
                ),
            )

        elif args.modo == "mantenimiento":
            # DEC-092: no recorre el listado. Los IDs salen de la base —los
            # pedidos entregados a los que les falta la información de
            # entrega— y se marcan para que determinar_modo() los mande a
            # `completo`. Recuperar febrero a mano costó encolar 808 pedidos
            # para arreglar 248; esto encola exactamente los que hacen falta.
            ids_pendientes = await obtener_ids_para_recuperar(db_path)
            await marcar_para_recuperacion(db_path, ids_pendientes)
            carriles = None
            # La pasada no escanea el listado del servidor, así que NO tiene
            # nada que decir sobre la cobertura de pedidos nuevos: mover el
            # watermark acá lo adelantaría sin haber mirado una sola página.
            fecha_cobertura = None
            _v_desde, _v_hasta = ventana_mantenimiento()
            log_event(
                "mantenimiento_seleccionados",
                msg=(
                    f"Pedidos entregados sin información de entrega: "
                    f"{len(ids_pendientes)} | ventana: {_v_desde} .. {_v_hasta}"
                ),
            )

        else:
            # Modo completo — recorre todas las páginas del rango dado
            ctx_0 = await browser.new_context(viewport=_VIEWPORTS[0], locale="es-CO")
            await _bloquear_recursos_pesados(ctx_0)
            page_0 = await ctx_0.new_page()
            try:
                await login(page_0, USUARIO, CLAVE)
                todos_ids = await obtener_lista_pedidos_con_watchdog(
                    page_0, args.desde, args.hasta, USUARIO, CLAVE
                )
            finally:
                await page_0.close()
                await ctx_0.close()

            # AUD-B5: deduplicar como en el incremental — el paginado puede
            # repetir IDs (re-render de Vue entre páginas) y cada duplicado
            # era un pedido procesado dos veces.
            ids_pendientes = list(dict.fromkeys(todos_ids))
            # HAL-005: el modo completo no tiene carriles — campos None
            # en el resumen para distinguirlo de un incremental en ceros.
            carriles = None
            # AUD-M4: en modo completo la cobertura es el rango escaneado,
            # acotada a hoy para nunca adelantar la marca a una fecha futura.
            fecha_cobertura = min(args.hasta, date.today().isoformat())
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
            resumen = construir_resumen(
                0, {"ok": set(), "error": set()}, t_min, args.modo, carriles
            )
            # AUD-M4: el escaneo del servidor terminó OK aunque no haya
            # pendientes — la cobertura de pedidos nuevos avanza igual.
            # DEC-092: salvo en mantenimiento, que no mira el listado y por
            # lo tanto no tiene nada que decir sobre pedidos nuevos.
            if fecha_cobertura is not None:
                marca = await actualizar_watermark(db_path, fecha_cobertura)
                log_event(
                    "watermark_actualizado",
                    msg=(
                        f"meta.ultima_corrida_ok = {marca} | cobertura del run: {fecha_cobertura}"
                    ),
                )
            print("\n>>>RESUMEN<<<")
            print(json.dumps(resumen, indent=4, ensure_ascii=False))
            return 0  # AUD-B7: sin pendientes es un run OK

        # — Paso 3: crear contextos y login de cada worker —
        # Paralelizado (auditoría de rendimiento 2026-08-26): cada login es
        # independiente (contexto propio, sin estado compartido), así que
        # asyncio.gather en vez del for secuencial no cambia el resultado
        # — solo el tiempo de pared. list() preserva el orden por índice:
        # contexts[wid] sigue siendo el contexto del worker wid.
        t_logins_ini = time.monotonic()
        contexts: list[BrowserContext] = list(
            await asyncio.gather(
                *[_preparar_contexto_worker(browser, wid) for wid in range(CONFIG["NUM_WORKERS"])]
            )
        )
        log_event(
            "workers_login_completado",
            duracion_ms=int((time.monotonic() - t_logins_ini) * 1000),
            msg=f"{CONFIG['NUM_WORKERS']} workers autenticados en paralelo",
        )

        # — Paso 4: colas y tasks —
        # FIX C-1 (auditoría 2026-07-01): pedidos_queue sin maxsize. Con cota,
        # _fill() quedaba bloqueado para siempre en put() si todos los workers
        # morían por circuit breaker (deadlock del orquestador). Los IDs son
        # strings: la cota no protegía nada real.
        pedidos_queue: asyncio.Queue = asyncio.Queue()
        resultados_queue: asyncio.Queue = asyncio.Queue()

        # FIX N-1: resultado final por pedido de ESTE run. Compartido entre
        # la persistencia principal y los pases dead-letter: un pedido que
        # falla y luego se recupera queda en ambos sets y el resumen lo
        # cuenta como éxito (error - ok).
        run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}

        persist_task = asyncio.create_task(
            persistencia_worker(resultados_queue, db_path, run_stats)
        )
        worker_tasks = [
            asyncio.create_task(
                scraper_worker(
                    wid,
                    contexts[wid],
                    pedidos_queue,
                    resultados_queue,
                    db_path,
                )
            )
            for wid in range(CONFIG["NUM_WORKERS"])
        ]

        # — Paso 5: llenar la cola concurrentemente con los workers —
        async def _fill() -> None:
            for pid in ids_pendientes:
                await pedidos_queue.put(pid)
            for _ in range(CONFIG["NUM_WORKERS"]):
                await pedidos_queue.put(None)

        fill_task = asyncio.create_task(_fill())
        # FIX C-1: timeout global de red de seguridad — si algo cuelga pese a
        # los timeouts locales, el run cancela los workers y termina con ERROR
        # en el log en vez de quedar congelado bajo el Task Scheduler.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(fill_task, *worker_tasks, return_exceptions=True),
                timeout=CONFIG["RUN_TIMEOUT_S"],
            )
        except asyncio.TimeoutError:
            log_event(
                "run_timeout",
                level="ERROR",
                msg=(
                    f"Timeout global de {CONFIG['RUN_TIMEOUT_S']}s alcanzado — "
                    "cancelando workers y cerrando el run"
                ),
            )
            # wait_for ya canceló el gather; re-esperar las tasks recoge
            # los CancelledError sin relanzarlos.
            results = await asyncio.gather(fill_task, *worker_tasks, return_exceptions=True)
        for wid, res in enumerate(results[1:], start=0):  # [0] es fill_task
            if isinstance(res, BaseException):
                log_event(
                    "worker_exception",
                    level="ERROR",
                    worker_id=wid,
                    msg=repr(res),
                )

        # FIX C-1: si los workers terminaron (circuit breaker o cancelación)
        # dejando IDs sin consumir, registrarlos como error para que el
        # dead-letter de este mismo run o la próxima corrida incremental
        # los reintente. Se drena aquí — nunca dentro del worker — para no
        # robarle pedidos a workers que sigan vivos.
        sin_procesar = 0
        while not pedidos_queue.empty():
            pid_pendiente = pedidos_queue.get_nowait()
            if pid_pendiente is None:
                continue
            sin_procesar += 1
            await resultados_queue.put(
                {
                    "id_pedido": pid_pendiente,
                    "_error": True,
                    "detalle": "No procesado — workers terminados antes de vaciar la cola",
                }
            )
        if sin_procesar:
            log_event(
                "cola_drenada",
                level="WARNING",
                msg=f"{sin_procesar} pedidos sin procesar registrados en errores",
            )
        await resultados_queue.put(None)
        await persist_task

        # — Paso 6: cerrar contextos —
        for ctx in contexts:
            await ctx.close()

        # — Dead-letter: reintentar pedidos con error (hasta 3 pases) —
        # AUD-B4: el tope reducido de reintentos (2) viaja como parámetro
        # hasta procesar_pedido — ya no se muta CONFIG["MAX_REINTENTOS"]
        # globalmente (el try/finally de restauración desapareció con la
        # mutación).
        for _dl_pass in range(1, 4):
            async with aiosqlite.connect(db_path) as _db_dl:
                _rows_dl = await (
                    await _db_dl.execute(
                        """SELECT DISTINCT id_pedido FROM errores
                       WHERE id_pedido NOT IN (
                           SELECT id_pedido FROM pedidos
                           WHERE scraping_completo = 1
                       )"""
                    )
                ).fetchall()
            retry_ids = [r[0] for r in _rows_dl]
            if not retry_ids:
                break
            log_event(
                "dead_letter_pass",
                msg=f"Pase {_dl_pass}: {len(retry_ids)} pedidos a reintentar",
            )
            ctx_dl = await browser.new_context(
                viewport=_VIEWPORTS[0],
                user_agent=_USER_AGENTS[0],
                locale="es-CO",
            )
            await _bloquear_recursos_pesados(ctx_dl)
            _p_dl = await ctx_dl.new_page()
            await login(_p_dl, USUARIO, CLAVE)
            await _p_dl.close()

            dl_pedidos_queue = asyncio.Queue()
            dl_resultados_queue = asyncio.Queue()

            dl_persist = asyncio.create_task(
                persistencia_worker(dl_resultados_queue, db_path, run_stats)
            )
            dl_worker = asyncio.create_task(
                scraper_worker(
                    0,
                    ctx_dl,
                    dl_pedidos_queue,
                    dl_resultados_queue,
                    db_path,
                    max_reintentos=2,
                )
            )

            _snap = list(retry_ids)

            # B023: la cola se liga como default por el mismo motivo que
            # _snap — el cierre vive en una iteración del for de pases.
            async def _dl_fill(
                _ids: list = _snap,
                _cola: asyncio.Queue = dl_pedidos_queue,
            ) -> None:
                for _pid in _ids:
                    await _cola.put(_pid)
                await _cola.put(None)

            await asyncio.gather(_dl_fill(), dl_worker, return_exceptions=True)
            await dl_resultados_queue.put(None)
            await dl_persist
            await ctx_dl.close()

        await browser.close()

    # — Paso 7: resumen final —
    # FIX N-1 (revisión 2026-07-14): las métricas salen de los resultados de
    # ESTE run (run_stats poblado por persistencia_worker), no de consultar
    # scraping_completo en DB. El conteo anterior marcaba como "procesados"
    # a los pedidos activos (que ya tenían scraping_completo=1) aunque su
    # procesamiento hubiera fallado: con la SPA caída, la tasa daba ~100%
    # y el exit code 0 mentía al Task Scheduler. AUD-B2 queda resuelto por
    # construcción: los sets cuentan pedidos distintos, no filas de errores.
    t_min = (time.monotonic() - t_inicio) / 60
    resumen = construir_resumen(len(ids_pendientes), run_stats, t_min, args.modo, carriles)

    log_event(
        "scraper_finalizado",
        msg=(
            f"Completado en {t_min:.1f} min | "
            f"OK={resumen['pedidos_procesados']} | "
            f"ERR={resumen['pedidos_error']} | "
            f"Tasa={resumen['tasa_exito_pct']:.1f}%"
        ),
    )
    # AUD-M4 (DEC-012): solo una corrida OK (tasa ≥ 95%, el mismo umbral del
    # exit code 0) avanza el watermark; un run fallido deja la marca atrás y
    # el hueco se re-escanea en la próxima corrida incremental.
    # DEC-092: mantenimiento deja fecha_cobertura en None — no escaneó el
    # listado, así que avanzar la marca haría creer al incremental que ya
    # miró pedidos nuevos que nunca vio.
    if resumen["tasa_exito_pct"] >= 95.0 and fecha_cobertura is not None:
        marca = await actualizar_watermark(db_path, fecha_cobertura)
        log_event(
            "watermark_actualizado",
            msg=f"meta.ultima_corrida_ok = {marca} | cobertura del run: {fecha_cobertura}",
        )

    print("\n>>>RESUMEN<<<")
    print(json.dumps(resumen, indent=4, ensure_ascii=False))
    # AUD-B7: retornar el código en vez de sys.exit() dentro de la
    # corrutina — SystemExit atravesando el event loop deja recursos de
    # Playwright sin cerrar de forma ordenada y complica los tests.
    return 0 if resumen["tasa_exito_pct"] >= 95.0 else 1


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────


def _fecha_iso(valor: str) -> str:
    """Valida una fecha CLI en formato estricto YYYY-MM-DD (N-5).

    Sin validación, un typo como '2026-5-1' o '01-05-2026' viajaba intacto
    hasta el filtro de la SPA y producía un listado vacío silencioso.

    Args:
        valor: String recibido en --desde/--hasta.

    Returns:
        El mismo string, si es una fecha de calendario válida.

    Raises:
        argparse.ArgumentTypeError: Formato o fecha inválidos (argparse lo
                                    convierte en error de uso con exit 2).
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor):
        raise argparse.ArgumentTypeError(
            f"fecha inválida: '{valor}' — formato requerido YYYY-MM-DD"
        )
    try:
        date.fromisoformat(valor)
    except ValueError:
        # B904: from None — la traducción a error de uso es deliberada,
        # el ValueError interno no aporta al usuario del CLI.
        raise argparse.ArgumentTypeError(
            f"fecha inválida: '{valor}' — no es una fecha de calendario real"
        ) from None
    return valor


def build_arg_parser() -> argparse.ArgumentParser:
    """Construye y retorna el parser de argumentos.

    Returns:
        ArgumentParser configurado con --desde,
        --hasta y --modo (fechas validadas con _fecha_iso, N-5).
    """
    parser = argparse.ArgumentParser(
        description="Scraper de pedidos — sistema administrativo interno"
    )
    parser.add_argument(
        "--desde",
        default="2026-01-01",
        type=_fecha_iso,
        help="Fecha inicio YYYY-MM-DD",
    )
    parser.add_argument(
        "--hasta",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        type=_fecha_iso,
        help="Fecha fin YYYY-MM-DD",
    )
    parser.add_argument(
        "--modo",
        choices=["completo", "incremental", "mantenimiento"],
        default="completo",
        help=(
            "completo: encola todos los IDs | incremental: omite "
            "scraping_completo=1 | mantenimiento: re-extrae los pedidos "
            "entregados a los que les falta la información de entrega, antes "
            "de que el origen deje de mostrarla (DEC-092)"
        ),
    )
    return parser
