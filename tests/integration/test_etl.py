import asyncio
import json
import logging

import aiosqlite
import pytest

from etl.etl_principal import (
    _VIEWS_RETIRADAS,
    crear_views,
    normalizar_montos,
    separar_concepto_tasa,
    verificar_pedidos_sin_subpedidos,
)


def _eventos_etl(caplog):
    """Parsea los registros JSONL del logger 'etl' capturados por caplog."""
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "etl"]


# View sobre la que se prueba la atomicidad de DEC-019: tiene que ser una
# que exista y se recree, no una de `_VIEWS_RETIRADAS` (esas se dropean
# primero y no existen, así que la sonda pasaría sin probar nada).
_SONDA = "v_inventario_comprometido"


@pytest.mark.integration
async def test_columnas_num_creadas(db_path):
    """Las 23 columnas _num existen tras normalizar."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        cols_lp = {
            c[1] for c in await (await db.execute("PRAGMA table_info(lineas_pedido)")).fetchall()
        }
        cols_em = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(estadisticas_monto)")).fetchall()
        }
        cols_gd = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(gestion_diferencias)")).fetchall()
        }
        cols_dd = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(detalle_diferencias)")).fetchall()
        }

    for col in [
        "precio_unitario_num",
        "descuento_num",
        "precio_descuento_num",
        "monto_pagar_num",
        "monto_final_num",
        "iva_num",
        "peso_total_num",
    ]:
        assert col in cols_lp, f"{col} falta en lineas_pedido"

    for col in ["monto_pagar_num", "monto_final_num", "diferencia_num"]:
        assert col in cols_em, f"{col} falta en estadisticas_monto"

    for col in [
        "total_pagar_pedido_num",
        "monto_final_pagar_num",
        "monto_pagado_num",
        "monto_diferencia_num",
    ]:
        assert col in cols_gd, f"{col} falta en gestion_diferencias"

    for col in [
        "precio_unitario_num",
        "descuento_num",
        "precio_descuento_num",
        "cantidad_pedido_num",
        "cantidad_entregada_num",
        "diferencia_cantidad_num",
        "monto_pagar_pedido_num",
        "monto_final_pagar_num",
        "iva_num",
        "monto_diferencia_num",
    ]:
        assert col in cols_dd, f"{col} falta en detalle_diferencias"


@pytest.mark.integration
async def test_normalizacion_es_idempotente(db_path):
    """Ejecutar normalizar_montos dos veces no genera errores."""
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        await normalizar_montos(db)


@pytest.mark.integration
async def test_views_creadas(db_path):
    """Las 12 VIEWs existen tras normalizar y crear_views.

    normalizar_montos debe ejecutarse primero porque
    v_diferencias_resumen y v_descuentos_lineas referencian columnas
    _num de gestion_diferencias/lineas_pedido.
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='view'")
        views = {r[0] for r in await cursor.fetchall()}

    views_esperadas = {
        # 3 VIEWs analíticas (eran 8 hasta DEC-065)
        "v_inventario_comprometido",
        "v_diferencias_resumen",
        "v_descuentos_lineas",
        # 4 VIEWs para el dashboard (montos _num con nombres limpios)
        "v_lineas_pedido_num",
        "v_estadisticas_monto_num",
        "v_gestion_diferencias_num",
        "v_detalle_diferencias_num",
    }
    assert views_esperadas.issubset(views), f"VIEWs faltantes: {views_esperadas - views}"


@pytest.mark.integration
async def test_views_retiradas_se_dropean(db_path):
    """DEC-065: quitar una entrada del dict no basta — `crear_views()` solo
    dropea lo que está a punto de recrear, así que una view retirada
    seguiría viva en las bases existentes si nadie la dropea explícitamente.

    Se simula una base legada creando las 5 a mano antes de correr el ETL.
    """
    async with aiosqlite.connect(db_path) as db:
        for nombre in _VIEWS_RETIRADAS:
            await db.execute(f"CREATE VIEW {nombre} AS SELECT 1 AS x")
        await db.commit()

        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='view'")
        antes = {r[0] for r in await cursor.fetchall()}
        assert set(_VIEWS_RETIRADAS) <= antes, "el fixture no simuló la base legada"

        await normalizar_montos(db)
        await crear_views(db)

        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='view'")
        despues = {r[0] for r in await cursor.fetchall()}

    sobrevivientes = set(_VIEWS_RETIRADAS) & despues
    assert not sobrevivientes, f"views retiradas que quedaron vivas: {sobrevivientes}"


