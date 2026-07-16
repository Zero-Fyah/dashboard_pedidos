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
    ids: list[str] = []
    for fila in await page.query_selector_all(SEL_LISTA_FILAS):
        el = await fila.query_selector(SEL_LISTA_ID)
        if el:
            ids.append((await el.inner_text()).strip())
    return ids


async def obtener_lista_pedidos(
    page: Page,
    fecha_desde: str,
    fecha_hasta: str,
) -> list[str]:
    """Navega a la lista de pedidos, aplica filtro de fechas y extrae todos los IDs.

    Recorre todas las páginas de resultados hasta detectar la última por la
    presencia de la clase 'disabled' o el atributo disabled en el botón
    de siguiente página.

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

    todos_los_ids: list[str] = []
    pagina_actual = 1

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

        log_event(
            "pagina_extraida",
            msg=f"Página {pagina_actual} — {len(ids_pagina)} pedidos",
        )

        btn_next = await page.query_selector("button.btn-next")
        if btn_next:
            aria_disabled = await btn_next.get_attribute("aria-disabled")
            if aria_disabled == "true":
                break
            await btn_next.click()
            # N-2: espera semántica del cambio de página — el número activo
            # del paginador debe ser el siguiente. Si el timeout expira
            # (paginador con clases distintas o render lento) se continúa:
            # la relectura de BUG-016 sigue cubriendo el render tardío.
            try:
                await page.wait_for_function(
                    """([sel, esperada]) => {
                        const activo = document.querySelector(sel);
                        return activo && activo.textContent.trim() === String(esperada);
                    }""",
                    arg=[SEL_PAGER_ACTIVO, pagina_actual + 1],
                    timeout=CONFIG["ELEM_TIMEOUT_MS"],
                )
            except PlaywrightTimeoutError:
                log_event(
                    "paginador_espera_timeout",
                    level="WARNING",
                    msg=(
                        f"El paginador no confirmó el paso a la página "
                        f"{pagina_actual + 1} tras {CONFIG['ELEM_TIMEOUT_MS']}ms"
                    ),
                )
            await asyncio.sleep(CONFIG["PAUSA_PAGINACION_S"])
            pagina_actual += 1
        else:
            break

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
        raise ValueError("id_pedido vacío — la página de detalle no cargó correctamente")

    pid = datos["id_pedido"]
    if len(pid) < 3 or pid.lower() in ("n/a", "null", "-"):
        raise ValueError(f"id_pedido inválido: '{pid}'")

    return datos


async def extraer_info_entrega(page: Page, id_pedido: str) -> dict:
    """Extrae el card 'Información de entrega' (tabla el-descriptions).

    Estructura del HTML:
      Fila 0: Método de entrega (texto + tag ruta opcional + tag descuento)
              | Despachador
      Fila 1: Hora de entrega | Número de estante (se omite)
      Fila 2: Observaciones (colspan 3)

    Returns:
        Dict con 6 claves. Todas vacías si el card no existe.
    """
    resultado = {
        "entrega_metodo_texto": "",
        "entrega_ruta_tag": "",
        "entrega_descuento_tag": "",
        "despachador": "",
        "hora_entrega": "",
        "obs_entrega": "",
    }
    try:
        tabla = await page.query_selector(".el-descriptions__table.is-bordered")
        if not tabla:
            return resultado

        filas = await tabla.query_selector_all("tbody tr")

        if len(filas) > 0:
            celdas = await filas[0].query_selector_all("td")
            if len(celdas) >= 4:
                celda_metodo = celdas[1]

                texto_raw = (await celda_metodo.inner_text()).strip()
                for tag_el in await celda_metodo.query_selector_all(".el-tag"):
                    tag_txt = (await tag_el.inner_text()).strip()
                    texto_raw = texto_raw.replace(tag_txt, "").strip()
                resultado["entrega_metodo_texto"] = texto_raw

                tag_ruta = await celda_metodo.query_selector(".el-tag--primary .el-tag__content")
                if tag_ruta:
                    resultado["entrega_ruta_tag"] = (await tag_ruta.inner_text()).strip()

                tag_dto = await celda_metodo.query_selector(".el-tag--warning .el-tag__content")
                if tag_dto:
                    resultado["entrega_descuento_tag"] = (await tag_dto.inner_text()).strip()

                resultado["despachador"] = (await celdas[3].inner_text()).strip()

        if len(filas) > 1:
            celdas = await filas[1].query_selector_all("td")
            if len(celdas) >= 2:
                resultado["hora_entrega"] = (await celdas[1].inner_text()).strip()

        if len(filas) > 2:
            celdas = await filas[2].query_selector_all("td")
            if len(celdas) >= 2:
                resultado["obs_entrega"] = (await celdas[1].inner_text()).strip()

    except Exception as exc:
        log_event(
            "entrega_error",
            id_pedido=id_pedido,
            level="WARNING",
            msg=str(exc),
        )
    return resultado


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
        card = await page.query_selector(".amount-statistics-card")
        if not card:
            return filas_data, hay_diferencia

        tag_dif = await card.query_selector(
            ".statistics-header .el-tag--warning, .statistics-header .el-tag--dark"
        )
        # FIX C-3: card presente → estado verificado (True o False)
        hay_diferencia = tag_dif is not None

        filas = await card.query_selector_all(".amount-statistics-table tbody tr")
        for orden, fila in enumerate(filas, start=1):
            celdas = await fila.query_selector_all("td")
            if not celdas:
                continue

            celda_concepto = celdas[0]
            concepto_txt = ""
            for span in await celda_concepto.query_selector_all(".cell > span"):
                clases_span = (await span.get_attribute("class")) or ""
                if "el-tag" not in clases_span:
                    concepto_txt = (await span.inner_text()).strip()
                    break
            if not concepto_txt:
                concepto_txt = (await celda_concepto.inner_text()).strip()

            concepto_tag_el = await celda_concepto.query_selector(".el-tag .el-tag__content")
            concepto_tag = (await concepto_tag_el.inner_text()).strip() if concepto_tag_el else ""

            monto_pagar = (await celdas[1].inner_text()).strip() if len(celdas) > 1 else ""
            monto_final = (await celdas[2].inner_text()).strip() if len(celdas) > 2 else ""
            diferencia = (await celdas[3].inner_text()).strip() if len(celdas) > 3 else ""

            filas_data.append(
                {
                    "id_pedido": id_pedido,
                    "orden": orden,
                    "concepto": concepto_txt,
                    "concepto_tag": concepto_tag,
                    "monto_pagar": monto_pagar,
                    "monto_final": monto_final,
                    "diferencia": diferencia,
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
        card = await page.query_selector(".difference-card-wrapper")
        if not card:
            return None

        items = await card.query_selector_all(".difference-content .difference-item")
        valores: list[str] = []
        for item in items:
            val_el = await item.query_selector(".item-value")
            valores.append((await val_el.inner_text()).strip() if val_el else "")

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

        filas = await card.query_selector_all("tbody tr")
        for fila in filas:
            celdas = await fila.query_selector_all("td")
            if len(celdas) < 13:
                continue

            # B023: celdas se liga como default — el cierre se usa solo en
            # esta iteración, pero el binding explícito lo hace inmune a
            # refactors que difieran la llamada.
            async def ct(idx: int, _celdas: list = celdas) -> str:
                return (await _celdas[idx].inner_text()).strip()

            tipo_el = await celdas[2].query_selector(".el-tag__content")
            tipo_val = (await tipo_el.inner_text()).strip() if tipo_el else await ct(2)

            dto_el = await celdas[4].query_selector(".el-tag__content")
            dto_val = (await dto_el.inner_text()).strip() if dto_el else await ct(4)

            resultado.append(
                {
                    "id_pedido": id_pedido,
                    "nombre_producto": await ct(0),
                    "especificacion": await ct(1),
                    "tipo": tipo_val,
                    "precio_unitario": await ct(3),
                    "descuento": dto_val,
                    "precio_descuento": await ct(5),
                    "cantidad_pedido": await ct(6),
                    "cantidad_entregada": await ct(7),
                    "diferencia_cantidad": await ct(8),
                    "monto_pagar_pedido": await ct(9),
                    "monto_final_pagar": await ct(10),
                    "iva": await ct(11),
                    "monto_diferencia": await ct(12),
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
        items = await page.query_selector_all(".operate-log-content .log-item")
        for item in items:
            tiempo_el = await item.query_selector(".log-time")
            usuario_el = await item.query_selector(".log-user")
            contenido_el = await item.query_selector(".log-content")

            momento = (await tiempo_el.inner_text()).strip() if tiempo_el else ""
            usuario = (await usuario_el.inner_text()).strip() if usuario_el else ""

            tipo_usuario = ""
            if usuario_el:
                clases = (await usuario_el.get_attribute("class")) or ""
                if "user-type-member" in clases:
                    tipo_usuario = "member"
                elif "user-type-staff" in clases:
                    tipo_usuario = "staff"
                elif "user-type-system" in clases:
                    tipo_usuario = "system"

            contenido_txt = ""
            if contenido_el:
                contenido_txt = (await contenido_el.inner_text()).strip()
            partes = contenido_txt.split(" - ", 1)
            accion = partes[0].strip()
            referencia = partes[1].strip() if len(partes) > 1 else ""

            resultado.append(
                {
                    "id_pedido": id_pedido,
                    "momento": momento,
                    "usuario": usuario,
                    "tipo_usuario": tipo_usuario,
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
            except Exception as exc:
                log_event(
                    "subpedido_expand_error",
                    level="WARNING",
                    msg=f"td.el-table__expanded-cell no expandió — líneas de este subpedido vacías: {type(exc).__name__}: {exc}",
                )
            await asyncio.sleep(0.5)

    # 2 — Leer filas DESPUÉS de haber expandido todo
    filas = await page.query_selector_all("div.el-scrollbar__wrap--hidden-default table tbody tr")

    subpedidos: list[dict] = []

    for fila in filas:
        # — Fila cabecera de subpedido (contiene celda expand) —
        if await fila.query_selector("td.el-table__expand-column"):
            raw_el = await fila.query_selector("span.child-order-id")
            raw = (await raw_el.inner_text()).strip() if raw_el else ""

            if " + " in raw:
                partes = raw.split(" + ", 1)
                tipo_sub = partes[0].strip()
                num_sub = partes[1].strip()
            else:
                tipo_sub = "desconocido"
                num_sub = raw

            celdas = await fila.query_selector_all("td")
            subpedidos.append(
                {
                    "numero_subpedido": num_sub,
                    "tipo_subpedido": tipo_sub,
                    "estado": await td_txt(celdas, 3, ".el-tag__content"),
                    "inicio_alistamiento": await td_txt(celdas, 4),
                    "alistamiento_completado": await td_txt(celdas, 5),
                    "alistador": await td_txt(celdas, 6),
                    "inicio_inspeccion": await td_txt(celdas, 7),
                    "inspeccion_completada": await td_txt(celdas, 8),
                    "inspector": await td_txt(celdas, 9),
                    "lineas": [],
                }
            )

        # — Fila de contenido expandido —
        elif await fila.query_selector("td.el-table__expanded-cell") and subpedidos:
            for prod_row in await fila.query_selector_all("div.goods-table-row"):
                cols = await prod_row.query_selector_all("div.goods-col")
                info_col = cols[1] if len(cols) > 1 else None

                nombre = await ic_txt(info_col, ".goods-name")
                referencia = await ic_txt(info_col, ".sn-tag")
                cod_raw = await ic_txt(info_col, ".goods-barcode")
                cod_barras = cod_raw.replace("Código de barras:", "").strip()
                presentac = await ic_txt(info_col, ".goods-specs span")

                cant_c_str = await col_text(cols, 3)
                cant_e_str = await col_text(cols, 4)
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

                tipo_el = (
                    await cols[5].query_selector(".el-tag__content") if len(cols) > 5 else None
                )
                tipo_val = (
                    (await tipo_el.inner_text()).strip() if tipo_el else await col_text(cols, 5)
                )

                # La columna descuento (índice 7) tiene dos hijos:
                #   <span>-</span>                  ← valor real (guión o %)
                #   <span class="el-tag">...</span> ← etiqueta "Tipo de cambio3%"
                # inner_text() los concatena con \n → "-\nTipo de cambio3%".
                # Extraemos solo el primer <span> para obtener únicamente el valor.
                descuento_val = ""
                if len(cols) > 7:
                    span_desc = await cols[7].query_selector("span:first-child")
                    if span_desc:
                        descuento_val = (await span_desc.inner_text()).strip()
                    else:
                        descuento_val = await col_text(cols, 7)

                subpedidos[-1]["lineas"].append(
                    {
                        "numero_caja": await col_text(cols, 0),
                        "nombre_producto": nombre,
                        "referencia": referencia,
                        "codigo_barras": cod_barras,
                        "presentacion": presentac,
                        "almacen": await col_text(cols, 2),
                        "cantidad_comprada": cant_c,
                        "cantidad_entregada": cant_e,
                        "tipo": tipo_val,
                        "precio_unitario": await col_text(cols, 6),
                        "descuento": descuento_val,
                        "precio_descuento": await col_text(cols, 8),
                        "monto_pagar": await col_text(cols, 9),
                        "monto_final": await col_text(cols, 10),
                        "iva": await col_text(cols, 11),
                        "peso_total": await col_text(cols, 12),
                        "observaciones": await col_text(cols, 13),
                    }
                )

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
            titulo_el = await paso.query_selector("div.step-title")
            fecha_el = await paso.query_selector("div.step-time")
            clases = (await paso.get_attribute("class")) or ""
            completado = 1 if "is-completed" in clases else 0
            titulo = (await titulo_el.inner_text()).strip() if titulo_el else ""
            fecha_hora = (await fecha_el.inner_text()).strip() if fecha_el else ""
            timeline.append(
                {
                    "id_pedido": id_pedido,
                    "paso": i + 1,
                    "titulo": titulo,
                    "fecha_hora": fecha_hora,
                    "completado": completado,
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
