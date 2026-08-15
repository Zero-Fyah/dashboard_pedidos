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
SESION_DEFAULT = Path(__file__).parent.parent / "data" / "sesiones" / "bochica_storage_state.json"


async def login_bochica(page: Page, usuario: str, clave: str) -> None:
    """Autentica en BOCHICA vía el flujo estándar de Google Workspace.

    Dos pantallas secuenciales (correo, luego contraseña) — a diferencia
    del login del admin, los campos no coexisten en la misma pantalla.

    Envía cada pantalla con Enter en vez de hacer clic en el botón "Siguiente"
    por nombre: Google no fija el idioma de forma consistente entre sesiones
    de un navegador automatizado (contexto nuevo sin idioma guardado en cada
    corrida), así que el texto del botón varía entre "Siguiente" y "Next" —
    Enter es independiente del idioma. También se agregó `:visible` al
    selector de contraseña porque esa pantalla puede traer un campo
    `type='password'` oculto (señuelo anti-autofill de Google) antes del
    real (hallazgo en vivo, 2026-08-14).

    Args:
        page: Página Playwright sobre la que operar.
        usuario: Correo de la cuenta de Google Workspace (BOCHICA_USUARIO).
        clave: Contraseña de la cuenta (BOCHICA_PASSWORD).
    """
    await page.goto(CONFIG["bochica_url"], timeout=CONFIG["NAV_TIMEOUT_MS"])

    await page.locator("input[type='email']").first.fill(usuario)
    await page.keyboard.press("Enter")

    await page.wait_for_selector(
        "input[type='password']:visible", timeout=CONFIG["ELEM_TIMEOUT_MS"]
    )
    await page.locator("input[type='password']:visible").first.fill(clave)
    await page.keyboard.press("Enter")

    await page.wait_for_function(
        f"() => window.location.href.startsWith({json.dumps(CONFIG['bochica_url'])})",
        timeout=CONFIG["NAV_TIMEOUT_MS"],
    )
    log_event("login_bochica_ok", msg="Autenticación exitosa en Bochica (Google)")


async def pagina_con_sesion_guardada(browser, ruta_sesion: Path = SESION_DEFAULT) -> Page:
    """Abre una página reutilizando la sesión de Google guardada por
    `sembrar_sesion()`, sin volver a pasar por el login (DEC-116).

    Google detecta el login automatizado de `login_bochica()` (vía
    Playwright/CDP) como bot y responde con un CAPTCHA que ningún selector
    puede resolver — confirmado en vivo el 2026-08-14: el mismo login manual,
    en incógnito o en una ventana normal, nunca lo pide. La única vía estable
    para la corrida horaria es no volver a autenticar: reutilizar las cookies
    de una sesión ya validada por una persona.

    Args:
        browser: Browser de Playwright ya lanzado.
        ruta_sesion: Archivo de `storage_state` generado por `sembrar_sesion()`.

    Returns:
        La página, ya en `CONFIG["bochica_url"]` con la sesión activa.

    Raises:
        RuntimeError: si no hay sesión guardada o si ya expiró (Google
            redirige de vuelta al login) — requiere correr
            `python -m scraper.bochica --sembrar-sesion` de nuevo.
    """
    if not ruta_sesion.exists():
        raise RuntimeError(
            f"No hay sesión guardada en {ruta_sesion}. "
            "Corre `python -m scraper.bochica --sembrar-sesion` una vez, con "
            "una persona completando el login de Google en la ventana visible."
        )
    ctx = await browser.new_context(locale="es-CO", storage_state=str(ruta_sesion))
    page = await ctx.new_page()
    await page.goto(CONFIG["bochica_url"], timeout=CONFIG["NAV_TIMEOUT_MS"])
    if not page.url.startswith(CONFIG["bochica_url"]):
        raise RuntimeError(
            f"La sesión guardada en {ruta_sesion} ya expiró (Google redirigió a "
            f"{page.url}). Corre `python -m scraper.bochica --sembrar-sesion` de nuevo."
        )
    log_event("bochica_sesion_reutilizada", msg="Sesión de Google cargada desde storage_state")
    return page


async def sembrar_sesion(ruta_sesion: Path = SESION_DEFAULT) -> None:
    """Modo manual, con navegador visible: una persona completa el login de
    Google una sola vez y el resultado (cookies) queda guardado para que
    `pagina_con_sesion_guardada()` lo reutilice en cada corrida automática.

    No llena ningún campo — deliberado. Es la persona quien escribe correo y
    contraseña, con mouse y teclado reales, para que Google la trate como el
    login humano que ya confirmamos que no dispara CAPTCHA (2026-08-14). Este
    proceso solo abre el navegador y espera a que la navegación llegue de
    vuelta a `bochica_url`, la misma señal de éxito que usa `login_bochica()`.

    Args:
        ruta_sesion: Dónde guardar el `storage_state` resultante.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(locale="es-CO")
        page = await ctx.new_page()
        await page.goto(CONFIG["bochica_url"], timeout=CONFIG["NAV_TIMEOUT_MS"])
        print(
            "\nCompleta el login de Google en la ventana del navegador "
            f"(usuario: {CONFIG['bochica_usuario']}).\n"
            "Esperando hasta 10 minutos a que la navegación llegue de vuelta a Bochica...\n"
        )
        await page.wait_for_function(
            f"() => window.location.href.startsWith({json.dumps(CONFIG['bochica_url'])})",
            timeout=600_000,
        )
        ruta_sesion.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(ruta_sesion))
        print(f"Sesión guardada en {ruta_sesion}")
        log_event("bochica_sesion_sembrada", msg=f"storage_state guardado en {ruta_sesion}")
        await browser.close()


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
    import sys

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
            # DEC-116: el login de Google vía Playwright dispara CAPTCHA — se
            # reutiliza una sesión sembrada manualmente en vez de autenticar
            # en cada corrida. login_bochica() queda disponible para
            # sembrar_sesion() y para investigación puntual, pero la corrida
            # automática ya no la llama.
            page = await pagina_con_sesion_guardada(browser)
            await login_app_bochica(
                page, CONFIG["bochica_app_usuario"], CONFIG["bochica_app_clave"]
            )
            ruta = await descargar_inventario_global(page)
            print(f"Inventario de Bochica descargado en: {ruta}")
            await browser.close()

    if "--sembrar-sesion" in sys.argv:
        asyncio.run(sembrar_sesion())
    else:
        asyncio.run(_main())
