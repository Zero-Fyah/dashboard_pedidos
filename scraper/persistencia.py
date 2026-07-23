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
    """
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


async def _persistir_secciones_satelite(
    db,
    id_pedido: str,
    resultado: dict,
    warn: bool,
) -> None:
    """Persiste las 4 tablas satélite del pedido con DELETE + INSERT condicional.

    Tablas: estadisticas_monto, gestion_diferencias, detalle_diferencias y
    registro_operaciones. El DELETE solo ocurre si la sección trae datos
    (HAL-004): una extracción vacía preserva los datos existentes en vez de
    borrarlos. Extraída de las 3 ramas de persistencia_worker() (AUD-M10),
    donde el mismo bloque estaba triplicado.

    Debe invocarse dentro de una transacción ya abierta (BEGIN) sobre db;
    no hace BEGIN/COMMIT propio.

    Args:
        db: Conexión aiosqlite con una transacción abierta.
        id_pedido: ID del pedido en persistencia.
        resultado: Dict de resultado producido por scraper_worker.
        warn: True en modo completo — que garantiza el renderizado de las 8
              secciones, por lo que una sección vacía amerita WARNING. False
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
                                estado_cambiado_en
                            ) VALUES (
                                :id_pedido, :numero_subpedido, :tipo_subpedido, :estado,
                                :inicio_alistamiento, :alistamiento_completado, :alistador,
                                :inicio_inspeccion, :inspeccion_completada, :inspector,
                                :estado_cambiado_en
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
                                    monto_pagar, monto_final, iva, peso_total, observaciones,
                                    numero_caja, tipo
                                ) VALUES (
                                    :id_pedido, :numero_subpedido, :tipo_subpedido,
                                    :nombre_producto, :referencia, :codigo_barras, :presentacion,
                                    :almacen, :cantidad_comprada, :cantidad_entregada,
                                    :precio_unitario, :descuento, :descuento_tipo,
                                    :precio_descuento,
                                    :monto_pagar, :monto_final, :iva, :peso_total, :observaciones,
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
                    await db.execute(
                        """
                        UPDATE pedidos SET
                            alistador_pedido      = :ap,
                            inspector_pedido      = :ip,
                            movil_cliente         = :mc,
                            despachador           = :desp,
                            conductor             = :cond,
                            hora_entrega          = :he,
                            vehiculo_entrega      = :veh,
                            obs_entrega           = :oe,
                            entrega_ruta_tag      = :ert,
                            entrega_descuento_tag = :edt,
                            persona_recogida      = :pr,
                            movil_recogida        = :mr,
                            dias_credito          = :dc,
                            inicio_credito        = :ic,
                            vencimiento_credito   = :vc
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

                    # HAL-004: DELETE dentro de if (sección con datos) para no
                    # borrar datos existentes cuando la extracción retorna
                    # vacío. warn=True: el modo completo garantiza el
                    # renderizado de las 8 secciones — un vacío es anómalo.
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
                        for linea in sp["lineas"]:
                            cursor = await db.execute(
                                "UPDATE lineas_pedido SET cantidad_entregada = ? "
                                "WHERE id_pedido = ? AND numero_subpedido = ? "
                                "AND codigo_barras = ?",
                                (
                                    linea["cantidad_entregada"],
                                    id_pedido,
                                    num_sub,
                                    linea["codigo_barras"],
                                ),
                            )
                            if cursor.rowcount == 0:
                                log_event(
                                    "update_sin_match",
                                    level="WARNING",
                                    id_pedido=id_pedido,
                                    msg=(
                                        f"cantidad_entregada no actualizada — "
                                        f"codigo_barras vacío o no encontrado en "
                                        f"subpedido {num_sub}"
                                    ),
                                )
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
