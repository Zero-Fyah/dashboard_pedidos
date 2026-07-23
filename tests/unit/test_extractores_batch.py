"""Tests de los extractores batcheados por page.evaluate() — DEC-030 Fase 3.

Antes de la Fase 3 estas funciones no tenían tests dedicados (requerían
un browser real, gap aceptado desde Fase 7). Con la lectura colapsada a
un solo page.evaluate(), el lado Python (mapeo, validación, warnings) se
puede probar con un fake que solo simula `.evaluate()` — el mismo patrón
que ya usa `test_lista_pedidos.py` para `_leer_ids_pagina`. El JS en sí
(la extracción del DOM) no es verificable aquí; se validó contra el DOM
real compartido en sesión y se seguirá verificando en el próximo piloto.
"""

import pytest

import scraper.extractores as sp
from scraper.extractores import (
    extraer_detalle_diferencias,
    extraer_estadisticas_monto,
    extraer_gestion_diferencias,
    extraer_info_general,
    extraer_registro_operaciones,
    extraer_subpedidos,
    extraer_timeline,
    extraer_total_subpedidos,
)


class _FakePage:
    def __init__(self, valor_evaluate):
        self._valor = valor_evaluate

    async def evaluate(self, js, arg=None):
        return self._valor


# ── extraer_timeline ────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_timeline_mapea_pasos_en_orden():
    pasos = [
        {"titulo": "Pendiente de pago", "fecha_hora": "2026-07-16 08:35:43", "completado": 1},
        {"titulo": "Alistamiento", "fecha_hora": "2026-07-16 15:37:59", "completado": 1},
    ]
    res = await extraer_timeline(_FakePage(pasos), "TEST-1")
    assert res == [
        {
            "id_pedido": "TEST-1",
            "paso": 1,
            "titulo": "Pendiente de pago",
            "fecha_hora": "2026-07-16 08:35:43",
            "completado": 1,
        },
        {
            "id_pedido": "TEST-1",
            "paso": 2,
            "titulo": "Alistamiento",
            "fecha_hora": "2026-07-16 15:37:59",
            "completado": 1,
        },
    ]


@pytest.mark.unit
async def test_timeline_sin_wrapper_retorna_vacio():
    assert await extraer_timeline(_FakePage(None), "TEST-2") == []


@pytest.mark.unit
async def test_timeline_excepcion_se_captura(monkeypatch):
    eventos = []
    monkeypatch.setattr(sp, "log_event", lambda evento, **kw: eventos.append((evento, kw)))

    class _RotaPage:
        async def evaluate(self, js, arg=None):
            raise RuntimeError("Connection closed while reading from the driver")

    res = await extraer_timeline(_RotaPage(), "TEST-3")
    assert res == []
    assert eventos[0][0] == "timeline_error"
    assert eventos[0][1]["level"] == "WARNING"


# ── extraer_registro_operaciones ────────────────────────────────────────────


@pytest.mark.unit
async def test_registro_ops_separa_accion_y_referencia():
    items = [
        {
            "momento": "2026-07-16 11:02:43",
            "usuario": "DUVIER DALLAN MORENO TRIANA",
            "tipo_usuario": "system",
            "contenido": "Auditoría de pago - Aprobado",
        },
        {
            "momento": "2026-07-16 08:35:40",
            "usuario": "ABAD RIOS FERNANDO",
            "tipo_usuario": "member",
            "contenido": "Usuario realizó pedido",
        },
    ]
    res = await extraer_registro_operaciones(_FakePage(items), "TEST-4")
    assert res[0]["accion"] == "Auditoría de pago"
    assert res[0]["referencia"] == "Aprobado"
    assert res[0]["tipo_usuario"] == "system"
    assert res[1]["accion"] == "Usuario realizó pedido"
    assert res[1]["referencia"] == ""


@pytest.mark.unit
async def test_registro_ops_lista_vacia():
    assert await extraer_registro_operaciones(_FakePage([]), "TEST-5") == []


# ── extraer_info_general ────────────────────────────────────────────────────


_CRUDO_INFO_GENERAL_OK = {
    "número de pedido": "2077748963",
    "fecha del pedido": "2026-07-16 08:35:40",
    "servicio al cliente": "GLENIS ELIANA MESA NUÑEZ",
    "vendedor": "RSC",
    "forma de pago": "Pago inmediato",
    "método de entrega": "Ruta",
    "destinatario": "Fernando Abad",
}


