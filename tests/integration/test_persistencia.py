import copy
import pytest
import aiosqlite
import asyncio
from scraper.scraper_principal import persistencia_worker

_PEDIDO_BASE = {
    "tipo": "completo",
    "id_pedido": "TEST-001",
    "info_general": {
        "id_pedido":         "TEST-001",
        "fecha":             "2026-05-22 10:30:00",
        "hora":              "10:30:00",
        "vendedor":          "Vendedor Test",
        "servicio_cliente":  "Cliente Test",
        "forma_pago":        "Crédito",
        "comprobante":       "COMP-001",
        "nombre_empresa":    "Empresa Test",
        "nit":               "900000000-1",
        "metodo_entrega":    "Domicilio",
        "destinatario":      "Destinatario Test",
        "telefono":          "3001234567",
        "direccion_envio":   "Calle Test 123",
        "observaciones":     "",
        "alistador_pedido":  "",
        "inspector_pedido":  "",
        "movil_cliente":     "",
    },
    "subpedidos": [
        {
            "numero_subpedido":        "SUB-001",
            "tipo_subpedido":          "Normal",
            "estado":                  "en alistamiento",
            "inicio_alistamiento":     "2026-05-22 08:00:00",
            "alistamiento_completado": "",
            "alistador":               "Alistador Test",
            "inicio_inspeccion":       "",
            "inspeccion_completada":   "",
            "inspector":               "",
            "lineas": [
                {
                    "nombre_producto":   "Producto Test",
                    "referencia":        "REF-001",
                    "codigo_barras":     "7700000000001",
                    "presentacion":      "Unidad",
                    "almacen":           "Almacén Principal",
                    "cantidad_comprada":  10.0,
                    "cantidad_entregada": 0.0,
                    "precio_unitario":   "10.000,00",
                    "descuento":         "0,00",
                    "precio_descuento":  "10.000,00",
                    "monto_pagar":       "100.000,00",
                    "monto_final":       "100.000,00",
                    "iva":               "0,00",
                    "peso_total":        "1,00",
                    "observaciones":     "",
                    "numero_caja":       "",
                    "tipo":              "",
                },
            ],
        }
    ],
    "timeline": [
        {"id_pedido": "TEST-001", "paso": 1, "titulo": "Recibido",
         "fecha_hora": "2026-05-22 07:00:00", "completado": 1},
    ],
    "info_entrega":  {"despachador": "", "hora_entrega": "", "obs_entrega": "",
                      "entrega_ruta_tag": "", "entrega_descuento_tag": ""},
    "estadisticas":  [],
    "hay_diferencia": 0,
    "gestion_dif":   None,
    "detalle_dif":   [],
    "registro_ops":  [],
}


@pytest.fixture
def pedido_sin_diferencias():
    return copy.deepcopy(_PEDIDO_BASE)


async def persistir_uno(resultado: dict, db_path: str) -> None:
    """Helper: encola un resultado y ejecuta el worker hasta el sentinel."""
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(resultado)
    await queue.put(None)  # sentinel
    await persistencia_worker(queue, db_path)


@pytest.mark.integration
async def test_modo_completo_inserta_pedido(db_path, pedido_sin_diferencias):
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT scraping_completo FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone()
    assert row is not None
    assert row[0] == 1


@pytest.mark.integration
async def test_modo_completo_es_idempotente(db_path, pedido_sin_diferencias):
    """Insertar el mismo pedido dos veces no duplica registros."""
    await persistir_uno(pedido_sin_diferencias, db_path)
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone())[0]
    assert count == 1


@pytest.mark.integration
async def test_modo_solo_estado_no_modifica_lineas(db_path, pedido_sin_diferencias):
    """solo_estado actualiza estado del subpedido pero no toca lineas_pedido."""
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        antes = await (await db.execute(
            "SELECT cantidad_entregada FROM lineas_pedido WHERE id_pedido = 'TEST-001'"
        )).fetchall()

    solo_estado = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "en alistamiento"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(solo_estado, db_path)
    async with aiosqlite.connect(db_path) as db:
        despues = await (await db.execute(
            "SELECT cantidad_entregada FROM lineas_pedido WHERE id_pedido = 'TEST-001'"
        )).fetchall()
    assert antes == despues


@pytest.mark.integration
async def test_solo_estado_marca_pedido_cerrado(db_path, pedido_sin_diferencias):
    """Cuando todos los subpedidos pasan a estado cerrado, scraping_completo=1."""
    await persistir_uno(pedido_sin_diferencias, db_path)
    cierre = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "completado"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(cierre, db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT scraping_completo FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone()
    assert row[0] == 1


@pytest.mark.integration
async def test_pedido_cerrado_no_aparece_en_ids_activos(db_path, pedido_sin_diferencias):
    """Regla de negocio central: un pedido cerrado no se reprocesa.

    Esta es la query que usa el modo incremental para obtener ids_activos.
    Un pedido con todos los subpedidos cerrados no debe aparecer.
    """
    await persistir_uno(pedido_sin_diferencias, db_path)
    cierre = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "completado"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(cierre, db_path)
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("""
            SELECT DISTINCT p.id_pedido
            FROM pedidos p
            JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 1
              AND LOWER(s.estado) NOT IN ('completado','cancelado','comentado')
        """)).fetchall()
    ids_activos = [r[0] for r in rows]
    assert "TEST-001" not in ids_activos