@pytest.mark.integration
async def test_views_son_idempotentes(db_path):
    """Ejecutar crear_views dos veces produce exactamente 7 VIEWs.

    7 = 3 analíticas + 4 del dashboard (eran 12 hasta DEC-065).
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)
        await crear_views(db)
        count = (
            await (
                await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'")
            ).fetchone()
        )[0]
    assert count == 7


# ── FIX C-4 (auditoría 2026-07-01) — COALESCE en cantidad_pendiente ────────────


@pytest.mark.integration
async def test_inventario_pendiente_incluye_entregada_null(db_path):
    """FIX C-4: una línea con cantidad_entregada NULL (aún sin entrega
    registrada) aporta su cantidad_comprada completa a cantidad_pendiente.

    Antes del fix, x - NULL = NULL y SUM ignoraba la fila entera:
    el KPI "Pendiente" subcontaba exactamente las líneas pendientes.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-C4', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO subpedidos "
            "(id_pedido, numero_subpedido, tipo_subpedido, estado) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Normal', 'Pendiente de entrega')"
        )
        # Línea 1: sin entrega registrada (NULL) — debe aportar 10 pendientes
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, referencia, "
            "codigo_barras, presentacion, almacen, "
            "cantidad_comprada, cantidad_entregada) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Producto C4', 'REF-C4', "
            "'7700000000C4', 'Unidad', 'Almacén Test', 10.0, NULL)"
        )
        # Línea 2: entrega parcial — debe aportar 6 pendientes
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, referencia, "
            "codigo_barras, presentacion, almacen, "
            "cantidad_comprada, cantidad_entregada) "
            "VALUES ('TEST-C4', 'SUB-C4', 'Producto C4', 'REF-C4', "
            "'7700000000C4', 'Unidad', 'Almacén Test', 10.0, 4.0)"
        )
        await db.commit()

        await normalizar_montos(db)  # ← obligatorio primero
        await crear_views(db)

        row = await (
            await db.execute(
                "SELECT cantidad_comprometida_total, cantidad_entregada_total, "
                "cantidad_pendiente "
                "FROM v_inventario_comprometido WHERE referencia = 'REF-C4'"
            )
        ).fetchone()

    assert row is not None, "la línea no entró a v_inventario_comprometido"
    assert row[0] == 20.0  # 10 + 10 compradas
    assert row[1] == 4.0  # solo la entrega registrada
    assert row[2] == 16.0  # 10 (NULL→0) + 6 — antes del fix daba 6


# ── DEC-021 Fase 6 — pedidos sin subpedidos: invisibles pero reportados ────────


