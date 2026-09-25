"""Login contra un formulario que se reinicia solo — DEC-141.

El login del origen es de AWS Cognito: tras el primer `load` sigue una cadena
de redirecciones y el formulario definitivo aparece ~3 s después, con la
huella `cognitoAsfData`. Llenar antes hacía que la siguiente navegación
borrara lo escrito o que se enviara un formulario sin huella (400). Acá se
simula en un navegador real, sin tocar el origen.
"""

import pytest
from playwright.async_api import async_playwright

from scraper.extractores import (
    _JS_ESPERA_LOGIN,
    _JS_FORMULARIO_LOGIN_LISTO,
    _SEL_LOGIN_CLAVE,
    _SEL_LOGIN_USUARIO,
    _llenar_formulario_login,
)

_FORM = """
<form>
  <input type="hidden" name="csrf" value="x">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="hidden" name="cognitoAsfData" value="">
  <button type="submit">Sign in</button>
</form>
"""

# Borra los dos campos UNA vez, poco después del primer llenado: la
# re-inicialización de la página que se midió en producción.
_BORRA_UNA_VEZ = """
<script>
  let borrado = false;
  document.querySelector("input[name=username]").addEventListener("input", () => {
    if (borrado) return;
    borrado = true;
    setTimeout(() => document.querySelectorAll("input[type=text],input[type=password]")
      .forEach(e => e.value = ""), 50);
  });
</script>
"""


async def _pagina(html: str):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch()
    page = await browser.new_page()
    await page.set_content(html)
    return pw, browser, page


@pytest.mark.e2e
async def test_formulario_que_se_borra_se_vuelve_a_llenar():
    pw, browser, page = await _pagina(_FORM + _BORRA_UNA_VEZ)
    try:
        rellenos = await _llenar_formulario_login(page, "usuario@x", "clave123")
        assert rellenos == 1
        assert await page.locator(_SEL_LOGIN_USUARIO).first.input_value() == "usuario@x"
        assert await page.locator(_SEL_LOGIN_CLAVE).first.input_value() == "clave123"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.e2e
async def test_formulario_estable_no_se_rellena():
    pw, browser, page = await _pagina(_FORM)
    try:
        assert await _llenar_formulario_login(page, "usuario@x", "clave123") == 0
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.e2e
async def test_formulario_listo_solo_con_huella_calculada():
    pw, browser, page = await _pagina(_FORM)
    try:
        assert await page.evaluate(_JS_FORMULARIO_LOGIN_LISTO) is False
        await page.evaluate(
            "() => document.querySelector(\"input[name=cognitoAsfData]\").value = 'abc'"
        )
        assert await page.evaluate(_JS_FORMULARIO_LOGIN_LISTO) is True
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.e2e
async def test_espera_post_clic_detecta_formulario_vacio():
    """Sin redirección y con un campo vacío: el envío no salió o fue
    rechazado — se falla ya en vez de esperar 45 s."""
    pw, browser, page = await _pagina(_FORM)
    try:
        arg = ["https://destino.invalido/", _SEL_LOGIN_USUARIO, _SEL_LOGIN_CLAVE]
        assert await page.evaluate(_JS_ESPERA_LOGIN, arg) == "vacio"
        await page.locator(_SEL_LOGIN_USUARIO).first.fill("u")
        await page.locator(_SEL_LOGIN_CLAVE).first.fill("c")
        assert await page.evaluate(_JS_ESPERA_LOGIN, arg) is False
    finally:
        await browser.close()
        await pw.stop()
