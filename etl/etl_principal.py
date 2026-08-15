"""
ETL principal — dashboard_pedidos
Normaliza montos TEXT a REAL y crea VIEWs analíticas
sobre data/pedidos.db.

Ejecutar desde la raíz del proyecto como módulo (E-7, requiere la
instalación editable de DEC-018):

    python -m etl.etl_principal
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone

import aiosqlite

# AUD-M5 (auditoría 2026-07-01): importar del módulo común — el ETL ya no
# carga el scraper (ni Playwright, ni sus efectos secundarios de import).
from comun import (
    COLUMNAS_GUION_ES_CERO,
    DDL_ERROR_COLUMNA_DUPLICADA,
    ESTADOS_CONOCIDOS,
    ejecutar_ddl_idempotente,
    es_placeholder,
    get_db_path,
    normalizar_numerico,
)

logger = logging.getLogger("etl")

# E-8 (Fase 5): tope de WARNINGs fila a fila por columna y por corrida —
# un formato masivo nuevo en el origen ya no genera miles de líneas; el
# excedente lo agrega etl_conversion_resumen con muestra de valores.
_MAX_DETALLE_FALLIDAS = 10
_MAX_MUESTRA_FALLIDAS = 10


# AUD-M8 (auditoría 2026-07-01): literales SQL generados desde las
# constantes del módulo común — único origen de verdad para
# "cerrado"/"activo" en las VIEWs.
#
# DEC-065: quedó uno solo. `_CERRADOS_SQL` y `_ACCIONES_RENDIMIENTO_SQL`
# existían para interpolarse en las views retiradas (ver `_VIEWS_RETIRADAS`)
# y se fueron con ellas.

# DEC-065: views que el ETL creó en el pasado y ya no crea. Hay que
# **dropearlas explícitamente**: `crear_views()` solo dropea lo que está a
# punto de recrear, así que quitar una entrada del dict la dejaría viva en
# `pedidos.db` para siempre, sin nadie que la mantenga.
#
# Por qué se retiró cada una está en DEC-065. En resumen: las dos de
# pedidos hardcodeaban la lista de estados que `comun/` posee (discreparían
# en silencio el día que cambie), `v_rendimiento_operadores` publicaba una
# comparación entre personas que DEC-054 midió como inválida, y las dos de
# variaciones eran vocabulario de exploración (30 filas entre ambas).
#
# Se puede vaciar esta tupla cuando no queden bases con esas views. No
# corre riesgo de borrar de más: `DROP VIEW` no toca tablas.
_VIEWS_RETIRADAS = (
    # DEC-103: `v_inventario_comprometido` no era código muerto sino una VIEW
    # que **no puede dar bien lo que promete**. Calcula
    # `cantidad_comprada − cantidad_entregada` sobre subpedidos **activos**, y
    # el origen puebla las dos iguales mientras el subpedido está abierto: 99,3%
    # de las líneas, con `cantidades_definitivas = 0` en 2.081 de 2.085. Reporta
    # **251 unidades** cuando lo comprometido son **497.929** (DEC-098).
    # `dashboard.db.get_comprometido()` la reemplaza usando `cantidad_comprada`.
    "v_inventario_comprometido",
    "v_pedidos_activos",
    "v_pedidos_cerrados",
    "v_rendimiento_operadores",
    "v_variaciones_timeline",
    "v_variaciones_operaciones",
)


def _log_event(
    event: str,
    level: str = "INFO",
    msg: str = "",
    **kwargs,
) -> None:
    """Emite una línea JSONL a stderr con schema consistente con el scraper.

    Args:
        event: Categoría semántica del evento (snake_case).
        level: Nivel de log: INFO, WARNING o ERROR.
        msg: Descripción textual del evento.
        **kwargs: Campos adicionales a incluir en el JSON.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "msg": msg,
        "modulo": "etl",
        **kwargs,
    }
    line = json.dumps(record, ensure_ascii=False)
    if level == "ERROR":
        logger.error(line)
    elif level == "WARNING":
        logger.warning(line)
    else:
        logger.info(line)