@pytest.mark.integration
async def test_pedido_sin_subpedidos_invisible_pero_reportado(db_path, caplog):
    """DEC-021: un pedido completo sin subpedidos es invisible para
    cualquier agregación por subpedido (el INNER JOIN lo descarta), y el
    check defensivo lo hace visible con etl_pedido_sin_subpedidos
    (WARNING, total + muestra_ids).

    DEC-065: la invisibilidad se comprueba contra el mismo INNER JOIN que
    usaban v_pedidos_activos/cerrados, ya retiradas. Lo que el check
    protege no cambió — el pedido sigue sin aparecer.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) VALUES "
            "('TEST-SIN-SUBS', '2026-06-01', 1), ('TEST-NORMAL', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO subpedidos "
            "(id_pedido, numero_subpedido, tipo_subpedido, estado) "
            "VALUES ('TEST-NORMAL', 'SUB-1', 'Normal', 'enviado')"
        )
        await db.commit()
        await normalizar_montos(db)
        await crear_views(db)

        async with db.execute(
            "SELECT p.id_pedido FROM pedidos p "
            "JOIN subpedidos s ON p.id_pedido = s.id_pedido "
            "WHERE p.scraping_completo = 1 GROUP BY p.id_pedido"
        ) as cur:
            visibles = {r[0] for r in await cur.fetchall()}

        with caplog.at_level(logging.INFO, logger="etl"):
            n = await verificar_pedidos_sin_subpedidos(db)

    assert "TEST-SIN-SUBS" not in visibles
    assert "TEST-NORMAL" in visibles  # el normal sí es visible

    assert n == 1
    evento = next(e for e in _eventos_etl(caplog) if e["event"] == "etl_pedido_sin_subpedidos")
    assert evento["level"] == "WARNING"
    assert evento["total"] == 1
    assert evento["muestra_ids"] == ["TEST-SIN-SUBS"]


@pytest.mark.integration
async def test_sin_pedidos_anomalos_no_emite_warning(db_path, caplog):
    """DEC-021: sin anomalías, el check retorna 0 y no emite evento."""
    async with aiosqlite.connect(db_path) as db:
        with caplog.at_level(logging.INFO, logger="etl"):
            n = await verificar_pedidos_sin_subpedidos(db)
    assert n == 0
    eventos = [e for e in _eventos_etl(caplog) if e["event"] == "etl_pedido_sin_subpedidos"]
    assert eventos == []


# ── DEC-025 — placeholders: '-' es cero solo en descuento ─────────────────────


@pytest.mark.integration
async def test_placeholders_guion_cero_solo_en_descuento(db_path, caplog):
    """DEC-025: en `descuento` el guion vale 0; en `precio_descuento`
    (misma tabla, misma fila) queda NULL porque significa 'sin descuento'.

    Convertirlo a 0 ahí pondría el precio en cero — el error que la
    decisión evita explícitamente.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-PH', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO subpedidos (id_pedido, numero_subpedido, tipo_subpedido, estado) "
            "VALUES ('TEST-PH', 'SUB-PH', 'Normal', 'enviado')"
        )
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, precio_unitario, "
            " descuento, precio_descuento, peso_total) "
            "VALUES ('TEST-PH', 'SUB-PH', 'Producto PH', 'COP 7.200', "
            "'-', '-', 'g')"
        )
        await db.commit()

        with caplog.at_level(logging.INFO, logger="etl"):
            await normalizar_montos(db)

        async with db.execute(
            "SELECT descuento_num, precio_descuento_num, precio_unitario_num, peso_total_num "
            "FROM lineas_pedido WHERE id_pedido = 'TEST-PH'"
        ) as cur:
            desc, precio_desc, precio_unit, peso = await cur.fetchone()

    assert desc == 0.0, "descuento '-' debe ser 0 (regla de negocio)"
    assert precio_desc is None, "precio_descuento '-' NO es cero: es 'sin descuento'"
    assert precio_unit == 7200.0, "los valores reales se convierten igual"
    assert peso is None, "peso 'g' es ausencia (el cero real sería '0g')"

    eventos = _eventos_etl(caplog)
    # El placeholder no genera WARNING de conversión fallida.
    fallidas = [
        e
        for e in eventos
        if e["event"] == "etl_conversion_fallida" and "TEST-PH" not in e.get("msg", "")
    ]
    evento = next(
        e
        for e in eventos
        if e["event"] == "etl_columna_ok"
        and e.get("tabla") == "lineas_pedido"
        and e.get("columna") == "precio_descuento_num"
    )
    assert evento["placeholders"] == 1
    assert evento["fallidas"] == 0
    assert fallidas == []


# ── E-8 Fase 5 — tope de WARNINGs por columna + resumen agregado ───────────────


