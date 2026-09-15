"""
integraciones/sheets_arena.py — Datos para el conteo físico de Arena por
ciudad, listos para exportar a Google Sheets (pedido del Arquitecto,
2026-09-14).

Las 11 ciudades de `comun.arena.CIUDADES_SOLO_ARENA` solo almacenan Arena
(a diferencia de Bogotá). Los asistentes de inventario cruzan estos dos
números — inventario disponible y pedidos aún no alistados — para calcular
el inventario teórico antes del conteo físico.

Solo las consultas SQL viven acá; escribir a Google Sheets es
`integraciones/sheets_cliente.py`. Separado a propósito: esta parte se
puede probar contra una base SQLite temporal sin credenciales de Google.

No depende de `dashboard/db.py` (DEC-022, bajo acoplamiento): este módulo
es un consumidor más de `pedidos.db`, no del dashboard.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from comun import ARENA_MODALIDADES_NUCLEO, ESTADOS_PREVIOS_PICKING
from comun.arena import CIUDADES_SOLO_ARENA

COLUMNAS_INVENTARIO = [
    "Ciudad",
    "Código de barras",
    "Referencia",
    "Especificación",
    "Nombre comercial",
    "Disponible para venta",
]

COLUMNAS_PEDIDOS = [
    "Fecha del pedido",
    "Hora",
    "Pedido padre",
    "Número de subpedido",
    "Tipo de subpedido",
    "Estado del subpedido",
    "Ciudad",
    "Referencia",
    "Código de barras",
    "Presentación",
    "Cantidad comprada",
]


def inventario_disponible_arena(con: sqlite3.Connection) -> pd.DataFrame:
    """Inventario Arena disponible para venta, por (ciudad, código de barras).

    Mismo criterio que `comun.arena.alertas_quiebre_arena()`: suma las 3
    modalidades núcleo (Unidades/Tonelada/Corporativo) en un solo pool —
    Respaldo y el hub de Yumbo no son inventario vendible (DEC-118/119), y
    sumar da la disponibilidad real porque no hay reserva formal que
    impida vender el stock de una modalidad bajo otra.

    Usa la columna `inventario`, no `existencias_restantes`: el origen
    dejó de publicar esa segunda columna — 100% NULL en las 1.307 filas de
    `arena_inventario` verificado el 2026-09-14, no es un hueco de esta
    consulta.

    Args:
        con: Conexión abierta a `pedidos.db`.

    Returns:
        Una fila por (ciudad, código de barras), columnas
        `COLUMNAS_INVENTARIO`. Vacío si `arena_inventario` no tiene filas
        para las ciudades de `CIUDADES_SOLO_ARENA`.
    """
    ciudades = ",".join(f"'{c}'" for c in CIUDADES_SOLO_ARENA)
    modalidades = ",".join(f"'{m}'" for m in ARENA_MODALIDADES_NUCLEO)
    return pd.read_sql(
        f"""
        SELECT almacen               AS "Ciudad",
               codigo_barras         AS "Código de barras",
               referencia            AS "Referencia",
               MAX(especificacion)   AS "Especificación",
               MAX(nombre_comercial) AS "Nombre comercial",
               SUM(inventario)       AS "Disponible para venta"
          FROM arena_inventario
         WHERE almacen IN ({ciudades})
           AND modalidad IN ({modalidades})
         GROUP BY almacen, codigo_barras, referencia
         ORDER BY almacen, referencia
        """,
        con,
    )


def pedidos_previos_picking_arena(con: sqlite3.Connection) -> pd.DataFrame:
    """Líneas de pedido de Arena en estados previos a picking, por ciudad.

    Mismo nivel de detalle que Dashboard → Pedidos → Consolidado con
    "Solo previos a picking" (DEC-109): subpedido comprometido y aún sin
    alistar (`ESTADOS_PREVIOS_PICKING`) y con el alistamiento físico sin
    terminar (`inicio_inspeccion = '-'`). No se agrega un filtro "Abiertos"
    aparte: ninguno de los 3 estados de `ESTADOS_PREVIOS_PICKING` está en
    `ESTADOS_CERRADOS`, así que ya es redundante a nivel de esta misma
    fila.

    A diferencia del Consolidado del dashboard (DEC-112, recorte a 31 días
    por tiempo de render en el navegador), acá no hay límite de fecha: un
    pedido viejo atascado en previos-a-picking es justo el caso que un
    asistente de inventario necesita ver, no uno para recortar.

    Arena se identifica por `codigo_barras` presente en `arena_inventario`
    — mismo criterio que `dashboard.db.get_arena_movimiento()`.

    Args:
        con: Conexión abierta a `pedidos.db`.

    Returns:
        Una fila por línea de pedido, columnas `COLUMNAS_PEDIDOS`.
    """
    ciudades = ",".join(f"'{c}'" for c in CIUDADES_SOLO_ARENA)
    estados = ",".join(f"'{e}'" for e in ESTADOS_PREVIOS_PICKING)
    return pd.read_sql(
        f"""
        SELECT p.fecha              AS "Fecha del pedido",
               p.hora               AS "Hora",
               p.id_pedido          AS "Pedido padre",
               s.numero_subpedido   AS "Número de subpedido",
               s.tipo_subpedido     AS "Tipo de subpedido",
               s.estado             AS "Estado del subpedido",
               l.almacen            AS "Ciudad",
               l.referencia         AS "Referencia",
               l.codigo_barras      AS "Código de barras",
               l.presentacion       AS "Presentación",
               l.cantidad_comprada  AS "Cantidad comprada"
          FROM lineas_pedido l
          JOIN pedidos p    ON p.id_pedido = l.id_pedido
          JOIN subpedidos s ON s.id_pedido = l.id_pedido
                           AND s.numero_subpedido = l.numero_subpedido
         WHERE l.almacen IN ({ciudades})
           AND LOWER(s.estado) IN ({estados})
           AND s.inicio_inspeccion = '-'
           AND l.codigo_barras IN (SELECT DISTINCT codigo_barras FROM arena_inventario)
         ORDER BY l.almacen, p.fecha DESC, p.hora DESC, p.id_pedido, s.numero_subpedido
        """,
        con,
    )