async def normalizar_montos(db: aiosqlite.Connection) -> None:
    """Agrega columnas _num REAL y las puebla con to_num().

    Valida primero que las tablas y columnas fuente existen
    (E-8: un rename del scraper falla con causa clara, no como
    OperationalError a mitad del poblado). Usa ALTER TABLE con
    try/except por columna para idempotencia. Puebla en batches
    de 500 filas procesando solo las filas donde _num IS NULL y
    la columna origen no es NULL (E-4/DEC-020). Las filas cuya
    conversión falla quedan _num NULL sin UPDATE y se re-escanean
    deliberadamente en cada corrida (política DEC-020).

    Args:
        db: Conexión abierta a pedidos.db.

    Raises:
        RuntimeError: si una tabla o columna fuente esperada
            no existe en la DB.
    """
    # E-8: dict explícito columna origen → columna _num destino. Antes se
    # derivaba con col_num[:-4] — un typo producía un error SQL opaco.
    columnas_por_tabla: dict[str, dict[str, str]] = {
        "lineas_pedido": {
            "precio_unitario": "precio_unitario_num",
            "descuento": "descuento_num",
            "precio_descuento": "precio_descuento_num",
            "monto_pagar": "monto_pagar_num",
            "monto_final": "monto_final_num",
            "iva": "iva_num",
            "peso_total": "peso_total_num",
        },
        "estadisticas_monto": {
            "monto_pagar": "monto_pagar_num",
            "monto_final": "monto_final_num",
            "diferencia": "diferencia_num",
        },
        "gestion_diferencias": {
            "total_pagar_pedido": "total_pagar_pedido_num",
            "monto_final_pagar": "monto_final_pagar_num",
            "monto_pagado": "monto_pagado_num",
            "monto_diferencia": "monto_diferencia_num",
        },
        "detalle_diferencias": {
            "precio_unitario": "precio_unitario_num",
            "descuento": "descuento_num",
            "precio_descuento": "precio_descuento_num",
            "cantidad_pedido": "cantidad_pedido_num",
            "cantidad_entregada": "cantidad_entregada_num",
            "diferencia_cantidad": "diferencia_cantidad_num",
            "monto_pagar_pedido": "monto_pagar_pedido_num",
            "monto_final_pagar": "monto_final_pagar_num",
            "iva": "iva_num",
            "monto_diferencia": "monto_diferencia_num",
        },
        # DEC-087: sin esto los montos de la tarjeta y de los comprobantes
        # quedan como texto "COP X.XXX" y no se pueden sumar ni comparar —
        # que es justamente lo que se necesita para falsar la hipótesis del
        # comprobante compartido entre pedidos.
        "pedidos": {
            "pago_total": "pago_total_num",
            "pago_pagado": "pago_pagado_num",
            "pago_saldo": "pago_saldo_num",
        },
        "registros_pago": {
            "monto_comprobante": "monto_comprobante_num",
            "monto_pago": "monto_pago_num",
        },
    }

    # DEC-087: la paginación por keyset asumía un `id` surrogate, y `pedidos`
    # usa `id_pedido` TEXT como clave primaria. Se declara por tabla en vez de
    # hardcodearse: el default sigue siendo `id`, así que las cuatro tablas
    # originales no cambian de comportamiento.
    clave_por_tabla: dict[str, str] = {"pedidos": "id_pedido"}

    # E-8: validar que tablas y columnas fuente existen ANTES de tocar el
    # schema — un rename en el scraper debe fallar aquí con causa clara,
    # no como OperationalError opaco a mitad del poblado.
    for tabla, columnas in columnas_por_tabla.items():
        async with db.execute(f"PRAGMA table_info({tabla})") as cur:
            cols_reales = {row[1] for row in await cur.fetchall()}
        if not cols_reales:
            raise RuntimeError(
                f"ETL: la tabla '{tabla}' no existe en la DB — ¿cambió el schema del scraper?"
            )
        faltantes = sorted(set(columnas) - cols_reales)
        if faltantes:
            raise RuntimeError(
                f"ETL: columnas fuente inexistentes en '{tabla}': {faltantes} — "
                "¿cambió el schema del scraper?"
            )

    for tabla, columnas in columnas_por_tabla.items():
        # Paso 1: agregar columnas con ALTER TABLE
        for col_num in columnas.values():
            # Solo "duplicate column name" es esperado (idempotencia);
            # cualquier otro error (no such table, DB locked, corrupta)
            # debe fallar aquí y no más adelante en el SELECT.
            await ejecutar_ddl_idempotente(
                db, f"ALTER TABLE {tabla} ADD COLUMN {col_num} REAL", DDL_ERROR_COLUMNA_DUPLICADA
            )

        # Paso 2: poblar en batches de 500 filas.
        # Filtra por col_num IS NULL para procesar únicamente
        # las filas que aún no tienen valor convertido.
        # Esto hace el ETL idempotente y captura correctamente
        # las filas nuevas que deja el scraper en cada ejecución.
        for col_src, col_num in columnas.items():
            # DEC-025: el guion solo significa cero en columnas concretas.
            guion_es_cero = col_src in COLUMNAS_GUION_ES_CERO
            clave = clave_por_tabla.get(tabla, "id")
            # El centinela tiene que ser del mismo tipo que la clave: con
            # `id_pedido` TEXT, un 0 entero compara mal en SQLite (los
            # enteros ordenan ANTES que el texto, así que 0 funcionaría por
            # accidente hoy y rompería el día que la clave sea numérica).
            last_id: object = "" if clave != "id" else 0
            batch_count = 0
            total_escaneadas = 0
            total_convertidas = 0
            total_fallidas = 0
            total_placeholders = 0
            muestra_fallidas: set[str] = set()
            while True:
                # E-4/DEC-020: las filas con fuente NULL se excluyen del
                # escaneo — no hay nada que convertir; su magnitud se
                # reporta con un COUNT al cierre de la columna.
                async with db.execute(
                    f"SELECT {clave}, {col_src} FROM {tabla} "
                    f"WHERE {clave} > ? AND {col_num} IS NULL "
                    f"AND {col_src} IS NOT NULL "
                    f"ORDER BY {clave} LIMIT 500",
                    (last_id,),
                ) as cur:
                    rows = await cur.fetchall()
                if not rows:
                    async with db.execute(
                        f"SELECT COUNT(*) FROM {tabla} "
                        f"WHERE {col_num} IS NULL AND {col_src} IS NULL"
                    ) as cur:
                        fuente_null = (await cur.fetchone())[0]
                    # E-8 (Fase 5): resumen agregado de fallidas — el
                    # detalle fila a fila se limita a _MAX_DETALLE_FALLIDAS;
                    # la muestra de valores distintos es la telemetría que
                    # AUD-M1 necesita para auditar formatos reales.
                    if total_fallidas:
                        _log_event(
                            "etl_conversion_resumen",
                            level="WARNING",
                            msg=(
                                f"{tabla}.{col_src} | "
                                f"{total_fallidas} fallidas "
                                f"({min(total_fallidas, _MAX_DETALLE_FALLIDAS)} "
                                f"detalladas) | muestra: "
                                f"{sorted(muestra_fallidas)}"
                            ),
                            tabla=tabla,
                            columna=col_num,
                            fallidas=total_fallidas,
                            detalladas=min(total_fallidas, _MAX_DETALLE_FALLIDAS),
                            muestra_valores=sorted(muestra_fallidas),
                        )
                    # E-3: tres contadores veraces como campos JSON
                    # (catálogo en docs/structure.md).
                    _log_event(
                        "etl_columna_ok",
                        msg=(
                            f"{tabla}.{col_num} | "
                            f"{total_convertidas} convertidas | "
                            f"{total_placeholders} placeholders | "
                            f"{total_fallidas} fallidas | "
                            f"{fuente_null} fuente NULL"
                        ),
                        tabla=tabla,
                        columna=col_num,
                        convertidas=total_convertidas,
                        placeholders=total_placeholders,
                        fallidas=total_fallidas,
                        fuente_null=fuente_null,
                    )
                    break
                for row_id, val in rows:
                    # val nunca es None: el SELECT filtra col_src IS NOT NULL
                    valor_num = normalizar_numerico(val, guion_es_cero=guion_es_cero)
                    if valor_num is None:
                        # DEC-020: sin UPDATE — la fila queda _num NULL y
                        # se reintenta en la próxima corrida (auto-repara
                        # cuando to_num cubra el formato, sin churn de WAL).
                        if es_placeholder(val):
                            # DEC-025: ausencia esperada del origen, no un
                            # fallo — no ensucia el WARNING ni la muestra.
                            total_placeholders += 1
                            continue
                        total_fallidas += 1
                        if len(muestra_fallidas) < _MAX_MUESTRA_FALLIDAS:
                            muestra_fallidas.add(repr(val))
                        # E-8 (Fase 5): detalle fila a fila solo hasta el
                        # tope; el excedente lo agrega el resumen.
                        if total_fallidas <= _MAX_DETALLE_FALLIDAS:
                            _log_event(
                                "etl_conversion_fallida",
                                level="WARNING",
                                msg=(f"{tabla}.{col_src} id={row_id} valor_original={val!r}"),
                            )
                        continue
                    await db.execute(
                        f"UPDATE {tabla} SET {col_num} = ? WHERE {clave} = ?",
                        (valor_num, row_id),
                    )
                    total_convertidas += 1
                last_id = rows[-1][0]
                await db.commit()
                batch_count += 1
                total_escaneadas += len(rows)
                if batch_count % 10 == 0:
                    _log_event(
                        "etl_batch",
                        msg=(
                            f"{tabla}.{col_num} | "
                            f"batch {batch_count} | "
                            f"{total_escaneadas} filas escaneadas"
                        ),
                    )


