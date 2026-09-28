"""
persistencia.py — Task única de escritura en SQLite (DEC-013).

persistencia_worker() consume resultados de la cola y persiste cada pedido
en una transacción atómica. Los helpers _actualizar_estado_subpedido() y
_persistir_secciones_satelite() (AUD-M10) operan dentro de la transacción
abierta por el worker.
"""

import asyncio
from datetime import datetime, timezone

import aiosqlite

from comun import ESTADOS_CERRADOS
from scraper.config import log_event

# DEC-087: `executemany` con parámetros nombrados revienta si a una fila le
# falta una clave, y al extractor le faltan las columnas que el origen no
# haya renderizado en esa fila. Este default se mezcla debajo de cada
# registro para que una columna ausente entre como cadena vacía en vez de
# tumbar la persistencia del pedido entero — que es exactamente lo que hizo
# DEC-074 con una nota al pie del layout.
_REGISTRO_PAGO_VACIO = {
    "secuencia": "",
    "metodo_pago": "",
    "cuenta_receptora": "",
    "monto_comprobante": "",
    "monto_pago": "",
    "hora_pago": "",
    "comprobante": "",
    "fecha_envio": "",
    "estado_revision": "",
    "fecha_revision": "",
    "revisor": "",
    "observaciones": "",
}

# ─────────────────────────────────────────────
# PERSISTENCIA
# ─────────────────────────────────────────────


async def _actualizar_estado_subpedido(
    db,
    id_pedido: str,
    numero_subpedido: str,
    estado_scrapeado: str,
) -> str:
    """Actualiza estado + estado_cambiado_en de un subpedido solo si el estado cambió.

    Compara el estado recién scrapeado contra el almacenado, ignorando
    diferencias de capitalización y espacios (la comparación usa
    strip().lower(); el valor que se guarda es el original de la SPA).
    Debe invocarse dentro de una transacción ya abierta (BEGIN) sobre db;
    no hace commit y usa el mismo objeto de conexión para SELECT y UPDATE.

    Args:
        db: Conexión aiosqlite con una transacción abierta.
        id_pedido: ID del pedido.
        numero_subpedido: Identificador del subpedido dentro del pedido.
        estado_scrapeado: Estado recién extraído de la SPA (sin normalizar).

    Returns:
        "actualizado"   — el estado cambió; se actualizó estado + estado_cambiado_en.
        "sin_cambio"    — el estado coincide (ignorando case/espacios); no se tocó nada.
        "no_encontrado" — no existe fila para (id_pedido, numero_subpedido); se loggeó WARNING.
        "vacio"         — el estado scrapeado llegó vacío; no se tocó nada y se loggeó
                          WARNING (auditoría 2026-09-25).
    """
    # Auditoría 2026-09-25: un estado vacío es siempre una lectura fallida
    # (celda sin etiqueta, columna movida), nunca un estado del origen — el
    # placeholder legítimo es '-' (DEC-040). Guardarlo borraba el estado
    # bueno y sacaba al pedido de ESTADOS_CERRADOS/activos.
    if not (estado_scrapeado or "").strip():
        log_event(
            "estado_subpedido_vacio",
            level="WARNING",
            id_pedido=id_pedido,
            msg=f"subpedido {numero_subpedido}: estado leído vacío — se conserva el de la base",
        )
        return "vacio"
    fila = await (
        await db.execute(
            "SELECT estado FROM subpedidos WHERE id_pedido = ? AND numero_subpedido = ?",
            (id_pedido, numero_subpedido),
        )
    ).fetchone()

    if fila is None:
        log_event(
            "subpedido_no_encontrado",
            level="WARNING",
            id_pedido=id_pedido,
            msg=(
                f"subpedido {numero_subpedido} no existe en DB — "
                f"estado no actualizado (scrapeado: '{estado_scrapeado}')"
            ),
        )
        return "no_encontrado"

    estado_en_db = fila[0]
    if (estado_en_db or "").strip().lower() != (estado_scrapeado or "").strip().lower():
        await db.execute(
            "UPDATE subpedidos SET estado = ?, estado_cambiado_en = ? "
            "WHERE id_pedido = ? AND numero_subpedido = ?",
            (
                estado_scrapeado,
                datetime.now(timezone.utc).isoformat(),
                id_pedido,
                numero_subpedido,
            ),
        )
        return "actualizado"

    return "sin_cambio"


# DEC-152 (D2): un valor real sobrescribe; un vacío o el placeholder '-' del
# origen nunca pisan uno bueno (sí llenan una columna todavía NULL).
_SQL_SIN_PISAR = (
    "CASE WHEN NULLIF(NULLIF(TRIM(:{c}), ''), '-') IS NOT NULL THEN :{c} "
    "ELSE COALESCE({c}, :{c}) END"
)
_CAMPOS_OPERACION_SUBPEDIDO = (
    "inicio_alistamiento",
    "alistamiento_completado",
    "alistador",
    "inicio_inspeccion",
    "inspeccion_completada",
    "inspector",
)