@pytest.mark.unit
async def test_info_general_mapea_etiquetas_conocidas():
    res = await extraer_info_general(_FakePage(_CRUDO_INFO_GENERAL_OK))
    assert res["id_pedido"] == "2077748963"
    assert res["fecha"] == "2026-07-16 08:35:40"
    assert res["vendedor"] == "RSC"
    assert res["metodo_entrega"] == "Ruta"


@pytest.mark.unit
async def test_info_general_ignora_etiquetas_desconocidas():
    crudo = dict(_CRUDO_INFO_GENERAL_OK, **{"campo nuevo de la spa": "valor"})
    res = await extraer_info_general(_FakePage(crudo))
    assert "campo nuevo de la spa" not in res


@pytest.mark.unit
async def test_info_general_mapea_persona_y_movil_recogida():
    """DEC-032: metodo_entrega='Almacen' reemplaza destinatario/telefono
    por persona de recogida/móvil de recogida — antes se perdían en
    silencio."""
    crudo = dict(_CRUDO_INFO_GENERAL_OK)
    del crudo["destinatario"]
    crudo["método de entrega"] = "Almacen"
    crudo["persona de recogida"] = "Fernando Abad"
    crudo["móvil de recogida"] = "3001234567"
    res = await extraer_info_general(_FakePage(crudo))
    assert res["persona_recogida"] == "Fernando Abad"
    assert res["movil_recogida"] == "3001234567"


@pytest.mark.unit
async def test_info_general_mapea_campos_credito():
    """DEC-033: forma_pago='Pago a crédito' agrega 3 etiquetas de crédito."""
    crudo = dict(
        _CRUDO_INFO_GENERAL_OK,
        **{
            "forma de pago": "Pago a crédito",
            "días de crédito": "30",
            "inicio de crédito": "2026-07-16",
            "vencimiento de crédito": "2026-08-15",
        },
    )
    res = await extraer_info_general(_FakePage(crudo))
    assert res["dias_credito"] == "30"
    assert res["inicio_credito"] == "2026-07-16"
    assert res["vencimiento_credito"] == "2026-08-15"


@pytest.mark.unit
async def test_info_general_etiqueta_desconocida_emite_warning(monkeypatch):
    """DEC-032: una etiqueta nueva se reporta, no se pierde callada."""
    eventos = []
    monkeypatch.setattr(sp, "log_event", lambda evento, **kw: eventos.append((evento, kw)))
    crudo = dict(_CRUDO_INFO_GENERAL_OK, **{"campo nuevo de la spa": "valor"})
    await extraer_info_general(_FakePage(crudo))
    desconocidas = [e for e in eventos if e[0] == "info_general_etiqueta_desconocida"]
    assert len(desconocidas) == 1
    assert "campo nuevo de la spa" in desconocidas[0][1]["msg"]
    assert desconocidas[0][1]["level"] == "WARNING"


@pytest.mark.unit
async def test_info_general_id_pedido_vacio_lanza_valueerror():
    crudo = dict(_CRUDO_INFO_GENERAL_OK)
    del crudo["número de pedido"]
    with pytest.raises(ValueError, match="vacío"):
        await extraer_info_general(_FakePage(crudo))


@pytest.mark.unit
async def test_info_general_id_pedido_placeholder_lanza_valueerror():
    crudo = dict(_CRUDO_INFO_GENERAL_OK, **{"número de pedido": "N/A"})
    with pytest.raises(ValueError, match="inválido"):
        await extraer_info_general(_FakePage(crudo))


# ── extraer_estadisticas_monto ──────────────────────────────────────────────


@pytest.mark.unit
async def test_estadisticas_sin_card_retorna_no_verificado():
    filas, hay_dif = await extraer_estadisticas_monto(_FakePage(None), "TEST-6")
    assert filas == []
    assert hay_dif is None