# Pendiente "fragmentación de concepto" (detectado 2026-07-18): la SPA
# etiqueta el mismo concepto de IVA con la tasa aplicada al subpedido, ej.
# "IVA alimentos (5%)" / "(0%)" / "(%)" — agrupar por concepto crudo parte
# el mismo concepto en 3 buckets y ensucia los KPIs. "(%)" (tasa no
# determinada) siempre trae monto COP 0 — verificado contra la DB real.
_PATRON_CONCEPTO_TASA = re.compile(r"^(.+) \((\d+)?%\)$")


def _parse_concepto(concepto: str) -> tuple[str, float | None]:
    """Separa el concepto canónico de la tasa de IVA embebida en el texto.

    Args:
        concepto: Texto original de estadisticas_monto.concepto.

    Returns:
        Tupla (concepto_base, tasa_iva). tasa_iva es None si el concepto
        no trae tasa embebida (no es un concepto de IVA) o si la tasa no
        está determinada (variante "(%)").
    """
    match = _PATRON_CONCEPTO_TASA.match(concepto)
    if not match:
        return concepto, None
    base, tasa = match.groups()
    return base, float(tasa) if tasa is not None else None


async def separar_concepto_tasa(db: aiosqlite.Connection) -> None:
    """Agrega concepto_base y tasa_iva a estadisticas_monto (idempotente).

    Mismo patrón que normalizar_montos: ALTER TABLE tolerante a columna
    duplicada + poblado en batches de 500 filas, solo sobre las filas
    donde concepto_base aún es NULL, para reprocesar exclusivamente lo
    nuevo que deja el scraper en cada corrida.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    for columna, tipo_sql in (("concepto_base", "TEXT"), ("tasa_iva", "REAL")):
        await ejecutar_ddl_idempotente(
            db,
            f"ALTER TABLE estadisticas_monto ADD COLUMN {columna} {tipo_sql}",
            DDL_ERROR_COLUMNA_DUPLICADA,
        )

    last_id = 0
    total = 0
    while True:
        async with db.execute(
            "SELECT id, concepto FROM estadisticas_monto "
            "WHERE id > ? AND concepto_base IS NULL AND concepto IS NOT NULL "
            "ORDER BY id LIMIT 500",
            (last_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            break
        for row_id, concepto in rows:
            base, tasa = _parse_concepto(concepto)
            await db.execute(
                "UPDATE estadisticas_monto SET concepto_base = ?, tasa_iva = ? WHERE id = ?",
                (base, tasa, row_id),
            )
            total += 1
        last_id = rows[-1][0]
        await db.commit()

    if total:
        _log_event(
            "etl_concepto_separado",
            msg=f"{total} filas con concepto_base/tasa_iva pobladas",
            total=total,
        )


async def crear_views(db: aiosqlite.Connection) -> None:
    """Crea o reemplaza las VIEWs analíticas y de montos limpios.

    Usa DROP VIEW IF EXISTS antes de cada CREATE VIEW
    para garantizar idempotencia. Todo el DROP+CREATE ocurre
    dentro de una transacción explícita (BEGIN IMMEDIATE,
    DEC-019): con WAL, los lectores concurrentes ven el
    snapshot anterior hasta el COMMIT y nunca una view ausente.

    VIEWs analíticas (2): v_diferencias_resumen y v_descuentos_lineas.
    Eran 8 hasta DEC-065 y 3 hasta DEC-103, que retiró
    `v_inventario_comprometido` por semántica inválida (medido: reportaba
    251 unidades cuando lo comprometido eran 497.929). Las 6 retiradas se
    listan en `_VIEWS_RETIRADAS` y se dropean acá mismo.

    VIEWs para el dashboard (4): v_lineas_pedido_num,
    v_estadisticas_monto_num, v_gestion_diferencias_num,
    v_detalle_diferencias_num. Exponen los valores monetarios
    ya convertidos a REAL con nombres limpios (sin sufijo _num)
    para que el dashboard los consuma directamente.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    views = {
        "v_diferencias_resumen": """
            SELECT
                p.id_pedido,
                p.fecha,
                p.vendedor,
                p.forma_pago,
                g.total_pagar_pedido,
                g.monto_final_pagar,
                g.monto_diferencia,
                g.total_pagar_pedido_num,
                g.monto_final_pagar_num,
                g.monto_diferencia_num,
                COUNT(d.id) AS productos_con_diferencia
            FROM pedidos p
            JOIN gestion_diferencias g
                ON p.id_pedido = g.id_pedido
            LEFT JOIN detalle_diferencias d
                ON p.id_pedido = d.id_pedido
            WHERE p.hay_diferencia = 1
            -- E-8/DEC-021: GROUP BY por PK; las columnas g.* son 1:1 por
            -- el UNIQUE de AUD-B9 (idx_gestion_dif_unico) — determinístico
            GROUP BY p.id_pedido
        """,
        # DEC-024 (nota "monto del descuento"): la ranura monetaria de
        # lineas_pedido.descuento nunca contiene dinero — el descuento real
        # se deriva de precio_unitario_num - precio_descuento_num. Solo
        # filas donde descuento_tipo está poblado tuvieron un descuento
        # real aplicado (correlación verificada en DEC-025).
        "v_descuentos_lineas": """
            SELECT
                l.id_pedido,
                p.fecha,
                p.servicio_cliente,
                p.nombre_empresa,
                p.nit,
                p.vendedor,
                l.numero_subpedido,
                l.nombre_producto,
                l.descuento_tipo,
                l.cantidad_comprada,
                l.precio_unitario_num  AS precio_unitario,
                l.precio_descuento_num AS precio_descuento,
                (l.precio_unitario_num - l.precio_descuento_num)
                    AS monto_descuento_unitario,
                (l.precio_unitario_num - l.precio_descuento_num)
                    * l.cantidad_comprada AS monto_descuento_total
            FROM lineas_pedido l
            JOIN pedidos p ON l.id_pedido = p.id_pedido
            WHERE l.descuento_tipo IS NOT NULL
            AND l.descuento_tipo != ''
            AND l.precio_unitario_num IS NOT NULL
            AND l.precio_descuento_num IS NOT NULL
        """,
        # ── VIEWs para el dashboard ────────────────────────────────────
        # Exponen los valores monetarios ya convertidos a REAL con
        # nombres limpios (sin sufijo _num). El dashboard lee estas
        # views en lugar de las tablas base.
        # Ejemplo: precio_unitario = 1940.0 en lugar de "COP 1.940"
        "v_lineas_pedido_num": """
            SELECT
                l.id,
                l.id_pedido,
                l.numero_subpedido,
                l.tipo_subpedido,
                l.nombre_producto,
                l.referencia,
                l.codigo_barras,
                l.presentacion,
                l.almacen,
                l.cantidad_comprada,
                l.cantidad_entregada,
                l.precio_unitario_num   AS precio_unitario,
                l.descuento_num         AS descuento,
                l.precio_descuento_num  AS precio_descuento,
                l.monto_pagar_num       AS monto_pagar,
                l.monto_final_num       AS monto_final,
                l.iva_num               AS iva,
                l.peso_total_num        AS peso_total,
                l.observaciones,
                l.numero_caja,
                l.tipo
            FROM lineas_pedido l
        """,
        "v_estadisticas_monto_num": """
            SELECT
                e.id,
                e.id_pedido,
                e.orden,
                e.concepto,
                e.concepto_tag,
                e.concepto_base,
                e.tasa_iva,
                e.monto_pagar_num   AS monto_pagar,
                e.monto_final_num   AS monto_final,
                e.diferencia_num    AS diferencia
            FROM estadisticas_monto e
        """,
        "v_gestion_diferencias_num": """
            SELECT
                g.id,
                g.id_pedido,
                g.total_pagar_pedido_num  AS total_pagar_pedido,
                g.monto_final_pagar_num   AS monto_final_pagar,
                g.monto_pagado_num        AS monto_pagado,
                g.monto_diferencia_num    AS monto_diferencia
            FROM gestion_diferencias g
        """,
        "v_detalle_diferencias_num": """
            SELECT
                d.id,
                d.id_pedido,
                d.nombre_producto,
                d.especificacion,
                d.tipo,
                d.precio_unitario_num        AS precio_unitario,
                d.descuento_num              AS descuento,
                d.precio_descuento_num       AS precio_descuento,
                d.cantidad_pedido_num        AS cantidad_pedido,
                d.cantidad_entregada_num     AS cantidad_entregada,
                d.diferencia_cantidad_num    AS diferencia_cantidad,
                d.monto_pagar_pedido_num     AS monto_pagar_pedido,
                d.monto_final_pagar_num      AS monto_final_pagar,
                d.iva_num                    AS iva,
                d.monto_diferencia_num       AS monto_diferencia
            FROM detalle_diferencias d
        """,
    }

    # DEC-019 (E-2): el DDL ejecuta en autocommit bajo el isolation_level
    # legacy — sin transacción explícita cada DROP VIEW se materializa al
    # instante y deja una ventana en la que un lector concurrente recibe
    # "no such view" (causa raíz de HAL-007/AUD-M6). BEGIN IMMEDIATE toma
    # el write lock de entrada; con WAL los lectores ven el snapshot
    # anterior hasta el COMMIT.
    await db.execute("BEGIN IMMEDIATE")
    try:
        # DEC-065: las retiradas se dropean dentro de la MISMA transacción
        # que recrea el resto — si fuera aparte, un lector concurrente
        # podría ver la base a medio migrar.
        for nombre in _VIEWS_RETIRADAS:
            await db.execute(f"DROP VIEW IF EXISTS {nombre}")
        for nombre, sql in views.items():
            await db.execute(f"DROP VIEW IF EXISTS {nombre}")
            await db.execute(f"CREATE VIEW {nombre} AS {sql}")
    except Exception:
        await db.rollback()
        raise
    await db.commit()