@pytest.mark.integration
async def test_warnings_fallidas_con_tope_y_resumen(db_path, caplog):
    """E-8 (Fase 5): 13 fallidas → 10 WARNINGs de detalle + 1 resumen.

    El detalle fila a fila se corta en _MAX_DETALLE_FALLIDAS (10); el
    resumen reporta el total real, cuántas salieron detalladas y una
    muestra de hasta 10 valores distintos — la telemetría de AUD-M1.
    """
    valores = ", ".join(f"('TEST-E8', 'ERR-{i}')" for i in range(1, 14))
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-E8', '2026-06-01', 1)"
        )
        await db.execute(
            f"INSERT INTO estadisticas_monto (id_pedido, monto_pagar) VALUES {valores}"
        )
        await db.commit()
        with caplog.at_level(logging.INFO, logger="etl"):
            await normalizar_montos(db)

    eventos = _eventos_etl(caplog)
    detalles = [
        e
        for e in eventos
        if e["event"] == "etl_conversion_fallida"
        and e["msg"].startswith("estadisticas_monto.monto_pagar id=")
    ]
    assert len(detalles) == 10, "el detalle fila a fila debe cortarse en 10"

    resumen = next(
        e
        for e in eventos
        if e["event"] == "etl_conversion_resumen"
        and e.get("tabla") == "estadisticas_monto"
        and e.get("columna") == "monto_pagar_num"
    )
    assert resumen["level"] == "WARNING"
    assert resumen["fallidas"] == 13
    assert resumen["detalladas"] == 10
    assert len(resumen["muestra_valores"]) == 10  # 13 distintos, muestra al tope


@pytest.mark.integration
async def test_resumen_ausente_sin_fallidas(db_path, caplog):
    """E-8 (Fase 5): sin conversiones fallidas no se emite resumen."""
    async with aiosqlite.connect(db_path) as db:
        with caplog.at_level(logging.INFO, logger="etl"):
            await normalizar_montos(db)
    resumenes = [e for e in _eventos_etl(caplog) if e["event"] == "etl_conversion_resumen"]
    assert resumenes == []


# ── E-1 (revisión ETL 2026-07-16) — except estrecho en ALTER TABLE ─────────────


@pytest.mark.integration
async def test_normalizar_montos_tabla_inexistente_propaga(tmp_path):
    """E-1/E-8: una DB sin el schema del scraper falla con causa clara.

    Antes de E-1, el except del ALTER tragaba "no such table" y el error
    aparecía desconectado de la causa en el SELECT del poblado. Desde la
    Fase 4 (E-8), la validación previa de schema falla aún antes, con un
    RuntimeError que nombra la tabla ausente.
    """
    path = str(tmp_path / "sin_schema.db")
    async with aiosqlite.connect(path) as db:
        with pytest.raises(RuntimeError, match="lineas_pedido.*no existe"):
            await normalizar_montos(db)


@pytest.mark.integration
async def test_normalizar_montos_columna_fuente_inexistente_propaga(tmp_path):
    """E-8: una columna fuente renombrada falla con un error que la nombra.

    Simula un rename del scraper: lineas_pedido existe pero solo con
    precio_unitario. La validación debe fallar ANTES del poblado
    nombrando las columnas faltantes, no con un OperationalError opaco.
    """
    path = str(tmp_path / "schema_incompleto.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE lineas_pedido (id INTEGER PRIMARY KEY, precio_unitario TEXT)"
        )
        await db.commit()
        with pytest.raises(RuntimeError, match="descuento"):
            await normalizar_montos(db)