async def _actualizar_operacion_subpedido(
    db: aiosqlite.Connection, id_pedido: str, num_sub: str, sp: dict
) -> None:
    """Alistamiento e inspección del subpedido en la pasada `con_cantidades`.

    DEC-152 (D2): el modo los leía y los descartaba, así que quedaban con lo
    que hubiera al crearse el pedido — antes de alistarlo (completados de
    ago/sep: alistador 95/82%). Esta pasada corre cuando el subpedido cierra,
    que es cuando los valores ya son definitivos.
    """
    asignaciones = ", ".join(
        f"{c} = {_SQL_SIN_PISAR.format(c=c)}" for c in _CAMPOS_OPERACION_SUBPEDIDO
    )
    await db.execute(
        f"UPDATE subpedidos SET {asignaciones} "
        "WHERE id_pedido = :id_pedido AND numero_subpedido = :num_sub",
        {
            **{c: sp.get(c) for c in _CAMPOS_OPERACION_SUBPEDIDO},
            "id_pedido": id_pedido,
            "num_sub": num_sub,
        },
    )


async def _actualizar_lineas_con_cantidades(
    db: aiosqlite.Connection, id_pedido: str, num_sub: str, lineas: list[dict]
) -> None:
    """Cantidad y peso entregados, y número de caja, línea por línea.

    DEC-152 (D1): antes el UPDATE iba por `(pedido, subpedido, código de
    barras)`, así que con el mismo código en varias líneas todas quedaban con
    la entregada de la ÚLTIMA (2.134 de las 2.142 líneas con entregada >
    comprada; 23/23 grupos verificados contra el origen). Ahora cada grupo de
    código repetido se empareja por posición con sus filas (orden de
    inserción = orden del DOM en el modo completo), y solo si las cantidades
    compradas coinciden en el mismo orden: si no, WARNING y el grupo no se
    toca — mejor un dato viejo que uno cruzado.
    """
    grupos: dict[str, list[dict]] = {}
    for linea in lineas:
        grupos.setdefault(linea["codigo_barras"], []).append(linea)

    for codigo, del_origen in grupos.items():
        filas = await (
            await db.execute(
                "SELECT id, cantidad_comprada FROM lineas_pedido "
                "WHERE id_pedido = ? AND numero_subpedido = ? AND codigo_barras = ? "
                "ORDER BY id",
                (id_pedido, num_sub, codigo),
            )
        ).fetchall()
        if not filas:
            log_event(
                "update_sin_match",
                level="WARNING",
                id_pedido=id_pedido,
                msg=(
                    f"cantidad_entregada no actualizada — codigo_barras vacío o "
                    f"no encontrado en subpedido {num_sub}"
                ),
            )
            continue
        if len(del_origen) == 1 and len(filas) == 1:
            pares = [(filas[0][0], del_origen[0])]
        elif len(del_origen) == len(filas) and all(
            _misma_cantidad(f[1], ln["cantidad_comprada"])
            for f, ln in zip(filas, del_origen, strict=True)
        ):
            pares = [(f[0], ln) for f, ln in zip(filas, del_origen, strict=True)]
        else:
            log_event(
                "lineas_repetidas_no_emparejadas",
                level="WARNING",
                id_pedido=id_pedido,
                msg=(
                    f"código {codigo!r} repetido en subpedido {num_sub}: "
                    f"{len(del_origen)} líneas en el origen contra {len(filas)} en la "
                    f"base, o compradas en otro orden — cantidades no actualizadas"
                ),
            )
            continue
        for fila_id, linea in pares:
            # DEC-140: el peso entregado se mueve con la cantidad entregada,
            # sin pisar un valor bueno con uno vacío.
            await db.execute(
                "UPDATE lineas_pedido SET cantidad_entregada = :entregada, "
                "peso_entregado = COALESCE(NULLIF(:peso_entregado, ''), peso_entregado), "
                f"numero_caja = {_SQL_SIN_PISAR.format(c='numero_caja')} "
                "WHERE id = :id",
                {
                    "entregada": linea["cantidad_entregada"],
                    "peso_entregado": linea.get("peso_entregado", ""),
                    "numero_caja": linea.get("numero_caja"),
                    "id": fila_id,
                },
            )


def _misma_cantidad(en_base: object, del_origen: object) -> bool:
    try:
        return float(en_base) == float(del_origen)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return en_base == del_origen