async def verificar_pedidos_sin_subpedidos(db: aiosqlite.Connection) -> int:
    """Check defensivo DEC-021: pedidos completos sin subpedidos.

    Un pedido con scraping_completo=1 sin filas en subpedidos es un
    estado anómalo que ninguna agregación por subpedido puede mostrar:
    el INNER JOIN lo descarta. Emite etl_pedido_sin_subpedidos
    (WARNING) con conteo y muestra de IDs.

    El check sobrevive a DEC-065 aunque las views de pedidos que lo
    motivaron ya no existan: la anomalía sigue siendo real y sigue
    siendo invisible desde cualquier consulta que una con subpedidos.

    Dos causas posibles, indistinguibles desde el ETL (ver DEC-021):
    el pedido está legítimamente vacío en el sistema origen ("Total 0
    subpedidos"), o su extracción fue incompleta. Verificar en el
    origen antes de re-scrapear; un re-scrape no repara el primer caso.

    Args:
        db: Conexión abierta a pedidos.db.

    Returns:
        Cantidad de pedidos anómalos encontrados.
    """
    async with db.execute(
        "SELECT p.id_pedido FROM pedidos p "
        "WHERE p.scraping_completo = 1 "
        "AND NOT EXISTS (SELECT 1 FROM subpedidos s WHERE s.id_pedido = p.id_pedido) "
        "ORDER BY p.id_pedido"
    ) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    if ids:
        _log_event(
            "etl_pedido_sin_subpedidos",
            level="WARNING",
            msg=(
                f"{len(ids)} pedidos con scraping_completo=1 sin subpedidos — "
                "invisibles para cualquier agregación por subpedido (DEC-021); "
                "verificar en el origen si están legítimamente vacíos"
            ),
            total=len(ids),
            muestra_ids=ids[:10],
        )
    return len(ids)