@pytest.mark.unit
async def test_estadisticas_mapea_filas_y_hay_diferencia():
    crudo = {
        "hay_diferencia": True,
        "filas": [
            {
                "concepto": "Total precio original",
                "concepto_tag": "",
                "monto_pagar": "COP 431.539",
                "monto_final": "COP 431.539",
                "diferencia": "COP 0",
            },
            {
                "concepto": "IVA alimentos",
                "concepto_tag": "5%",
                "monto_pagar": "COP 16.522",
                "monto_final": "COP 16.522",
                "diferencia": "COP 0",
            },
        ],
    }
    filas, hay_dif = await extraer_estadisticas_monto(_FakePage(crudo), "TEST-7")
    assert hay_dif is True
    assert len(filas) == 2
    assert filas[0]["orden"] == 1
    assert filas[1]["orden"] == 2
    assert filas[1]["concepto_tag"] == "5%"
    assert all(f["id_pedido"] == "TEST-7" for f in filas)


@pytest.mark.unit
async def test_estadisticas_card_presente_sin_diferencia():
    crudo = {"hay_diferencia": False, "filas": []}
    filas, hay_dif = await extraer_estadisticas_monto(_FakePage(crudo), "TEST-8")
    assert hay_dif is False
    assert filas == []


# ── extraer_gestion_diferencias ─────────────────────────────────────────────


@pytest.mark.unit
async def test_gestion_dif_sin_card_retorna_none():
    assert await extraer_gestion_diferencias(_FakePage(None), "TEST-9") is None


@pytest.mark.unit
async def test_gestion_dif_mapea_los_4_valores_en_orden():
    valores = ["COP 431.539", "COP 423.496", "COP 0", "COP 8.043"]
    res = await extraer_gestion_diferencias(_FakePage(valores), "TEST-10")
    assert res == {
        "id_pedido": "TEST-10",
        "total_pagar_pedido": "COP 431.539",
        "monto_final_pagar": "COP 423.496",
        "monto_pagado": "COP 0",
        "monto_diferencia": "COP 8.043",
    }


@pytest.mark.unit
async def test_gestion_dif_menos_de_4_valores_rellena_vacio():
    res = await extraer_gestion_diferencias(_FakePage(["COP 100"]), "TEST-11")
    assert res["total_pagar_pedido"] == "COP 100"
    assert res["monto_final_pagar"] == ""
    assert res["monto_pagado"] == ""
    assert res["monto_diferencia"] == ""


# ── extraer_subpedidos ──────────────────────────────────────────────────────
# extraer_subpedidos quedó 100% basado en evaluate() (el descuento y la
# presentación se procesan con las funciones puras compartidas con
# leer_celda_descuento()/leer_presentacion(), no con ElementHandles) — el
# fake solo necesita simular el paso 1 (sin iconos por expandir) y el
# resultado crudo del evaluate() del paso 2.


class _FakePageSubpedidos(_FakePage):
    """Además de evaluate(), simula query_selector_all del paso 1 (expand)."""

    async def query_selector_all(self, sel: str):
        assert sel == "div.el-table__expand-icon"
        return []  # sin iconos pendientes de expandir en este fake


_CRUDO_SUBPEDIDO_SIMPLE = [
    {
        "raw_child_order_id": "Arena + 176319",
        "estado": "Completado",
        "inicio_alistamiento": "2026-07-16 11:02:43",
        "alistamiento_completado": "2026-07-16 15:42:47",
        "alistador": "JOSE RAMON PULIDO RIVAS",
        "inicio_inspeccion": "2026-07-16 15:37:57",
        "inspeccion_completada": "2026-07-16 15:42:47",
        "inspector": "PASTOR YESID RODRIGUEZ NIETO",
        "lineas": [
            {
                "numero_caja": "",
                "nombre_producto": "Arena clasica unidades",
                "referencia": "PRA13",
                "codigo_barras_raw": "Código de barras: 6972228791654",
                "presentacion_specs": ["Presentacion: PRA13, CLASICA, 4.5KG, VAINILLA"],
                "almacen": "Bogotá",
                "cantidad_comprada_raw": "1",
                "cantidad_entregada_raw": "1",
                "tipo": "Arena",
                "precio_unitario": "COP 9.777",
                "descuento_etiquetas": ["Tipo de cambio6%"],
                "descuento_monto_crudo": "-",
                "descuento_texto_completo": "-\nTipo de cambio6%",
                "precio_descuento": "COP 9.190",
                "monto_pagar": "COP 10.936",
                "monto_final": "COP 10.936",
                "iva": "COP 1.746",
                "peso_total": "4500g",
                "observaciones": "-",
            }
        ],
    }
]