@pytest.mark.integration
async def test_actualizado_en_refrescado_con_cantidades(db_path, pedido_sin_diferencias):
    p = copy.deepcopy(pedido_sin_diferencias)
    p["subpedidos"][0]["estado"] = "completado"
    p["subpedidos"][0]["lineas"][0]["cantidad_entregada"] = 10.0
    await persistir_uno(p, db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE subpedidos SET cantidades_definitivas = 0 WHERE id_pedido = 'TEST-001'"
        )
        await db.commit()

    con_cant = {
        "tipo": "con_cantidades",
        "id_pedido": "TEST-001",
        "subpedidos": [{
            "numero_subpedido": "SUB-001",
            "tipo_subpedido": "Normal",
            "estado": "completado",
            "inicio_alistamiento": "2026-05-22 08:00:00",
            "alistamiento_completado": "2026-05-22 09:00:00",
            "alistador": "Alistador Test",
            "inicio_inspeccion": "",
            "inspeccion_completada": "",
            "inspector": "",
            "lineas": [{
                "nombre_producto": "Producto Test",
                "referencia": "REF-001",
                "codigo_barras": "7700000000001",
                "presentacion": "Unidad",
                "almacen": "Almacén Principal",
                "cantidad_comprada": 10.0,
                "cantidad_entregada": 10.0,
                "precio_unitario": "10.000,00",
                "descuento": "0,00",
                "precio_descuento": "10.000,00",
                "monto_pagar": "100.000,00",
                "monto_final": "100.000,00",
                "iva": "0,00",
                "peso_total": "1,00",
                "observaciones": "",
                "numero_caja": "",
                "tipo": "",
            }],
        }],
        "timeline": [],
        "info_entrega": {"despachador": "", "hora_entrega": "", "obs_entrega": "",
                         "entrega_ruta_tag": "", "entrega_descuento_tag": ""},
        "estadisticas": [],
        "hay_diferencia": 0,
        "gestion_dif": None,
        "detalle_dif": [],
        "registro_ops": [],
    }
    await persistir_uno(con_cant, db_path)

    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT actualizado_en FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone()
    assert row is not None
    assert row[0] is not None
    assert "T" in row[0] or "2026-" in row[0]


@pytest.mark.integration
async def test_update_sin_match_codigo_barras_vacio(db_path, pedido_sin_diferencias):
    p = copy.deepcopy(pedido_sin_diferencias)
    p["id_pedido"] = "TEST-BAR-EMPTY"
    p["info_general"]["id_pedido"] = "TEST-BAR-EMPTY"
    p["subpedidos"][0]["lineas"][0]["codigo_barras"] = ""
    await persistir_uno(p, db_path)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE subpedidos SET cantidades_definitivas = 0 WHERE id_pedido = 'TEST-BAR-EMPTY'"
        )
        await db.commit()

    con_cant = {
        "tipo": "con_cantidades",
        "id_pedido": "TEST-BAR-EMPTY",
        "subpedidos": [{
            "numero_subpedido": "SUB-001",
            "tipo_subpedido": "Normal",
            "estado": "completado",
            "inicio_alistamiento": "",
            "alistamiento_completado": "",
            "alistador": "",
            "inicio_inspeccion": "",
            "inspeccion_completada": "",
            "inspector": "",
            "lineas": [{
                "nombre_producto": "Producto Test",
                "referencia": "REF-001",
                "codigo_barras": "",
                "presentacion": "Unidad",
                "almacen": "Almacén Principal",
                "cantidad_comprada": 10.0,
                "cantidad_entregada": 10.0,
                "precio_unitario": "10.000,00",
                "descuento": "0,00",
                "precio_descuento": "10.000,00",
                "monto_pagar": "100.000,00",
                "monto_final": "100.000,00",
                "iva": "0,00",
                "peso_total": "1,00",
                "observaciones": "",
                "numero_caja": "",
                "tipo": "",
            }],
        }],
        "timeline": [],
        "info_entrega": {"despachador": "", "hora_entrega": "", "obs_entrega": "",
                         "entrega_ruta_tag": "", "entrega_descuento_tag": ""},
        "estadisticas": [],
        "hay_diferencia": 0,
        "gestion_dif": None,
        "detalle_dif": [],
        "registro_ops": [],
    }

    await persistir_uno(con_cant, db_path)

    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT id_pedido FROM pedidos WHERE id_pedido = 'TEST-BAR-EMPTY'"
        )).fetchone()
    assert row is not None
    assert row[0] == "TEST-BAR-EMPTY"