async def actualizar_estadisticas(db: aiosqlite.Connection) -> None:
    """Refresca las estadísticas del planificador de SQLite (DEC-068).

    Sin `sqlite_stat1` el planificador no sabe que el filtro de fecha del
    consolidado es selectivo —7 días de 210— y arranca escaneando la tabla
    más grande: 864.073 líneas para devolver 34.000. Con estadísticas
    reordena los joins solo.

    Medido sobre copias limpias de la base real, tres corridas por
    configuración: el consolidado pasa de **6,1 s a 1,9 s**. Agregar índices
    encima aporta 0,1 s más y no paga su peso en cada INSERT del scraper,
    así que **no se agregó ninguno** — mismo criterio que DEC-049.

    Corre acá y no en el scraper porque el ETL es el último paso del ciclo:
    cuando termina, los datos del scheduler ya están completos. Costo
    medido: 2,1 s sobre un estacionario de 33 s.

    Un fallo acá **no tumba el ETL**: las estadísticas son una optimización,
    y quedarse con las de la corrida anterior solo cuesta velocidad.

    Args:
        db: Conexión abierta a pedidos.db.
    """
    try:
        inicio = datetime.now(timezone.utc)
        await db.execute("ANALYZE")
        await db.commit()
        ms = (datetime.now(timezone.utc) - inicio).total_seconds() * 1000
        _log_event(
            "etl_analyze_ok", msg=f"estadísticas actualizadas en {ms:.0f} ms", duracion_ms=ms
        )
    except Exception as exc:  # noqa: BLE001 — optimización, nunca crítica
        _log_event(
            "etl_analyze_fallido",
            level="WARNING",
            msg="no se pudieron actualizar las estadísticas; se sigue con las anteriores",
            error=str(exc),
        )