@pytest.mark.integration
async def test_lector_concurrente_no_ve_ventana_sin_views(db_path):
    """DEC-019 (E-2): la recreación de views es atómica para lectores.

    Pausa crear_views justo después del DROP de una view que **existe** y
    va a recrearse (hook sobre db.execute — el instante exacto de la
    ventana de HAL-007) y la consulta desde una segunda conexión. Con la
    transacción explícita, el lector ve el snapshot anterior de WAL y la
    view sigue existiendo. Sin el fix, el DROP era autocommit y este test
    falla con "no such view".

    DEC-065: el hook apunta a `_SONDA` y no "al primer DROP". Los DROP de
    `_VIEWS_RETIRADAS` corren primero y sobre views que no existen, así
    que pausar ahí haría pasar el test sin probar nada.
    """
    async with aiosqlite.connect(db_path) as db:
        await normalizar_montos(db)
        await crear_views(db)  # estado inicial: las 7 views existen

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        orig_execute = db.execute
        primer_drop_hecho = asyncio.Event()
        sonda_terminada = asyncio.Event()

        def execute_pausado(sql, *args, **kwargs):
            async def _run():
                cursor = await orig_execute(sql, *args, **kwargs)
                if sql.startswith(f"DROP VIEW IF EXISTS {_SONDA}"):
                    primer_drop_hecho.set()
                    await sonda_terminada.wait()
                return cursor

            return _run()

        db.execute = execute_pausado  # type: ignore[method-assign]
        tarea = asyncio.create_task(crear_views(db))
        try:
            await asyncio.wait_for(primer_drop_hecho.wait(), timeout=10)

            # DROP ejecutado, transacción sin commit: el lector concurrente
            # debe seguir viendo la view.
            async with aiosqlite.connect(db_path) as lector:
                await lector.execute("PRAGMA busy_timeout=5000")
                row = await (
                    await lector.execute(f"SELECT COUNT(*) FROM {_SONDA}")  # noqa: S608
                ).fetchone()
            assert row is not None
        finally:
            sonda_terminada.set()
            await asyncio.wait_for(tarea, timeout=30)
            db.execute = orig_execute  # type: ignore[method-assign]

        # Tras el COMMIT, las 7 views recreadas quedan visibles.
        count = (
            await (
                await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'")
            ).fetchone()
        )[0]
    assert count == 7


# ── E-3 + E-4 / DEC-020 — métricas veraces y fin del retrabajo ─────────────────


@pytest.mark.integration
async def test_etl_columna_ok_conteos_veraces(db_path, caplog):
    """E-3: convertidas / fallidas / fuente_null cuentan lo que dicen.

    2 valores convertibles, 1 inconvertible ("N/A") y 3 con fuente NULL
    deben reportarse en el contador correcto del evento etl_columna_ok,
    y los _num escritos deben ser exactamente los 2 convertidos.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-E3', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO estadisticas_monto (id_pedido, monto_pagar) VALUES "
            "('TEST-E3', '1.234,56'), ('TEST-E3', 'COP 200'), "
            "('TEST-E3', 'N/A'), "
            "('TEST-E3', NULL), ('TEST-E3', NULL), ('TEST-E3', NULL)"
        )
        await db.commit()

        with caplog.at_level(logging.INFO, logger="etl"):
            await normalizar_montos(db)

        valores = await (
            await db.execute(
                "SELECT monto_pagar, monto_pagar_num FROM estadisticas_monto "
                "WHERE id_pedido = 'TEST-E3' ORDER BY id"
            )
        ).fetchall()

    evento = next(
        e
        for e in _eventos_etl(caplog)
        if e["event"] == "etl_columna_ok"
        and e.get("tabla") == "estadisticas_monto"
        and e.get("columna") == "monto_pagar_num"
    )
    assert evento["convertidas"] == 2
    assert evento["fallidas"] == 1
    assert evento["fuente_null"] == 3

    assert valores[0] == ("1.234,56", 1234.56)
    assert valores[1] == ("COP 200", 200.0)
    assert valores[2] == ("N/A", None)  # fallida: queda NULL, sin centinela
    assert all(v == (None, None) for v in valores[3:])


@pytest.mark.integration
async def test_fuente_null_no_se_reescanea_en_segunda_corrida(db_path, caplog):
    """E-4/DEC-020: la segunda corrida no retrabaja lo irresoluble.

    Tras una primera corrida, la segunda no ejecuta ningún UPDATE (espía
    sobre db.execute): las filas con fuente NULL quedan fuera del SELECT
    y la fallida genuina se re-escanea sin escribir NULL sobre NULL.
    Con el código previo al fix, esta corrida ejecutaba 3 UPDATEs inútiles.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-E4', '2026-06-01', 1)"
        )
        await db.execute(
            "INSERT INTO estadisticas_monto (id_pedido, monto_pagar) VALUES "
            "('TEST-E4', '100'), ('TEST-E4', 'N/A'), "
            "('TEST-E4', NULL), ('TEST-E4', NULL)"
        )
        await db.commit()
        await normalizar_montos(db)  # primera corrida: convierte el '100'

        # Segunda corrida instrumentada: espía que registra cada UPDATE.
        updates: list[str] = []
        orig_execute = db.execute

        def execute_espia(sql, *args, **kwargs):
            if sql.lstrip().startswith("UPDATE"):
                updates.append(sql)
            return orig_execute(sql, *args, **kwargs)

        db.execute = execute_espia  # type: ignore[method-assign]
        caplog.clear()
        try:
            with caplog.at_level(logging.INFO, logger="etl"):
                await normalizar_montos(db)
        finally:
            db.execute = orig_execute  # type: ignore[method-assign]

    assert updates == [], f"la segunda corrida ejecutó UPDATEs inútiles: {updates}"

    evento = next(
        e
        for e in _eventos_etl(caplog)
        if e["event"] == "etl_columna_ok"
        and e.get("tabla") == "estadisticas_monto"
        and e.get("columna") == "monto_pagar_num"
    )
    assert evento["convertidas"] == 0  # nada nuevo que convertir
    assert evento["fallidas"] == 1  # la 'N/A' se re-escanea (política DEC-020)
    assert evento["fuente_null"] == 2  # contadas por COUNT, no escaneadas