async def _persistir_secciones_satelite(
    db,
    id_pedido: str,
    resultado: dict,
    warn: bool,
) -> None:
    """Persiste las 5 tablas satélite del pedido con DELETE + INSERT condicional.

    Tablas: estadisticas_monto, gestion_diferencias, detalle_diferencias,
    registro_operaciones y registros_pago (DEC-087). El DELETE solo ocurre si la sección trae datos
    (HAL-004): una extracción vacía preserva los datos existentes en vez de
    borrarlos. Extraída de las 3 ramas de persistencia_worker() (AUD-M10),
    donde el mismo bloque estaba triplicado.

    Debe invocarse dentro de una transacción ya abierta (BEGIN) sobre db;
    no hace BEGIN/COMMIT propio.

    Args:
        db: Conexión aiosqlite con una transacción abierta.
        id_pedido: ID del pedido en persistencia.
        resultado: Dict de resultado producido por scraper_worker.
        warn: True en modo completo — que garantiza el renderizado de todas
              las secciones, por lo que una sección vacía amerita WARNING. False
              en con_cantidades y solo_estado, donde el vacío es esperable
              porque esos modos no garantizan el renderizado (criterio
              BUG-013/HAL-004).
    """
    if resultado.get("estadisticas"):
        await db.execute("DELETE FROM estadisticas_monto WHERE id_pedido = ?", (id_pedido,))
        await db.executemany(
            """INSERT INTO estadisticas_monto
               (id_pedido, orden, concepto, concepto_tag,
                monto_pagar, monto_final, diferencia)
               VALUES
               (:id_pedido, :orden, :concepto, :concepto_tag,
                :monto_pagar, :monto_final, :diferencia)""",
            resultado["estadisticas"],
        )
    elif warn:
        log_event(
            "estadisticas_vacio",
            level="WARNING",
            msg="estadisticas_monto retornó vacío — datos existentes preservados",
            id_pedido=id_pedido,
        )

    if resultado.get("gestion_dif"):
        await db.execute("DELETE FROM gestion_diferencias WHERE id_pedido = ?", (id_pedido,))
        gd = resultado["gestion_dif"]
        await db.execute(
            """INSERT INTO gestion_diferencias
               (id_pedido, total_pagar_pedido, monto_final_pagar,
                monto_pagado, monto_diferencia)
               VALUES (?, ?, ?, ?, ?)""",
            (
                id_pedido,
                gd.get("total_pagar_pedido", ""),
                gd.get("monto_final_pagar", ""),
                gd.get("monto_pagado", ""),
                gd.get("monto_diferencia", ""),
            ),
        )
    elif warn:
        # DEC-030 Fase 0: INFO, no WARNING — el 74% de los pedidos no tiene
        # diferencias; la ausencia del card es el caso normal, no una anomalía.
        log_event(
            "gestion_dif_vacio",
            level="INFO",
            msg="gestion_diferencias retornó vacío — datos existentes preservados",
            id_pedido=id_pedido,
        )

    if resultado.get("detalle_dif"):
        await db.execute("DELETE FROM detalle_diferencias WHERE id_pedido = ?", (id_pedido,))
        await db.executemany(
            """INSERT INTO detalle_diferencias
               (id_pedido, nombre_producto, especificacion, tipo,
                precio_unitario, descuento, descuento_tipo, precio_descuento,
                cantidad_pedido, cantidad_entregada, diferencia_cantidad,
                monto_pagar_pedido, monto_final_pagar, iva, monto_diferencia)
               VALUES
               (:id_pedido, :nombre_producto, :especificacion, :tipo,
                :precio_unitario, :descuento, :descuento_tipo, :precio_descuento,
                :cantidad_pedido, :cantidad_entregada, :diferencia_cantidad,
                :monto_pagar_pedido, :monto_final_pagar, :iva, :monto_diferencia)""",
            resultado["detalle_dif"],
        )
    elif warn:
        # DEC-030 Fase 0: mismo criterio que gestion_dif_vacio.
        log_event(
            "detalle_dif_vacio",
            level="INFO",
            msg="detalle_diferencias retornó vacío — datos existentes preservados",
            id_pedido=id_pedido,
        )

    if resultado.get("registro_ops"):
        await db.execute("DELETE FROM registro_operaciones WHERE id_pedido = ?", (id_pedido,))
        await db.executemany(
            """INSERT INTO registro_operaciones
               (id_pedido, momento, usuario, tipo_usuario, accion, referencia)
               VALUES
               (:id_pedido, :momento, :usuario, :tipo_usuario,
                :accion, :referencia)""",
            resultado["registro_ops"],
        )
    elif warn:
        log_event(
            "registro_ops_vacio",
            level="WARNING",
            msg="registro_operaciones retornó vacío — datos existentes preservados",
            id_pedido=id_pedido,
        )

    # DEC-087: los comprobantes de pago. Mismo DELETE condicional que el
    # resto (HAL-004), pero SIN el WARNING de sección vacía ni siquiera en
    # modo completo: la tabla solo existe si el pedido tuvo comprobantes, y
    # además la sección es nueva —la SPA la agregó el 2026-07-16— así que en
    # pedidos viejos su ausencia es lo esperado. Un WARNING que suena para el
    # caso normal enseña a ignorar el log (la patología de DEC-070/075).
    if resultado.get("registros_pago"):
        await db.execute("DELETE FROM registros_pago WHERE id_pedido = ?", (id_pedido,))
        await db.executemany(
            """INSERT INTO registros_pago
               (id_pedido, secuencia, metodo_pago, cuenta_receptora,
                monto_comprobante, monto_pago, hora_pago, comprobante,
                fecha_envio, estado_revision, fecha_revision, revisor,
                observaciones)
               VALUES
               (:id_pedido, :secuencia, :metodo_pago, :cuenta_receptora,
                :monto_comprobante, :monto_pago, :hora_pago, :comprobante,
                :fecha_envio, :estado_revision, :fecha_revision, :revisor,
                :observaciones)""",
            [{**_REGISTRO_PAGO_VACIO, **r} for r in resultado["registros_pago"]],
        )


