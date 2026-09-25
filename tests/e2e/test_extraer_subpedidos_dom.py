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

from scraper.extractores import _COLUMNAS_LINEA, _JS_SUBPEDIDOS

# Encabezado real de 15 columnas (DOM compartido por el Arquitecto el
# 2026-09-23, DEC-140): «Peso entregado» entre «Peso pedido» y
# «Observaciones».
_COLUMNAS_15 = [
    "Número de caja",
    "Información de productos",
    "Almacén",
    "Cantidad comprada",
    "Cantidad entregada",
    "Tipo",
    "Precio unitario",
    "Descuento",
    "Precio con descuento",
    "Monto a pagar del pedido",
    "Monto final a pagar",
    "IVA",
    "Peso pedido",
    "Peso entregado",
    "Observaciones",
]


def _encabezado(columnas: list[str]) -> str:
    celdas = "".join(
        f'<div class="goods-col">{c} <i class="el-icon"><svg></svg></i></div>' for c in columnas
    )
    return f'<div class="goods-table-header">{celdas}</div>'


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
          {ENCABEZADO}
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
            <div class="goods-col">1758g</div>
            <div class="goods-col"></div>
          </div>
        </div>
      </td>
    </tr>
  </tbody></table>
</div>
""".replace("{ENCABEZADO}", _encabezado(_COLUMNAS_15))


@pytest.mark.e2e
async def test_js_subpedidos_descarta_la_fila_total():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(_DOM_CON_FILA_TOTAL)
        resultado = await page.evaluate(_JS_SUBPEDIDOS, _COLUMNAS_LINEA)
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
          {ENCABEZADO}
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
            <div class="goods-col">1758g</div>
            <div class="goods-col">-</div>
          </div>
        </div>
      </td>
    </tr>
  </tbody></table>
</div>
""".replace("{ENCABEZADO}", _encabezado(_COLUMNAS_15))


@pytest.mark.e2e
async def test_js_subpedidos_sin_fila_total_no_pierde_lineas():
    """Control: un subpedido sin fila de resumen conserva su única línea real."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(_DOM_SIN_FILA_TOTAL)
        resultado = await page.evaluate(_JS_SUBPEDIDOS, _COLUMNAS_LINEA)
        await browser.close()

    assert len(resultado[0]["lineas"]) == 1
    assert resultado[0]["lineas"][0]["referencia"] == "PB62"


async def _evaluar(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html)
        resultado = await page.evaluate(_JS_SUBPEDIDOS, _COLUMNAS_LINEA)
        await browser.close()
    return resultado


@pytest.mark.e2e
async def test_js_subpedidos_15_columnas_no_corre_observaciones():
    """DEC-140: con «Peso entregado» en el medio, `observaciones` sigue
    leyendo su propia columna y no el peso (lo que pasó del 2026-08-19 al
    2026-09-23 leyendo por posición)."""
    linea = (await _evaluar(_DOM_SIN_FILA_TOTAL))[0]["lineas"][0]
    assert linea["peso_total"] == "1758g"
    assert linea["peso_entregado"] == "1758g"
    assert linea["observaciones"] == "-"
    assert linea["cantidad_comprada_raw"] == "2"
    assert linea["precio_unitario"] == "COP 36.900"


@pytest.mark.e2e
async def test_js_subpedidos_devuelve_encabezados_leidos():
    sp = (await _evaluar(_DOM_SIN_FILA_TOTAL))[0]
    assert sp["encabezados"] == _COLUMNAS_15


@pytest.mark.e2e
async def test_js_subpedidos_columnas_reordenadas_se_leen_por_nombre():
    """Si el origen mueve columnas, el mapeo sigue al encabezado."""
    columnas = [
        "Información de productos",
        "Cantidad entregada",
        "Cantidad comprada",
        "Observaciones",
        "Peso entregado",
        "Peso pedido",
    ]
    html = f"""
<div class="el-scrollbar__wrap--hidden-default"><table><tbody>
  <tr><td class="el-table__expand-column"></td>
      <td><span class="child-order-id">Arena + 1</span></td></tr>
  <tr><td class="el-table__expanded-cell"><div class="goods-expand-area">
    {_encabezado(columnas)}
    <div class="goods-table-row">
      <div class="goods-col"><div class="goods-name">X</div>
        <div class="goods-sn">Referencia: <span class="sn-tag">PRA13</span></div></div>
      <div class="goods-col">1</div>
      <div class="goods-col">3</div>
      <div class="goods-col">frágil</div>
      <div class="goods-col">4.5KG</div>
      <div class="goods-col">13.5KG</div>
    </div>
  </div></td></tr>
</tbody></table></div>"""
    linea = (await _evaluar(html))[0]["lineas"][0]
    assert linea["referencia"] == "PRA13"
    assert linea["cantidad_comprada_raw"] == "3"
    assert linea["cantidad_entregada_raw"] == "1"
    assert linea["observaciones"] == "frágil"
    assert linea["peso_entregado"] == "4.5KG"
    assert linea["peso_total"] == "13.5KG"
    assert linea["precio_unitario"] == ""  # columna ausente → vacío, no corrimiento
