"""`_JS_SUBPEDIDOS` contra un DOM real (hallazgo del Arquitecto, 2026-08-12).

`tests/unit/test_extractores_batch.py` fake-a `page.evaluate()` devolviendo
el resultado ya procesado — no ejercita el JS en sí, así que un bug en el
selector del DOM (como este) no lo detecta ningún test unitario. Acá se
levanta un browser real y se evalúa el JS contra HTML calcado del DOM que
compartió el Arquitecto (subpedido 181314): una fila de producto real y la
fila de resumen `goods-table-row summary-row` ('Total') que el selector por
clase capturaba de más.
"""

import pytest
from playwright.async_api import async_playwright

from scraper.extractores import _JS_SUBPEDIDOS

_DOM_CON_FILA_TOTAL = """
<div class="el-scrollbar__wrap--hidden-default">
  <table><tbody>
    <tr>
      <td class="el-table__expand-column"></td>
      <td><span class="child-order-id">Accesorios + 181314</span></td>
      <td></td>
      <td><span class="el-tag__content">Pendiente de pago (pago inmediato)</span></td>
      <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
    </tr>
    <tr>
      <td class="el-table__expanded-cell" colspan="10">
        <div class="goods-expand-area">
          <div class="goods-table-header">Encabezado</div>
          <div class="goods-table-row">
            <div class="goods-col"></div>
            <div class="goods-col goods-info-col">
              <div class="goods-text">
                <div class="goods-name">Forro protector para carro</div>
                <div class="goods-sn">Referencia: <span class="sn-tag">PB62</span></div>
                <div class="goods-barcode">Código de barras: 6972228790862</div>
                <div class="goods-specs"><span>Colores: Negro Huellitas 160*140cm</span></div>
              </div>
            </div>
            <div class="goods-col">Bogotá</div>
            <div class="goods-col">2</div>
            <div class="goods-col">2</div>
            <div class="goods-col goods-col--type"><span class="el-tag__content">Accesorios</span></div>
            <div class="goods-col goods-col--price">COP 36.900</div>
            <div class="goods-col goods-col--discount"><span>-</span></div>
            <div class="goods-col goods-col--price"><span>COP 32.472</span></div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price"><span>COP 12.339</span></div>
            <div class="goods-col">1758g</div>
            <div class="goods-col">-</div>
          </div>
          <div class="goods-table-row summary-row">
            <div class="goods-col">Total</div>
            <div class="goods-col"></div>
            <div class="goods-col"></div>
            <div class="goods-col">2</div>
            <div class="goods-col">2</div>
            <div class="goods-col goods-col--type"></div>
            <div class="goods-col goods-col--price"></div>
            <div class="goods-col goods-col--discount"></div>
            <div class="goods-col goods-col--price"></div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price">COP 12.339</div>
            <div class="goods-col">1758g</div>
            <div class="goods-col"></div>
          </div>
        </div>
      </td>
    </tr>
  </tbody></table>
</div>
"""


@pytest.mark.e2e
async def test_js_subpedidos_descarta_la_fila_total():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(_DOM_CON_FILA_TOTAL)
        resultado = await page.evaluate(_JS_SUBPEDIDOS)
        await browser.close()

    assert len(resultado) == 1
    lineas = resultado[0]["lineas"]
    assert len(lineas) == 1, "la fila 'Total' (summary-row) no debe quedar como línea de producto"
    assert lineas[0]["referencia"] == "PB62"
    assert lineas[0]["cantidad_comprada_raw"] == "2"


_DOM_SIN_FILA_TOTAL = """
<div class="el-scrollbar__wrap--hidden-default">
  <table><tbody>
    <tr>
      <td class="el-table__expand-column"></td>
      <td><span class="child-order-id">Accesorios + 181314</span></td>
      <td></td>
      <td><span class="el-tag__content">Pendiente de pago (pago inmediato)</span></td>
      <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
    </tr>
    <tr>
      <td class="el-table__expanded-cell" colspan="10">
        <div class="goods-expand-area">
          <div class="goods-table-header">Encabezado</div>
          <div class="goods-table-row">
            <div class="goods-col"></div>
            <div class="goods-col goods-info-col">
              <div class="goods-text">
                <div class="goods-name">Forro protector para carro</div>
                <div class="goods-sn">Referencia: <span class="sn-tag">PB62</span></div>
                <div class="goods-barcode">Código de barras: 6972228790862</div>
                <div class="goods-specs"><span>Colores: Negro Huellitas 160*140cm</span></div>
              </div>
            </div>
            <div class="goods-col">Bogotá</div>
            <div class="goods-col">2</div>
            <div class="goods-col">2</div>
            <div class="goods-col goods-col--type"><span class="el-tag__content">Accesorios</span></div>
            <div class="goods-col goods-col--price">COP 36.900</div>
            <div class="goods-col goods-col--discount"><span>-</span></div>
            <div class="goods-col goods-col--price"><span>COP 32.472</span></div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price">COP 77.283</div>
            <div class="goods-col goods-col--price"><span>COP 12.339</span></div>
            <div class="goods-col">1758g</div>
            <div class="goods-col">-</div>
          </div>
        </div>
      </td>
    </tr>
  </tbody></table>
</div>
"""


@pytest.mark.e2e
async def test_js_subpedidos_sin_fila_total_no_pierde_lineas():
    """Control: un subpedido sin fila de resumen conserva su única línea real."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(_DOM_SIN_FILA_TOTAL)
        resultado = await page.evaluate(_JS_SUBPEDIDOS)
        await browser.close()

    assert len(resultado[0]["lineas"]) == 1
    assert resultado[0]["lineas"][0]["referencia"] == "PB62"