async def persistencia_worker(
    resultados_queue: asyncio.Queue,
    db_path: str,
    run_stats: dict[str, set[str]] | None = None,
) -> None:
    """Task única que escribe en SQLite. Termina al recibir el sentinel None.

    Procesa cuatro tipos de registros desde resultados_queue:
      - completo:       upsert en pedidos, DELETE + INSERT en subpedidos y
                        lineas_pedido, scraping_completo=1.
      - con_cantidades: UPDATE cantidad_entregada + estado + cantidades_definitivas=1
                        en subpedidos; reemplaza timeline.
      - solo_estado:    UPDATE estado en subpedidos; reemplaza timeline; marca
                        scraping_completo=1 si todos los subpedidos están cerrados.
      - error (_error=True): INSERT en errores.
    Cada pedido se persiste en una sola transacción atómica (BEGIN/COMMIT).

    Args:
        resultados_queue: Cola de resultados producidos por los scraper_workers.
        db_path: Ruta al archivo SQLite.
        run_stats: Dict opcional con sets "ok" y "error" donde se registra el
                   resultado final de cada pedido del run (FIX N-1): "ok" tras
                   COMMIT exitoso, "error" ante resultado _error o fallo de
                   persistencia. None desactiva el conteo.
    """
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        # N-3 (DEC-014): FK ON en toda conexión de escritura. El pragma es
        # por conexión — el ON de init_db() muere con la suya. El upsert de
        # pedidos precede a sus hijos en la misma transacción, así que la
        # verificación solo dispara ante bugs reales de huérfanos.
        await db.execute("PRAGMA foreign_keys = ON")

        while True:
            resultado = await resultados_queue.get()
            if resultado is None:
                break

            id_pedido = resultado["id_pedido"]

            # — Registro de error —
            if resultado.get("_error"):
                # FIX N-1: resultado final de este pedido en el run = error.
                # Un pase dead-letter posterior puede revertirlo agregándolo
                # a "ok" (el cálculo del resumen resta ok de error).
                if run_stats is not None:
                    run_stats["error"].add(id_pedido)
                try:
                    await db.execute("BEGIN")
                    await db.execute(
                        "INSERT INTO errores (id_pedido, momento, detalle) VALUES (?, ?, ?)",
                        (
                            id_pedido,
                            datetime.now(timezone.utc).isoformat(),
                            resultado["detalle"],
                        ),
                    )
                    await db.execute("COMMIT")
                except Exception as exc:
                    await db.execute("ROLLBACK")
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error guardando en errores: {exc}",
                    )
                continue

            tipo = resultado.get("tipo", "completo")

            # ── Modo completo ──────────────────────────────────────────────
            if tipo == "completo":
                info_g = resultado["info_general"]
                subped = resultado["subpedidos"]

                lineas_rows: list[dict] = []
                for sp in subped:
                    for linea in sp["lineas"]:
                        lineas_rows.append(
                            {
                                "id_pedido": id_pedido,
                                "numero_subpedido": sp["numero_subpedido"],
                                "tipo_subpedido": sp["tipo_subpedido"],
                                "nombre_producto": linea["nombre_producto"],
                                "referencia": linea["referencia"],
                                "codigo_barras": linea["codigo_barras"],
                                "presentacion": linea["presentacion"],
                                "almacen": linea["almacen"],
                                "cantidad_comprada": linea["cantidad_comprada"],
                                "cantidad_entregada": linea["cantidad_entregada"],
                                "precio_unitario": linea["precio_unitario"],
                                "descuento": linea["descuento"],
                                # DEC-024: tipo separado del monto
                                "descuento_tipo": linea.get("descuento_tipo", ""),
                                "precio_descuento": linea["precio_descuento"],
                                "monto_pagar": linea["monto_pagar"],
                                "monto_final": linea["monto_final"],
                                "iva": linea["iva"],
                                "peso_total": linea["peso_total"],
                                "peso_entregado": linea.get("peso_entregado", ""),
                                "observaciones": linea["observaciones"],
                                "numero_caja": linea["numero_caja"],
                                "tipo": linea["tipo"],
                            }
                        )

                fecha_completa = info_g.get("fecha", "")
                partes_fecha = fecha_completa.split(" ")
                fecha_val = partes_fecha[0] if partes_fecha else ""
                hora_val = partes_fecha[1] if len(partes_fecha) > 1 else ""

                _info_insert = {
                    "id_pedido": info_g.get("id_pedido", ""),
                    "fecha": fecha_val,
                    "hora": hora_val,
                    "servicio_cliente": info_g.get("servicio_cliente", ""),
                    "vendedor": info_g.get("vendedor", ""),
                    "forma_pago": info_g.get("forma_pago", ""),
                    "comprobante": info_g.get("comprobante", ""),
                    "nombre_empresa": info_g.get("nombre_empresa", ""),
                    "nit": info_g.get("nit", ""),
                    "metodo_entrega": info_g.get("metodo_entrega", ""),
                    "destinatario": info_g.get("destinatario", ""),
                    "telefono": info_g.get("telefono", ""),
                    "direccion_envio": info_g.get("direccion_envio", ""),
                    "observaciones": info_g.get("observaciones", ""),
                    "actualizado_en": datetime.now(timezone.utc).isoformat(),
                }

                try:
                    await db.execute("BEGIN")

                    await db.execute(
                        """
                        INSERT INTO pedidos (
                            id_pedido, fecha, hora, servicio_cliente, vendedor, forma_pago,
                            comprobante, nombre_empresa, nit, metodo_entrega,
                            destinatario, telefono, direccion_envio, observaciones,
                            scraping_completo, actualizado_en
                        ) VALUES (
                            :id_pedido, :fecha, :hora, :servicio_cliente, :vendedor, :forma_pago,
                            :comprobante, :nombre_empresa, :nit, :metodo_entrega,
                            :destinatario, :telefono, :direccion_envio, :observaciones,
                            0, :actualizado_en
                        )
                        ON CONFLICT(id_pedido) DO UPDATE SET
                            fecha               = excluded.fecha,
                            hora                = excluded.hora,
                            servicio_cliente    = excluded.servicio_cliente,
                            vendedor            = excluded.vendedor,
                            forma_pago          = excluded.forma_pago,
                            comprobante         = excluded.comprobante,
                            nombre_empresa      = excluded.nombre_empresa,
                            nit                 = excluded.nit,
                            metodo_entrega      = excluded.metodo_entrega,
                            destinatario        = excluded.destinatario,
                            telefono            = excluded.telefono,
                            direccion_envio     = excluded.direccion_envio,
                            observaciones       = excluded.observaciones,
                            actualizado_en      = excluded.actualizado_en
                        """,
                        _info_insert,
                    )

                    if subped:
                        await db.execute(
                            "DELETE FROM subpedidos     WHERE id_pedido = ?", (id_pedido,)
                        )
                        await db.execute(
                            "DELETE FROM lineas_pedido  WHERE id_pedido = ?", (id_pedido,)
                        )

                        ts_completo = datetime.now(timezone.utc).isoformat()
                        await db.executemany(
                            """
                            INSERT INTO subpedidos (
                                id_pedido, numero_subpedido, tipo_subpedido, estado,
                                inicio_alistamiento, alistamiento_completado, alistador,
                                inicio_inspeccion, inspeccion_completada, inspector,
                                estado_cambiado_en, cantidades_definitivas
                            ) VALUES (
                                :id_pedido, :numero_subpedido, :tipo_subpedido, :estado,
                                :inicio_alistamiento, :alistamiento_completado, :alistador,
                                :inicio_inspeccion, :inspeccion_completada, :inspector,
                                :estado_cambiado_en, :cantidades_definitivas
                            )
                            """,
                            [
                                {
                                    "id_pedido": id_pedido,
                                    "numero_subpedido": sp["numero_subpedido"],
                                    "tipo_subpedido": sp["tipo_subpedido"],
                                    "estado": sp["estado"],
                                    "inicio_alistamiento": sp["inicio_alistamiento"],
                                    "alistamiento_completado": sp["alistamiento_completado"],
                                    "alistador": sp["alistador"],
                                    "inicio_inspeccion": sp["inicio_inspeccion"],
                                    "inspeccion_completada": sp["inspeccion_completada"],
                                    "inspector": sp["inspector"],
                                    "estado_cambiado_en": ts_completo,
                                    # DEC-147: un subpedido que ya llega cerrado
                                    # trae sus cantidades definitivas en esta misma
                                    # lectura. Sin esto el DELETE + INSERT lo dejaba
                                    # en 0 y el ciclo siguiente lo repasaba entero
                                    # en con_cantidades (1.108 de 1.131 el
                                    # 2026-09-25 con la tanda de 1.200).
                                    "cantidades_definitivas": int(
                                        (sp["estado"] or "").strip().lower() in ESTADOS_CERRADOS
                                    ),
                                }
                                for sp in subped
                            ],
                        )

                        if lineas_rows:
                            await db.executemany(
                                """
                                INSERT INTO lineas_pedido (
                                    id_pedido, numero_subpedido, tipo_subpedido,
                                    nombre_producto, referencia, codigo_barras, presentacion,
                                    almacen, cantidad_comprada, cantidad_entregada,
                                    precio_unitario, descuento, descuento_tipo,
                                    precio_descuento,
                                    monto_pagar, monto_final, iva, peso_total, peso_entregado, observaciones,
                                    numero_caja, tipo
                                ) VALUES (
                                    :id_pedido, :numero_subpedido, :tipo_subpedido,
                                    :nombre_producto, :referencia, :codigo_barras, :presentacion,
                                    :almacen, :cantidad_comprada, :cantidad_entregada,
                                    :precio_unitario, :descuento, :descuento_tipo,
                                    :precio_descuento,
                                    :monto_pagar, :monto_final, :iva, :peso_total, :peso_entregado, :observaciones,
                                    :numero_caja, :tipo
                                )
                                """,
                                lineas_rows,
                            )
                    else:
                        # Seguimiento DEC-021: el contador del origen
                        # discrimina "0 subpedidos legítimo" (no es un fallo,
                        # no hay WARNING) de "no renderizó" (guard
                        # conservador de FIX C-2, WARNING como antes).
                        if resultado.get("total_subpedidos_origen") == 0:
                            log_event(
                                "subpedidos_vacio_legitimo",
                                msg="origen declara Total 0 subpedidos — vacío confirmado, no es fallo de renderizado",
                                id_pedido=id_pedido,
                            )
                        else:
                            log_event(
                                "subpedidos_vacio",
                                level="WARNING",
                                msg="extracción de subpedidos retornó vacío — datos existentes preservados",
                                id_pedido=id_pedido,
                            )

                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.execute(
                            "DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,)
                        )
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )
                    elif tipo == "completo":
                        log_event(
                            "timeline_vacio",
                            level="WARNING",
                            msg="div.order-steps-wrapper no renderizó o retornó vacío",
                            id_pedido=id_pedido,
                        )

                    # FIX C-2 (auditoría 2026-07-01): no cerrar scraping_completo
                    # sin subpedidos extraídos. Si subped vino vacío, se conserva
                    # el valor actual (0 en pedidos nuevos) para que
                    # determinar_modo() re-extraiga en modo completo en la
                    # próxima corrida — salvo que el contador del origen
                    # (seguimiento DEC-021) confirme que el vacío es legítimo.
                    vacio_legitimo = not subped and resultado.get("total_subpedidos_origen") == 0
                    if subped or vacio_legitimo:
                        await db.execute(
                            "UPDATE pedidos SET scraping_completo = 1, actualizado_en = ? WHERE id_pedido = ?",
                            (datetime.now(timezone.utc).isoformat(), id_pedido),
                        )
                    else:
                        await db.execute(
                            "UPDATE pedidos SET actualizado_en = ? WHERE id_pedido = ?",
                            (datetime.now(timezone.utc).isoformat(), id_pedido),
                        )

                    info_e = resultado.get("info_entrega") or {}
                    # DEC-091: un valor vacío NO pisa uno bueno.
                    #
                    # La SPA deja de renderizar la tarjeta de entrega para
                    # pedidos viejos —medido: 0-2% de cobertura antes del
                    # 2026-02-03 contra 76-84% después, y re-extraer 24
                    # pedidos de enero con el código de hoy no recupera nada—
                    # pero esos pedidos SÍ se entregaron (3.333 con paso
                    # «Recibido y recibido» y 3.425 con evento «Entrega»).
                    #
                    # Con el UPDATE incondicional anterior, cada re-scrape de
                    # un pedido cuya tarjeta ya no renderiza escribía cadena
                    # vacía encima del dato bueno. **Preservar es la elección
                    # deliberada**: el costo es quedarse con un valor viejo si
                    # el origen realmente lo borra; el beneficio es no perder
                    # lo único que tenemos de un periodo que ya no se puede
                    # volver a leer. El detector de cobertura (DEC-091) avisa
                    # si un campo empieza a caerse, que es el caso que esta
                    # protección vuelve invisible.
                    await db.execute(
                        """
                        UPDATE pedidos SET
                            alistador_pedido      = CASE WHEN TRIM(:ap) <> '' THEN :ap ELSE alistador_pedido END,
                            inspector_pedido      = CASE WHEN TRIM(:ip) <> '' THEN :ip ELSE inspector_pedido END,
                            movil_cliente         = CASE WHEN TRIM(:mc) <> '' THEN :mc ELSE movil_cliente END,
                            despachador           = CASE WHEN TRIM(:desp) <> '' THEN :desp ELSE despachador END,
                            conductor             = CASE WHEN TRIM(:cond) <> '' THEN :cond ELSE conductor END,
                            hora_entrega          = CASE WHEN TRIM(:he) <> '' THEN :he ELSE hora_entrega END,
                            vehiculo_entrega      = CASE WHEN TRIM(:veh) <> '' THEN :veh ELSE vehiculo_entrega END,
                            obs_entrega           = CASE WHEN TRIM(:oe) <> '' THEN :oe ELSE obs_entrega END,
                            entrega_ruta_tag      = CASE WHEN TRIM(:ert) <> '' THEN :ert ELSE entrega_ruta_tag END,
                            entrega_descuento_tag = CASE WHEN TRIM(:edt) <> '' THEN :edt ELSE entrega_descuento_tag END,
                            persona_recogida      = CASE WHEN TRIM(:pr) <> '' THEN :pr ELSE persona_recogida END,
                            movil_recogida        = CASE WHEN TRIM(:mr) <> '' THEN :mr ELSE movil_recogida END,
                            dias_credito          = CASE WHEN TRIM(:dc) <> '' THEN :dc ELSE dias_credito END,
                            inicio_credito        = CASE WHEN TRIM(:ic) <> '' THEN :ic ELSE inicio_credito END,
                            vencimiento_credito   = CASE WHEN TRIM(:vc) <> '' THEN :vc ELSE vencimiento_credito END
                        WHERE id_pedido = :pid
                        """,
                        {
                            "ap": info_g.get("alistador_pedido", ""),
                            "ip": info_g.get("inspector_pedido", ""),
                            "mc": info_g.get("movil_cliente", ""),
                            "desp": info_e.get("despachador", ""),
                            # DEC-023: conductor y vehículo solo vienen con
                            # metodo_entrega='Ruta'; vacíos en el resto.
                            "cond": info_e.get("conductor", ""),
                            "he": info_e.get("hora_entrega", ""),
                            "veh": info_e.get("vehiculo_entrega", ""),
                            "oe": info_e.get("obs_entrega", ""),
                            "ert": info_e.get("entrega_ruta_tag", ""),
                            "edt": info_e.get("entrega_descuento_tag", ""),
                            # DEC-032: solo vienen con metodo_entrega='Almacen'.
                            "pr": info_g.get("persona_recogida", ""),
                            "mr": info_g.get("movil_recogida", ""),
                            # DEC-033: solo vienen con forma_pago='Pago a crédito'.
                            "dc": info_g.get("dias_credito", ""),
                            "ic": info_g.get("inicio_credito", ""),
                            "vc": info_g.get("vencimiento_credito", ""),
                            "pid": id_pedido,
                        },
                    )

                    # FIX C-3 (auditoría 2026-07-01): hay_diferencia solo se
                    # actualiza si se pudo verificar (None = card no leído).
                    _hd = resultado.get("hay_diferencia")
                    if _hd is not None:
                        await db.execute(
                            "UPDATE pedidos SET hay_diferencia = ? WHERE id_pedido = ?",
                            (_hd, id_pedido),
                        )

                    # DEC-087: la tarjeta «Operación de pago» se actualiza
                    # SOLO si se leyó. Mismo criterio que FIX C-3 y por un
                    # motivo concreto: la sección es del 2026-07-16, no
                    # renderiza en pedidos viejos, y el UPDATE de arriba
                    # —que pisa con cadena vacía lo que no viene— borraría
                    # datos buenos en cada re-scrape de un pedido anterior.
                    _op = resultado.get("operacion_pago")
                    if _op:
                        await db.execute(
                            """
                            UPDATE pedidos SET
                                pago_estado   = :estado,
                                pago_total    = :total,
                                pago_pagado   = :pagado,
                                pago_saldo    = :saldo,
                                pago_progreso = :progreso
                            WHERE id_pedido = :pid
                            """,
                            {
                                "estado": _op.get("pago_estado", ""),
                                "total": _op.get("pago_total", ""),
                                "pagado": _op.get("pago_pagado", ""),
                                "saldo": _op.get("pago_saldo", ""),
                                "progreso": _op.get("pago_progreso", ""),
                                "pid": id_pedido,
                            },
                        )

                    # HAL-004: DELETE dentro de if (sección con datos) para no
                    # borrar datos existentes cuando la extracción retorna
                    # vacío. warn=True: el modo completo garantiza el
                    # renderizado de las secciones — un vacío es anómalo.
                    await _persistir_secciones_satelite(db, id_pedido, resultado, warn=True)

                    await db.execute("COMMIT")
                    # FIX N-1: éxito solo tras COMMIT — un resultado publicado
                    # cuya transacción falla no es un pedido asegurado.
                    if run_stats is not None:
                        run_stats["ok"].add(id_pedido)
                    log_event("db_guardado", id_pedido=id_pedido, msg="Pedido persistido")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    if run_stats is not None:
                        run_stats["error"].add(id_pedido)
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo pedido: {exc}",
                    )

            # ── Modo con_cantidades ────────────────────────────────────────
            elif tipo == "con_cantidades":
                try:
                    await db.execute("BEGIN")

                    for sp in resultado["subpedidos"]:
                        num_sub = sp["numero_subpedido"]
                        await _actualizar_lineas_con_cantidades(
                            db, id_pedido, num_sub, sp["lineas"]
                        )
                        # DEC-152 (D2): alistador/inspector/fechas se leían y
                        # no se persistían; esta pasada es justo la del cierre.
                        await _actualizar_operacion_subpedido(db, id_pedido, num_sub, sp)
                        # Estado / estado_cambiado_en: la función auxiliar decide su
                        # propio UPDATE condicional (solo escribe si el estado cambió,
                        # ignorando diferencias de capitalización) y maneja el caso
                        # "fila no encontrada" con WARNING. Misma lógica que solo_estado.
                        await _actualizar_estado_subpedido(db, id_pedido, num_sub, sp["estado"])
                        # cantidades_definitivas: segundo UPDATE SEPARADO e
                        # INCONDICIONAL. Se ejecuta siempre, sin importar si el estado
                        # cambió o no — preserva el comportamiento previo (cantidades
                        # marcadas como definitivas en cada pasada con_cantidades).
                        # El orden relativo a la llamada anterior es indiferente:
                        # ambas operan sobre columnas distintas de la misma fila,
                        # dentro de la misma transacción ya abierta. Si la fila no
                        # existe, este UPDATE es un no-op (rowcount 0), sin reportar
                        # nada adicional aquí — el caso "fila no encontrada" ya fue
                        # loggeado con WARNING por la llamada anterior.
                        await db.execute(
                            "UPDATE subpedidos SET cantidades_definitivas = 1 "
                            "WHERE id_pedido = ? AND numero_subpedido = ?",
                            (id_pedido, num_sub),
                        )

                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.execute(
                            "DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,)
                        )
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )

                    # FIX C-3 (auditoría 2026-07-01): no pisar hay_diferencia
                    # cuando no se pudo verificar (card no renderizado).
                    _hd = resultado.get("hay_diferencia")
                    if _hd is not None:
                        await db.execute(
                            "UPDATE pedidos SET hay_diferencia = ? WHERE id_pedido = ?",
                            (_hd, id_pedido),
                        )

                    # HAL-004: warn=False — sin WARNING en con_cantidades
                    # porque el modo no garantiza renderizado de estas
                    # secciones (mismo criterio que BUG-013/timeline).
                    await _persistir_secciones_satelite(db, id_pedido, resultado, warn=False)

                    # AUD-M3: el heartbeat solo se refresca si la extracción
                    # trajo subpedidos. Con extracción vacía (tabla no
                    # renderizada), tocar actualizado_en reportaría como
                    # verificado un pedido que no se pudo leer, falsificando
                    # la base de "Días sin mov." (BUG-018). Se emite WARNING
                    # porque tras AUD-M3 este modo sí espera el renderizado
                    # de la tabla: un vacío aquí es anómalo (a diferencia de
                    # las secciones satélite, criterio BUG-013/HAL-004).
                    if resultado["subpedidos"]:
                        await db.execute(
                            "UPDATE pedidos SET actualizado_en = ? WHERE id_pedido = ?",
                            (datetime.now(timezone.utc).isoformat(), id_pedido),
                        )
                    else:
                        log_event(
                            "subpedidos_vacio",
                            level="WARNING",
                            msg="extracción de subpedidos retornó vacío en con_cantidades — actualizado_en no refrescado",
                            id_pedido=id_pedido,
                        )
                    await db.execute("COMMIT")
                    # FIX N-1: éxito solo tras COMMIT.
                    if run_stats is not None:
                        run_stats["ok"].add(id_pedido)
                    log_event("db_guardado", id_pedido=id_pedido, msg="Cantidades actualizadas")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    if run_stats is not None:
                        run_stats["error"].add(id_pedido)
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo con_cantidades: {exc}",
                    )

            # ── Modo solo_estado ───────────────────────────────────────────
            elif tipo == "solo_estado":
                try:
                    await db.execute("BEGIN")

                    for sp in resultado["subpedidos"]:
                        await _actualizar_estado_subpedido(
                            db, id_pedido, sp["numero_subpedido"], sp["estado"]
                        )

                    timeline = resultado.get("timeline", [])
                    if timeline:
                        await db.execute(
                            "DELETE FROM timeline_pedido WHERE id_pedido = ?", (id_pedido,)
                        )
                        await db.executemany(
                            """
                            INSERT INTO timeline_pedido
                                (id_pedido, paso, titulo, fecha_hora, completado)
                            VALUES
                                (:id_pedido, :paso, :titulo, :fecha_hora, :completado)
                            """,
                            timeline,
                        )

                    _closed_ph = ",".join("?" * len(ESTADOS_CERRADOS))
                    open_count_row = await (
                        await db.execute(
                            f"SELECT COUNT(*) FROM subpedidos "
                            f"WHERE id_pedido = ? "
                            f"AND LOWER(estado) NOT IN ({_closed_ph})",
                            (id_pedido, *ESTADOS_CERRADOS),
                        )
                    ).fetchone()
                    ts_ahora = datetime.now(timezone.utc).isoformat()
                    if open_count_row and open_count_row[0] == 0:
                        await db.execute(
                            "UPDATE pedidos SET scraping_completo = 1, actualizado_en = ? "
                            "WHERE id_pedido = ?",
                            (ts_ahora, id_pedido),
                        )
                    else:
                        await db.execute(
                            "UPDATE pedidos SET actualizado_en = ? WHERE id_pedido = ?",
                            (ts_ahora, id_pedido),
                        )

                    # FIX C-3 (auditoría 2026-07-01): no pisar hay_diferencia
                    # cuando no se pudo verificar (card no renderizado).
                    _hd = resultado.get("hay_diferencia")
                    if _hd is not None:
                        await db.execute(
                            "UPDATE pedidos SET hay_diferencia = ? WHERE id_pedido = ?",
                            (_hd, id_pedido),
                        )

                    # HAL-004: warn=False — sin WARNING en solo_estado
                    # porque el modo no garantiza renderizado de estas
                    # secciones (mismo criterio que BUG-013/timeline).
                    await _persistir_secciones_satelite(db, id_pedido, resultado, warn=False)

                    await db.execute("COMMIT")
                    # FIX N-1: éxito solo tras COMMIT.
                    if run_stats is not None:
                        run_stats["ok"].add(id_pedido)
                    log_event("db_guardado", id_pedido=id_pedido, msg="Estado actualizado")

                except Exception as exc:
                    await db.execute("ROLLBACK")
                    if run_stats is not None:
                        run_stats["error"].add(id_pedido)
                    log_event(
                        "db_error",
                        level="ERROR",
                        id_pedido=id_pedido,
                        msg=f"Error persistiendo solo_estado: {exc}",
                    )