@pytest.mark.integration
async def test_normalizar_montos_columna_duplicada_no_propaga(db_path):
    """E-1: "duplicate column name" sigue tolerado — idempotencia intacta.

    Pre-agrega una columna _num a mano y verifica que normalizar_montos
    no falla al encontrarla ya existente.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("ALTER TABLE lineas_pedido ADD COLUMN precio_unitario_num REAL")
        await db.commit()
        await normalizar_montos(db)


# ── VIEW de descuentos (deriva de DEC-024) ─────────────────────────────────────


@pytest.mark.integration
async def test_v_descuentos_lineas_solo_lineas_con_descuento_real(db_path):
    """v_descuentos_lineas incluye solo líneas con descuento_tipo poblado
    y expone el monto derivado (precio_unitario_num - precio_descuento_num).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos "
            "(id_pedido, fecha, servicio_cliente, nombre_empresa, nit, vendedor, "
            "scraping_completo) VALUES "
            "('TEST-DESC', '2026-07-01', 'Cliente Test', 'Empresa Test', "
            "'900000000-1', 'Vendedor Test', 1)"
        )
        await db.execute(
            "INSERT INTO subpedidos (id_pedido, numero_subpedido, tipo_subpedido, estado) "
            "VALUES ('TEST-DESC', 'SUB-1', 'Normal', 'enviado')"
        )
        await db.execute(
            "INSERT INTO lineas_pedido "
            "(id_pedido, numero_subpedido, nombre_producto, cantidad_comprada, "
            "precio_unitario, descuento, descuento_tipo, precio_descuento) VALUES "
            "('TEST-DESC', 'SUB-1', 'Con descuento', 2.0, "
            "'COP 10.000', '-', 'Promoción30%', 'COP 7.000'), "
            "('TEST-DESC', 'SUB-1', 'Sin descuento', 3.0, "
            "'COP 5.000', '-', '', 'COP 5.000')"
        )
        await db.commit()

        await normalizar_montos(db)
        await separar_concepto_tasa(db)
        await crear_views(db)

        rows = await (
            await db.execute(
                "SELECT nombre_producto, descuento_tipo, precio_unitario, "
                "precio_descuento, monto_descuento_unitario, monto_descuento_total, "
                "servicio_cliente, nombre_empresa, nit, vendedor "
                "FROM v_descuentos_lineas WHERE id_pedido = 'TEST-DESC'"
            )
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "Con descuento"
    assert row[1] == "Promoción30%"
    assert row[2] == 10000.0
    assert row[3] == 7000.0
    assert row[4] == 3000.0
    assert row[5] == 6000.0  # 3000 * 2 unidades
    assert row[6:] == ("Cliente Test", "Empresa Test", "900000000-1", "Vendedor Test")


# ── Fragmentación de concepto en estadisticas_monto ────────────────────────────


@pytest.mark.integration
async def test_separar_concepto_tasa_iva_con_porcentaje(db_path):
    """'IVA alimentos (5%)' se separa en concepto_base='IVA alimentos'
    y tasa_iva=5.0 — el mismo concepto ya no fragmenta en 3 buckets."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-CONC', '2026-07-01', 1)"
        )
        await db.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto) VALUES "
            "('TEST-CONC', 'IVA alimentos (5%)'), "
            "('TEST-CONC', 'IVA alimentos (0%)'), "
            "('TEST-CONC', 'IVA alimentos (%)'), "
            "('TEST-CONC', 'Flete')"
        )
        await db.commit()

        await separar_concepto_tasa(db)

        rows = await (
            await db.execute(
                "SELECT concepto, concepto_base, tasa_iva FROM estadisticas_monto "
                "WHERE id_pedido = 'TEST-CONC' ORDER BY id"
            )
        ).fetchall()

    assert rows[0] == ("IVA alimentos (5%)", "IVA alimentos", 5.0)
    assert rows[1] == ("IVA alimentos (0%)", "IVA alimentos", 0.0)
    assert rows[2] == ("IVA alimentos (%)", "IVA alimentos", None)
    assert rows[3] == ("Flete", "Flete", None)


@pytest.mark.integration
async def test_separar_concepto_tasa_es_idempotente(db_path):
    """Ejecutar separar_concepto_tasa dos veces no genera errores ni
    re-escribe filas ya pobladas."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) "
            "VALUES ('TEST-CONC2', '2026-07-01', 1)"
        )
        await db.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto) VALUES "
            "('TEST-CONC2', 'IVA accesorios (19%)')"
        )
        await db.commit()
        await separar_concepto_tasa(db)
        await separar_concepto_tasa(db)

        row = await (
            await db.execute(
                "SELECT concepto_base, tasa_iva FROM estadisticas_monto "
                "WHERE id_pedido = 'TEST-CONC2'"
            )
        ).fetchone()
    assert row == ("IVA accesorios", 19.0)