@pytest.mark.unit
async def test_subpedidos_separa_tipo_y_numero():
    res = await extraer_subpedidos(_FakePageSubpedidos(_CRUDO_SUBPEDIDO_SIMPLE))
    assert res[0]["tipo_subpedido"] == "Arena"
    assert res[0]["numero_subpedido"] == "176319"
    assert res[0]["estado"] == "Completado"


@pytest.mark.unit
async def test_subpedidos_sin_separador_marca_desconocido():
    crudo = [dict(_CRUDO_SUBPEDIDO_SIMPLE[0], raw_child_order_id="176319", lineas=[])]
    res = await extraer_subpedidos(_FakePageSubpedidos(crudo))
    assert res[0]["tipo_subpedido"] == "desconocido"
    assert res[0]["numero_subpedido"] == "176319"


@pytest.mark.unit
async def test_subpedidos_linea_codigo_barras_y_presentacion():
    res = await extraer_subpedidos(_FakePageSubpedidos(_CRUDO_SUBPEDIDO_SIMPLE))
    linea = res[0]["lineas"][0]
    assert linea["codigo_barras"] == "6972228791654"
    assert linea["presentacion"] == "Presentacion: PRA13, CLASICA, 4.5KG, VAINILLA"


@pytest.mark.unit
async def test_subpedidos_linea_descuento_reclasificado():
    """DEC-024: '-' + tag de tipo de cambio → descuento='-', tipo poblado."""
    res = await extraer_subpedidos(_FakePageSubpedidos(_CRUDO_SUBPEDIDO_SIMPLE))
    linea = res[0]["lineas"][0]
    assert linea["descuento"] == "-"
    assert linea["descuento_tipo"] == "Tipo de cambio6%"


@pytest.mark.unit
async def test_subpedidos_cantidad_no_numerica_emite_warning(monkeypatch):
    eventos = []
    monkeypatch.setattr(sp, "log_event", lambda evento, **kw: eventos.append((evento, kw)))
    crudo = [
        dict(
            _CRUDO_SUBPEDIDO_SIMPLE[0],
            lineas=[dict(_CRUDO_SUBPEDIDO_SIMPLE[0]["lineas"][0], cantidad_comprada_raw="varios")],
        )
    ]
    res = await extraer_subpedidos(_FakePageSubpedidos(crudo))
    assert res[0]["lineas"][0]["cantidad_comprada"] is None
    assert any(e[0] == "cantidad_no_numerica" for e in eventos)


@pytest.mark.unit
async def test_subpedidos_sin_subpedidos():
    assert await extraer_subpedidos(_FakePageSubpedidos([])) == []


# ── extraer_total_subpedidos ────────────────────────────────────────────────
# Seguimiento DEC-021: contador del origen para que FIX C-2 discrimine
# "0 subpedidos legítimo" de "no renderizó".


class _FakeElementoTexto:
    def __init__(self, texto: str):
        self._texto = texto

    async def inner_text(self) -> str:
        return self._texto


class _FakePageContador:
    def __init__(self, texto: str | None, lanza: bool = False):
        self._texto = texto
        self._lanza = lanza

    async def query_selector(self, sel: str):
        assert sel == "span.section-count"
        if self._lanza:
            raise RuntimeError("boom")
        if self._texto is None:
            return None
        return _FakeElementoTexto(self._texto)


@pytest.mark.unit
async def test_total_subpedidos_lee_contador_positivo():
    assert await extraer_total_subpedidos(_FakePageContador("Total 3 subpedidos")) == 3


@pytest.mark.unit
async def test_total_subpedidos_lee_contador_cero():
    assert await extraer_total_subpedidos(_FakePageContador("Total 0 subpedidos")) == 0


@pytest.mark.unit
async def test_total_subpedidos_selector_ausente_retorna_none():
    assert await extraer_total_subpedidos(_FakePageContador(None)) is None


@pytest.mark.unit
async def test_total_subpedidos_texto_no_parseable_retorna_none():
    assert await extraer_total_subpedidos(_FakePageContador("Subpedidos")) is None


