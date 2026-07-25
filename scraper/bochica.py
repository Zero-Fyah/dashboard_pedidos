"""
bochica.py — Login y descarga de inventario del sistema de bodega BOCHICA
(Google Apps Script).

Segundo sistema fuente de DEC-039. Login de Google Workspace + login
propio de la app, independientes del admin (dominios, credenciales y
selectores propios) — vive en su propio módulo por SRP (DEC-013).
"""

import json
import time
from pathlib import Path

from playwright.async_api import Download, Frame, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper.config import CONFIG, log_event

DESTINO_DEFAULT = Path(__file__).parent.parent / "data" / "inventario" / "bochica_inventario.xlsx"


async def login_bochica(page: Page, usuario: str, clave: str) -> None:
    """Autentica en BOCHICA vía el flujo estándar de Google Workspace.

    Dos pantallas secuenciales (correo, luego contraseña) — a diferencia
    del login del admin, los campos no coexisten en la misma pantalla.

    Args:
        page: Página Playwright sobre la que operar.
        usuario: Correo de la cuenta de Google Workspace (BOCHICA_USUARIO).
        clave: Contraseña de la cuenta (BOCHICA_PASSWORD).
    """
    await page.goto(CONFIG["bochica_url"], timeout=CONFIG["NAV_TIMEOUT_MS"])

    await page.locator("input[type='email']").first.fill(usuario)
    await page.get_by_role("button", name="Siguiente").click()

    await page.wait_for_selector("input[type='password']", timeout=CONFIG["ELEM_TIMEOUT_MS"])
    await page.locator("input[type='password']").first.fill(clave)
    await page.get_by_role("button", name="Siguiente").click()

    await page.wait_for_function(
        f"() => window.location.href.startsWith({json.dumps(CONFIG['bochica_url'])})",
        timeout=CONFIG["NAV_TIMEOUT_MS"],
    )
    log_event("login_bochica_ok", msg="Autenticación exitosa en Bochica (Google)")


