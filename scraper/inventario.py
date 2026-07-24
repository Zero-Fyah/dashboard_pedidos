"""
inventario.py — Descarga del Excel de inventario del sistema administrativo.

Primer flujo de DEC-039 (comparación de inventario bodega vs. sistema
administrativo). Reutiliza login() de extractores.py — misma sesión, misma
SPA — pero navega a la pantalla de gestión de inventario en vez del
listado de pedidos, así que vive en su propio módulo (SRP, DEC-013): es
un concepto de dominio distinto, sin selectores ni ritmo en común con el
scraping de pedidos.
"""

from pathlib import Path

from playwright.async_api import Download, Page

from scraper.config import CONFIG, log_event
from scraper.extractores import SEL_LISTA_FILAS, login

DESTINO_DEFAULT = Path(__file__).parent.parent / "data" / "inventario" / "admin_inventario.xlsx"


async def descargar_inventario_admin(page: Page, destino: Path = DESTINO_DEFAULT) -> Path:
    """Descarga el Excel de inventario disponible para la venta.

    Asume que `page` ya pasó por login() y su sesión está activa. Navega
    a la pantalla de gestión de inventario, dispara la búsqueda para
    cargar los datos (igual que el listado de pedidos: espera semántica
    de filas, no networkidle — N-2) y exporta el resultado a `destino`.

    Args:
        page: Página Playwright con sesión autenticada.
        destino: Ruta donde guardar el .xlsx. Se sobreescribe si ya
            existe — el cruce siempre usa la última descarga.

    Returns:
        La misma ruta `destino`, tras confirmar que el archivo se guardó.
    """
    if not CONFIG["url_inventario"]:
        raise RuntimeError(
            "SCRAPER_URL_INVENTARIO no está definida en .env — requerida "
            "para descargar el inventario del sistema administrativo."
        )

    await page.goto(CONFIG["url_inventario"], timeout=CONFIG["NAV_TIMEOUT_MS"])

    await page.get_by_role("button", name="Buscar", exact=True).click()
    await page.wait_for_selector(SEL_LISTA_FILAS, timeout=CONFIG["ELEM_TIMEOUT_MS"])

    async with page.expect_download(timeout=CONFIG["NAV_TIMEOUT_MS"]) as descarga_info:
        await page.get_by_role("button", name="Exportar", exact=True).click()
    descarga: Download = await descarga_info.value

    destino.parent.mkdir(parents=True, exist_ok=True)
    await descarga.save_as(destino)

    log_event("inventario_admin_descargado", msg=f"Guardado en {destino}")
    return destino


if __name__ == "__main__":
    import asyncio

    from playwright.async_api import async_playwright

    from scraper.config import CLAVE, USUARIO, validar_config

    async def _main() -> None:
        validar_config()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CONFIG["HEADLESS"],
                slow_mo=CONFIG["SLOW_MO"],
            )
            ctx = await browser.new_context(locale="es-CO")
            page = await ctx.new_page()
            await login(page, USUARIO, CLAVE)
            ruta = await descargar_inventario_admin(page)
            print(f"Inventario descargado en: {ruta}")
            await browser.close()

    asyncio.run(_main())