@pytest.mark.unit
async def test_total_subpedidos_excepcion_retorna_none():
    assert (
        await extraer_total_subpedidos(_FakePageContador("Total 1 subpedidos", lanza=True)) is None
    )


# ── extraer_detalle_diferencias ─────────────────────────────────────────────
# Híbrido: bulk vía evaluate() + leer_celda_descuento() (ElementHandle) para
# la celda de descuento. El fake simula el card, el evaluate y las filas.


class _FakeSpanDD:
    def __init__(self, texto: str, clase: str = ""):
        self._texto = texto
        self._clase = clase

    async def get_attribute(self, nombre: str):
        return self._clase if nombre == "class" else None

    async def inner_text(self) -> str:
        return self._texto


class _FakeCeldaDescuentoDD:
    """Celda de descuento (índice 4) — mismo contrato que leer_celda_descuento."""

    def __init__(self, spans: list[_FakeSpanDD], tags: list[str], texto: str):
        self._spans = spans
        self._tags = tags
        self._texto = texto

    async def query_selector_all(self, sel: str):
        if sel == ".el-tag__content":
            return [_FakeSpanDD(t) for t in self._tags]
        if sel == "span":
            return self._spans
        return []

    async def inner_text(self) -> str:
        return self._texto


class _FakeFilaDD:
    def __init__(self, celda_descuento):
        self._celda_descuento = celda_descuento

    async def query_selector_all(self, sel: str):
        assert sel == "td"
        # 13 celdas para pasar el chequeo de longitud del extractor — solo
        # el índice 4 (descuento) se usa realmente, el resto viene del bulk.
        celdas = [object()] * 13
        celdas[4] = self._celda_descuento
        return celdas


class _FakeCardDD:
    def __init__(self, filas: list[_FakeFilaDD]):
        self._filas = filas

    async def query_selector_all(self, sel: str):
        assert sel == "tbody tr"
        return self._filas


class _FakePageDD:
    def __init__(self, card, bulk):
        self._card = card
        self._bulk = bulk

    async def query_selector(self, sel: str):
        assert sel == ".diff-items-card"
        return self._card

    async def evaluate(self, js, arg=None):
        return self._bulk


_FILA_BULK_DD = {
    "nombre_producto": "Snack cremoso para gatos",
    "especificacion": "A- Sabor a pollo",
    "tipo": "Accesorios",
    "precio_unitario": "COP 2.000",
    "precio_descuento": "COP 1.820",
    "cantidad_pedido": "10",
    "cantidad_entregada": "8",
    "diferencia_cantidad": "2",
    "monto_pagar_pedido": "COP 19.110",
    "monto_final_pagar": "COP 15.288",
    "iva": "COP 728",
    "monto_diferencia": "COP 3.822",
}


@pytest.mark.unit
async def test_detalle_dif_sin_card_retorna_vacio():
    res = await extraer_detalle_diferencias(_FakePageDD(None, None), "TEST-12")
    assert res == []


@pytest.mark.unit
async def test_detalle_dif_combina_bulk_y_descuento():
    celda_dto = _FakeCeldaDescuentoDD(
        spans=[_FakeSpanDD("-")], tags=["Tipo de cambio9%"], texto="-\nTipo de cambio9%"
    )
    fila = _FakeFilaDD(celda_dto)
    card = _FakeCardDD([fila])
    res = await extraer_detalle_diferencias(_FakePageDD(card, [_FILA_BULK_DD]), "TEST-13")

    assert len(res) == 1
    assert res[0]["id_pedido"] == "TEST-13"
    assert res[0]["nombre_producto"] == "Snack cremoso para gatos"
    assert res[0]["cantidad_entregada"] == "8"
    assert res[0]["descuento"] == "-"
    assert res[0]["descuento_tipo"] == "Tipo de cambio9%"


@pytest.mark.unit
async def test_detalle_dif_fila_invalida_en_bulk_se_omite():
    """Filas con <13 celdas llegan como None desde el JS y se saltan."""
    fila = _FakeFilaDD(_FakeCeldaDescuentoDD(spans=[], tags=[], texto=""))
    card = _FakeCardDD([fila])
    res = await extraer_detalle_diferencias(_FakePageDD(card, [None]), "TEST-14")
    assert res == []