async def _frame_con_selector(page: Page, selector: str, timeout_ms: int) -> Frame:
    """Busca, entre todos los frames de la página, el primero que contenga `selector`.

    Bochica (Google Apps Script, modo sandbox) renderiza su contenido real
    dentro de un iframe anidado (`userHtmlFrame`) cuyo nombre se repite
    entre sub-frames embebidos no relacionados (ej. un diálogo OAuth) —
    encadenar `frame_locator` por nombre resuelve al frame equivocado o
    a ninguno. Buscar por contenido en todos los `page.frames` es lo único
    que funcionó de forma confiable contra la app real.

    Args:
        page: Página Playwright.
        selector: Selector CSS a buscar dentro de cada frame.
        timeout_ms: Tiempo máximo de espera en milisegundos.

    Raises:
        PlaywrightTimeoutError: Si ningún frame contiene el selector a tiempo.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for f in page.frames:
            try:
                if await f.locator(selector).count() > 0:
                    return f
            except Exception:  # noqa: BLE001 — frame puede desprenderse a mitad de chequeo
                continue
        await page.wait_for_timeout(500)
    raise PlaywrightTimeoutError(f"Ningún frame contiene el selector {selector!r}")


async def login_app_bochica(page: Page, usuario: str, clave: str) -> None:
    """Autentica en la segunda capa de login de la propia app de Bochica.

    Distinta del login de Google: es un usuario/clave propios de la app
    (el "usuario de miempresa"). Tras un login válido aparece un selector
    de país (Colombia/Chile) antes de que se muestre #appContent — el
    proyecto solo opera Colombia (btnPais_CO), Chile queda fuera de alcance.
    Asume que `page` ya pasó por login_bochica().

    Args:
        page: Página Playwright con sesión de Google ya autenticada.
        usuario: Usuario de la app (BOCHICA_APP_USUARIO).
        clave: Contraseña de ese usuario (BOCHICA_APP_PASSWORD).
    """
    frame = await _frame_con_selector(page, "#loginEmail", CONFIG["ELEM_TIMEOUT_MS"])
    await frame.locator("#loginEmail").fill(usuario)
    await frame.locator("#loginPassword").fill(clave)
    await frame.locator("#loginButton").click()

    await frame.locator("#btnPais_CO").click(timeout=CONFIG["ELEM_TIMEOUT_MS"])

    await frame.locator("#appContent").wait_for(state="visible", timeout=CONFIG["ELEM_TIMEOUT_MS"])
    log_event("login_bochica_app_ok", msg="Autenticación exitosa en Bochica (app, país CO)")


async def descargar_inventario_global(page: Page, destino: Path = DESTINO_DEFAULT) -> Path:
    """Descarga el Excel de inventario global desde el módulo "Inventario General".

    Asume que `page` ya pasó por login_bochica() y login_app_bochica().
    Abre el menú lateral si está colapsado, navega a "Inventario General"
    (mod-auditoria), genera el inventario y descarga el Excel resultante.

    Args:
        page: Página Playwright con las dos sesiones (Google + app) activas.
        destino: Ruta donde guardar el .xlsx. Se sobreescribe si ya existe.

    Returns:
        La misma ruta `destino`, tras confirmar que el archivo se guardó.
    """
    frame = await _frame_con_selector(page, "#appContent", CONFIG["ELEM_TIMEOUT_MS"])

    # El sidebar se desliza fuera del viewport con CSS (transform), no con
    # display/visibility — is_visible() no lo detecta como oculto y el click
    # posterior falla con "element is outside of the viewport". La clase
    # "open" en #sidebar es la señal real de que está desplegado.
    sidebar_class = await frame.locator("#sidebar").get_attribute("class") or ""
    if "open" not in sidebar_class:
        await frame.locator("button.hamburger").click()

    nav_item = frame.locator("button.nav-item[data-module='mod-auditoria']")
    await nav_item.click(timeout=CONFIG["ELEM_TIMEOUT_MS"])

    await frame.locator("#btnGenerarInv").click()
    # Medido contra la app real (2026-07-23): ~111s para generar ~21.300
    # filas. LISTADO_TIMEOUT_S (600s, mismo criterio que el listado de
    # pedidos del admin) deja margen amplio — espera semántica a la tabla
    # real, no networkidle ni un sleep fijo.
    await frame.locator("#inventarioGlobalContainer table").wait_for(
        timeout=CONFIG["LISTADO_TIMEOUT_S"] * 1000
    )

    async with page.expect_download(timeout=CONFIG["NAV_TIMEOUT_MS"]) as descarga_info:
        await frame.locator("button[onclick='descargarInventarioGlobal()']").click()
    descarga: Download = await descarga_info.value

    destino.parent.mkdir(parents=True, exist_ok=True)
    await descarga.save_as(destino)

    log_event("inventario_bochica_descargado", msg=f"Guardado en {destino}")
    return destino


if __name__ == "__main__":
    import asyncio

    from playwright.async_api import async_playwright

    async def _main() -> None:
        faltantes = [
            k
            for k in (
                "bochica_url",
                "bochica_usuario",
                "bochica_clave",
                "bochica_app_usuario",
                "bochica_app_clave",
            )
            if not CONFIG[k]
        ]
        if faltantes:
            raise RuntimeError(
                f"Variables de entorno faltantes para Bochica: {', '.join(faltantes)}."
            )
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CONFIG["HEADLESS"],
                slow_mo=CONFIG["SLOW_MO"],
            )
            ctx = await browser.new_context(locale="es-CO")
            page = await ctx.new_page()
            await login_bochica(page, CONFIG["bochica_usuario"], CONFIG["bochica_clave"])
            await login_app_bochica(
                page, CONFIG["bochica_app_usuario"], CONFIG["bochica_app_clave"]
            )
            ruta = await descargar_inventario_global(page)
            print(f"Inventario de Bochica descargado en: {ruta}")
            await browser.close()

    asyncio.run(_main())