async def main() -> int:
    """Punto de entrada del ETL.

    Abre la conexión a data/pedidos.db, ejecuta la
    normalización de montos y la creación de VIEWs,
    y cierra la conexión.

    Returns:
        0: ETL completado sin errores.
        1: Error no recuperable durante la ejecución.
        (AUD-B7: el sys.exit vive en __main__, no en la corrutina.)
    """
    db_path = get_db_path()
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            await normalizar_montos(db)
            await separar_concepto_tasa(db)
            await crear_views(db)
            # DEC-065: acá vivía un check de 0 filas sobre
            # v_rendimiento_operadores. Se fue con la view.

            # AUD-M8 (auditoría 2026-07-01): check defensivo — un estado en
            # DB fuera de las listas del módulo común indica que el sistema
            # origen agregó o renombró estados; las VIEWs podrían estar
            # excluyéndolo en silencio.
            async with db.execute(
                "SELECT DISTINCT LOWER(estado) FROM subpedidos "
                "WHERE estado IS NOT NULL AND estado != ''"
            ) as cur:
                rows_est = await cur.fetchall()
            desconocidos = sorted(r[0] for r in rows_est if r[0] not in ESTADOS_CONOCIDOS)
            if desconocidos:
                _log_event(
                    "etl_estado_desconocido",
                    level="WARNING",
                    msg=(
                        "Estados de subpedidos fuera de las listas conocidas "
                        f"del módulo común: {desconocidos}"
                    ),
                )

            # DEC-021 (Fase 6): pedidos completos sin subpedidos — anómalos
            # e invisibles en las views de pedidos; hacerlos visibles.
            await verificar_pedidos_sin_subpedidos(db)

            await actualizar_estadisticas(db)
        _log_event("etl_completado", db_path=db_path)
        return 0
    except Exception as exc:
        _log_event(
            "etl_fallido",
            level="ERROR",
            error=str(exc),
            error_type=type(exc).__name__,
            db_path=db_path,
        )
        return 1


if __name__ == "__main__":
    # E-6: basicConfig solo al ejecutar como script — importar este módulo
    # (tests, dashboard) no debe reconfigurar el root logger del proceso.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # AUD-B7: main() retorna el exit code; sys.exit() solo vive aquí.
    sys.exit(asyncio.run(main()))
