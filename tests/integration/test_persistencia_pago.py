"""Persistencia de «Operación de pago» y «Registros de pago» — DEC-087.

Lo que se fija acá es el comportamiento que separa esta sección del resto:
la SPA la agregó el 2026-07-16, así que **no renderiza en pedidos viejos**, y
una extracción vacía tiene que preservar lo que ya había en vez de pisarlo.
"""

import copy

import aiosqlite
import pytest

from tests.integration.test_persistencia import _PEDIDO_BASE, persistir_uno

_REGISTRO = {
    "id_pedido": "TEST-001",
    "secuencia": "1",
    "metodo_pago": "Transferencia",
    "cuenta_receptora": "Banco X 123",
    "monto_comprobante": "COP 5.000.000",
    "monto_pago": "COP 1.937.658",
    "hora_pago": "2026-07-30 10:00:00",
    "comprobante": "144357",
    "fecha_envio": "2026-07-30 10:05:00",
    "estado_revision": "Aprobado",
    "fecha_revision": "2026-07-30 11:20:00",
    "revisor": "ana",
    "observaciones": "",
}

_TARJETA = {
    "pago_estado": "Pagado",
    "pago_total": "COP 1.937.658",
    "pago_pagado": "COP 1.937.658",
    "pago_saldo": "COP 0",
    "pago_progreso": "100%",
}


def _con_pago(**extra) -> dict:
    r = copy.deepcopy(_PEDIDO_BASE)
    r.update(extra)
    return r


@pytest.mark.integration
async def test_persiste_la_tarjeta_y_los_comprobantes(db_path):
    await persistir_uno(_con_pago(operacion_pago=_TARJETA, registros_pago=[_REGISTRO]), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT pago_estado, pago_saldo FROM pedidos WHERE id_pedido='TEST-001'"
        ) as cur:
            assert await cur.fetchone() == ("Pagado", "COP 0")
        async with db.execute(
            "SELECT cuenta_receptora, estado_revision, revisor FROM registros_pago"
        ) as cur:
            assert await cur.fetchone() == ("Banco X 123", "Aprobado", "ana")


@pytest.mark.integration
async def test_los_dos_montos_se_guardan_por_separado(db_path):
    """DEC-087: es lo que permite falsar la hipótesis de que un comprobante
    cubra varios pedidos — la explicación candidata de los pagos de 107 veces
    el pedido que DEC-084 tuvo que apartar."""
    await persistir_uno(_con_pago(registros_pago=[_REGISTRO]), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT monto_comprobante, monto_pago FROM registros_pago") as cur:
            comprobante, pago = await cur.fetchone()

    assert comprobante == "COP 5.000.000"
    assert pago == "COP 1.937.658"


@pytest.mark.integration
async def test_una_fila_incompleta_no_tumba_la_persistencia(db_path):
    """`executemany` con parámetros nombrados revienta si falta una clave, y
    al extractor le faltan las columnas que el origen no renderice. Es la
    misma forma de falla que DEC-074, donde una nota al pie del layout mató
    la corrida entera."""
    await persistir_uno(
        _con_pago(registros_pago=[{"id_pedido": "TEST-001", "secuencia": "7"}]), db_path
    )

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT secuencia, revisor FROM registros_pago") as cur:
            assert await cur.fetchone() == ("7", "")


@pytest.mark.integration
async def test_la_tarjeta_ausente_no_borra_lo_ya_capturado(db_path):
    """EL test de esta sección. Un re-scrape de un pedido anterior al
    2026-07-16 no encuentra la tarjeta; si la persistencia pisara con vacío,
    cada corrida del scheduler destruiría lo capturado."""
    await persistir_uno(_con_pago(operacion_pago=_TARJETA), db_path)
    await persistir_uno(_con_pago(operacion_pago=None), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT pago_estado, pago_saldo FROM pedidos WHERE id_pedido='TEST-001'"
        ) as cur:
            assert await cur.fetchone() == ("Pagado", "COP 0")


@pytest.mark.integration
async def test_los_comprobantes_ausentes_tampoco_borran(db_path):
    await persistir_uno(_con_pago(registros_pago=[_REGISTRO]), db_path)
    await persistir_uno(_con_pago(registros_pago=[]), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM registros_pago") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.integration
async def test_reprocesar_no_duplica_comprobantes(db_path):
    """El scraper es idempotente por diseño y el scheduler corre cada hora.
    Sin el DELETE condicional previo al INSERT, cada corrida acumularía."""
    for _ in range(3):
        await persistir_uno(_con_pago(registros_pago=[_REGISTRO]), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM registros_pago") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.integration
async def test_el_indice_unico_existe(db_path):
    """AUD-B9: el invariante «un pedido no repite secuencia» queda escrito en
    el esquema, no en la buena voluntad del próximo INSERT."""
    await persistir_uno(_con_pago(), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_registros_pago_unico'"
        ) as cur:
            assert await cur.fetchone() is not None


@pytest.mark.integration
async def test_varios_comprobantes_del_mismo_pedido_conviven(db_path):
    """El caso real del DOM compartido: 9 comprobantes en un solo pedido."""
    registros = [{**_REGISTRO, "secuencia": str(n)} for n in range(1, 10)]
    await persistir_uno(_con_pago(registros_pago=registros), db_path)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM registros_pago") as cur:
            assert (await cur.fetchone())[0] == 9