# -- DEC-087: clave de paginacion por tabla --------------------------------


@pytest.mark.integration
async def test_normaliza_una_tabla_con_clave_texto(db_path):
    """`pedidos` usa `id_pedido` TEXT como PK, no el `id` surrogate que la
    paginacion por keyset asumia. Con mas de 500 filas se ejercita el bucle
    completo: si el keyset estuviera roto, la segunda pagina no avanzaria y
    quedarian filas sin convertir (o el bucle no terminaria).
    """
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO pedidos (id_pedido, fecha, pago_total, pago_pagado, pago_saldo) "
            "VALUES (?,?,?,?,?)",
            [(f"P{n:05d}", "2026-07-20", "COP 1.000", "COP 400", "COP 600") for n in range(1200)],
        )
        await db.commit()

        await normalizar_montos(db)

        async with db.execute(
            "SELECT COUNT(*), SUM(pago_saldo_num) FROM pedidos WHERE pago_saldo_num IS NOT NULL"
        ) as cur:
            n, suma = await cur.fetchone()

    assert n == 1200
    assert suma == 1200 * 600


@pytest.mark.integration
async def test_las_tablas_con_id_surrogate_no_cambiaron(db_path):
    """El default sigue siendo `id`: el cambio de DEC-087 no debe alterar el
    comportamiento de las cuatro tablas originales."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO pedidos (id_pedido, fecha) VALUES ('Q1','2026-07-20')")
        await db.executemany(
            "INSERT INTO registros_pago (id_pedido, secuencia, monto_comprobante, monto_pago) "
            "VALUES (?,?,?,?)",
            [("Q1", str(n), "COP 5.000", "COP 1.250") for n in range(600)],
        )
        await db.commit()

        await normalizar_montos(db)

        async with db.execute(
            "SELECT COUNT(*), SUM(monto_pago_num) FROM registros_pago "
            "WHERE monto_pago_num IS NOT NULL"
        ) as cur:
            n, suma = await cur.fetchone()

    assert n == 600
    assert suma == 600 * 1250
