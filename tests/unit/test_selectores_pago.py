"""Los nombres que el extractor espera son los que el origen emite — DEC-087.

`test_extractores_pago.py` prueba el mapeo con un fake, y por construcción
**no puede detectar un nombre mal escrito**: si el extractor busca
`.pay-info-lable` y el fake devuelve lo que el extractor pide, todo pasa y en
producción no se captura nada.

Acá se fijan los nombres **transcritos del DOM real** que el Arquitecto
compartió el 2026-07-31, parseado en local sin exponer datos. Si alguien
toca un selector o una etiqueta del extractor sin que el origen haya
cambiado, la suite frena.

Cuando el origen SÍ cambie —y cambia: 18 veces entre enero y julio,
DEC-081— este test es el que hay que actualizar, con la evidencia del DOM
nuevo al lado.
"""

import pytest

from scraper.extractores import (
    _COLUMNAS_REGISTRO_PAGO,
    _ETIQUETAS_PAGO,
    _JS_OPERACION_PAGO,
    _JS_REGISTROS_PAGO,
)

pytestmark = pytest.mark.unit

# Transcritos del DOM real (2026-07-31), en el orden en que los emite la SPA.
ENCABEZADOS_REALES = [
    "Secuencia",
    "Método de pago",
    "Cuenta receptora",
    "Monto del comprobante",
    "Monto de pago",
    "Hora de pago",
    "Comprobante",
    "Fecha de envío",
    "Estado de revisión",
    "Fecha de revisión",
    "Revisor",
    "Observaciones",
]

ETIQUETAS_REALES = [
    "Monto total del pedido",
    "Monto pagado",
    "Saldo pendiente",
    "Progreso de pago",
]

# Clases observadas en el DOM real. Las de Element Plus (`el-*`) son del
# framework y no las controla la empresa; las `pay-*` sí son del componente.
CLASES_REALES = [
    "payment-progress-card",
    "pay-info-item",
    "pay-info-label",
    "pay-info-value",
    "payment-records-card",
    "el-tag__content",
    "el-table",
    "el-table__header",
    "el-table__body",
    "cell",
]


def test_las_doce_columnas_estan_mapeadas():
    """Ni de más ni de menos: una columna sin mapear se pierde en silencio,
    y una de más señala que alguien transcribió mal un nombre."""
    assert list(_COLUMNAS_REGISTRO_PAGO) == ENCABEZADOS_REALES


def test_las_cuatro_etiquetas_de_la_tarjeta_estan_mapeadas():
    assert list(_ETIQUETAS_PAGO) == ETIQUETAS_REALES


def test_el_saldo_pendiente_esta_capturado():
    """Es la razón de ser de DEC-087: el saldo calculado por el origen es lo
    que elimina la derivación que dejó tres cifras de cartera sin publicar
    (DEC-082/084/086)."""
    assert _ETIQUETAS_PAGO["Saldo pendiente"] == "pago_saldo"


@pytest.mark.parametrize("clase", CLASES_REALES)
def test_el_js_usa_clases_que_existen_en_el_origen(clase):
    js = _JS_OPERACION_PAGO + _JS_REGISTROS_PAGO
    assert clase in js, f"ningún JS referencia .{clase}"


def test_el_js_no_indexa_columnas_por_posicion():
    """DEC-023 costó 10.696 pedidos mal poblados justamente por esto: leer
    por índice y que el origen inserte una fila. La tabla se recorre
    emparejando cada celda con el texto de su `<th>`."""
    assert "encabezados[i]" in _JS_REGISTROS_PAGO
    assert "querySelectorAll('.el-table__header thead th')" in _JS_REGISTROS_PAGO


def test_la_tarjeta_se_lee_por_etiqueta_no_por_orden():
    assert ".pay-info-label" in _JS_OPERACION_PAGO
    assert "textContent.trim()] =" in _JS_OPERACION_PAGO.replace(" ", " ")
