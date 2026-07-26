"""
persistencia.py — La comparación de inventario aterriza en SQLite (DEC-043).

El dashboard **no importa este paquete ni lee Excel**: consume el
resultado por VIEW, igual que cualquier otro dato del proyecto. Calcular
en vivo costaba 14,19 s por request (13,83 s de parseo de Excel) y habría
sido el primer import cruzado entre etapas, contra DEC-022.

Este módulo es el punto de entrada del paso: `python -m
inventario.persistencia` carga las tres fuentes, calcula y escribe. Lo
invoca `scraper/actualizar_pedidos.bat` después de las dos descargas y
antes del ETL.

`inventario/` es dueño de sus tablas y de sus VIEWs — no pasa por el ETL,
que no tiene nada que normalizar acá. Son dos dueños de VIEWs en la misma
base (prefijo `v_inventario_*` propio, sin colisión con las 12 del ETL):
quien agregue una VIEW nueva debe mirar los dos módulos.
"""

import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from comun import get_db_path

logger = logging.getLogger("inventario.persistencia")

# Con el scheduler corriendo cada hora, una fuente de más de 3 h implica
# al menos dos descargas fallidas seguidas (DEC-043). El layout queda
# fuera del umbral a propósito: es manual, es normal que tenga semanas.
UMBRAL_DESACTUALIZADO_H = 3.0

_TABLAS = {
    "inventario_corridas": """
        CREATE TABLE IF NOT EXISTS inventario_corridas (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            ejecutado_en           TEXT    NOT NULL,
            admin_actualizado_en   TEXT,
            bochica_actualizado_en TEXT,
            layout_actualizado_en  TEXT,
            fuente_mas_vieja_h     REAL,
            datos_desactualizados  INTEGER NOT NULL DEFAULT 0,
            referencias            INTEGER,
            disponible_venta       REAL,
            vendido_no_alistado    REAL,
            inventario_teorico     REAL,
            bochica_altura         REAL,
            bochica_picking        REAL,
            bochica_paso           REAL,
            picking_estimado       REAL,
            referencias_negativas  INTEGER,
            unidades_negativas     REAL,
            anomalias_filas        INTEGER,
            anomalias_unidades     REAL
        )
    """,
    "inventario_comparacion": """
        CREATE TABLE IF NOT EXISTS inventario_comparacion (
            referencia          TEXT PRIMARY KEY,
            familia             TEXT,
            es_averia           INTEGER,
            disponible_venta    REAL,
            vendido_no_alistado REAL,
            inventario_teorico  REAL,
            bochica_altura      REAL,
            bochica_picking     REAL,
            bochica_paso        REAL,
            picking_estimado    REAL,
            diferencia          REAL,
            corrida_id          INTEGER
        )
    """,
    "catalogo_productos": """
        CREATE TABLE IF NOT EXISTS catalogo_productos (
            referencia    TEXT NOT NULL,
            codigo_barras TEXT NOT NULL,
            id_producto   TEXT NOT NULL,
            nombre_comercial TEXT,
            corrida_id    INTEGER,
            PRIMARY KEY (referencia, codigo_barras)
        )
    """,
    "inventario_salud": """
        CREATE TABLE IF NOT EXISTS inventario_salud (
            referencia     TEXT PRIMARY KEY,
            familia        TEXT,
            disponible     REAL,
            valor_venta    REAL,
            demanda_30d    REAL,
            demanda_90d    REAL,
            demanda_diaria REAL,
            dias_cobertura REAL,
            ultima_salida  TEXT,
            dias_sin_salida REAL,
            estado         TEXT,
            corrida_id     INTEGER
        )
    """,
    "inventario_abc": """
        CREATE TABLE IF NOT EXISTS inventario_abc (
            referencia      TEXT PRIMARY KEY,
            familia         TEXT,
            valor_consumo   REAL,
            pct_valor       REAL,
            pct_acumulado   REAL,
            abc             TEXT,
            unidades        REAL,
            cv              REAL,
            meses_con_venta REAL,
            xyz             TEXT,
            celda           TEXT,
            politica        TEXT,
            corrida_id      INTEGER
        )
    """,
    "tareas_hallazgos": """
        CREATE TABLE IF NOT EXISTS tareas_hallazgos (
            clave       TEXT PRIMARY KEY,
            titulo      TEXT NOT NULL,
            explicacion TEXT,
            categoria   TEXT,
            prioridad   TEXT,
            origen      TEXT,
            unidad      TEXT,
            cantidad    INTEGER NOT NULL,
            detalle     TEXT,
            medido_en   TEXT,
            corrida_id  INTEGER
        )
    """,
    "inventario_anomalias": """
        CREATE TABLE IF NOT EXISTS inventario_anomalias (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            motivo            TEXT,
            ubicacion         TEXT,
            id_especificacion TEXT,
            cantidad          REAL,
            corrida_id        INTEGER
        )
    """,
}

_VIEWS = {
    "v_inventario_comparacion": """
        SELECT referencia, familia, es_averia,
               disponible_venta, vendido_no_alistado, inventario_teorico,
               bochica_altura, bochica_picking, bochica_paso,
               picking_estimado, diferencia
        FROM inventario_comparacion
    """,
    "v_inventario_salud": """
        SELECT referencia, familia, disponible, valor_venta,
               demanda_30d, demanda_90d, demanda_diaria,
               dias_cobertura, ultima_salida, dias_sin_salida, estado
        FROM inventario_salud
    """,
    "v_inventario_abc": """
        SELECT referencia, familia, valor_consumo, pct_valor, pct_acumulado,
               abc, unidades, cv, meses_con_venta, xyz, celda, politica
        FROM inventario_abc
    """,
    "v_inventario_anomalias": """
        SELECT motivo, ubicacion, id_especificacion, cantidad
        FROM inventario_anomalias
    """,
    "v_inventario_corridas": """
        SELECT id, ejecutado_en,
               admin_actualizado_en, bochica_actualizado_en, layout_actualizado_en,
               fuente_mas_vieja_h, datos_desactualizados,
               referencias, disponible_venta, vendido_no_alistado,
               inventario_teorico, bochica_altura, bochica_picking, bochica_paso,
               picking_estimado, referencias_negativas, unidades_negativas,
               anomalias_filas, anomalias_unidades
        FROM inventario_corridas
    """,
}

_COLUMNAS_COMPARACION = (
    "referencia",
    "familia",
    "es_averia",
    "disponible_venta",
    "vendido_no_alistado",
    "inventario_teorico",
    "bochica_altura",
    "bochica_picking",
    "bochica_paso",
    "picking_estimado",
    "diferencia",
)

_COLUMNAS_ANOMALIAS = ("motivo", "ubicacion", "id_especificacion", "cantidad")

_COLUMNAS_CATALOGO = ("referencia", "codigo_barras", "id_producto", "nombre_comercial")

_COLUMNAS_ABC = (
    "referencia",
    "familia",
    "valor_consumo",
    "pct_valor",
    "pct_acumulado",
    "abc",
    "unidades",
    "cv",
    "meses_con_venta",
    "xyz",
    "celda",
    "politica",
)

_COLUMNAS_SALUD = (
    "referencia",
    "familia",
    "disponible",
    "valor_venta",
    "demanda_30d",
    "demanda_90d",
    "demanda_diaria",
    "dias_cobertura",
    "ultima_salida",
    "dias_sin_salida",
    "estado",
)


def construir_catalogo_productos(df_admin: pd.DataFrame) -> pd.DataFrame:
    """Arma el puente `(referencia, código de barras)` → ID de producto (DEC-045).

    `lineas_pedido` no guarda ningún ID de producto: identifica el artículo
    por referencia, código de barras, nombre y presentación. Los IDs viven
    solo en el catálogo del admin, así que esta tabla es la que permite
    mostrarlos en la vista consolidada de Pedidos.

    **Solo se conservan los pares que resuelven a un único `id_producto`.**
    El código de barras no es único por ID (un mismo código cuelga de hasta
    21 referencias — la misma arena vendida por unidad, tonelada o
    corporativo), y si un par quedara con dos IDs el LEFT JOIN del dashboard
    **duplicaría la línea de pedido e inflaría `Cantidad comprada`**. Medido:
    el 99,6% de los pares es inequívoco; el 0,4% restante se descarta a
    propósito y esas líneas muestran el ID vacío.

    Se usa el catálogo **sin filtrar por alcance**: acá interesa poder
    nombrar cualquier producto que aparezca en un pedido, incluidas las
    arenas y los otros almacenes que `filtrar_alcance_admin()` excluye del
    cruce de inventario.

    Args:
        df_admin: Resultado de `inventario.normalizador.cargar_admin()`.

    Returns:
        DataFrame con `referencia`, `codigo_barras`, `id_producto` y
        `nombre_comercial`; una fila por par inequívoco.
    """
    df = df_admin[["referencia", "codigo_barras", "id_producto_admin", "nombre_comercial"]].copy()
    df["referencia"] = df["referencia"].astype(str).str.strip()
    df["codigo_barras"] = df["codigo_barras"].astype(str).str.strip()
    df["id_producto"] = df["id_producto_admin"].astype(str).str.strip()

    total = df[["referencia", "codigo_barras"]].drop_duplicates().shape[0]
    por_par = df.groupby(["referencia", "codigo_barras"])["id_producto"].nunique()
    inequivocos = por_par[por_par == 1].index

    puente = (
        df.set_index(["referencia", "codigo_barras"])
        .loc[df.set_index(["referencia", "codigo_barras"]).index.isin(inequivocos)]
        .reset_index()
        .drop_duplicates(subset=["referencia", "codigo_barras"])
    )
    descartados = total - len(puente)
    if descartados:
        logger.info(
            "construir_catalogo_productos: %d/%d pares descartados por ambigüedad "
            "(más de un id_producto) — esas líneas quedan sin ID (DEC-045)",
            descartados,
            total,
        )
    return puente[list(_COLUMNAS_CATALOGO)]


def init_schema(con: sqlite3.Connection) -> None:
    """Crea tablas y VIEWs si no existen. Idempotente.

    Args:
        con: Conexión abierta a pedidos.db.
    """
    for sql in _TABLAS.values():
        con.execute(sql)
    _crear_views(con)


def _crear_views(con: sqlite3.Connection) -> None:
    """Recrea las VIEWs de inventario dentro de una transacción (DEC-019).

    Sin transacción explícita cada DROP VIEW se materializa al instante y
    deja una ventana en la que el dashboard recibe "no such view" — la
    causa raíz de HAL-007. `BEGIN IMMEDIATE` toma el write lock de
    entrada; con WAL los lectores ven el snapshot anterior hasta el COMMIT.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        for nombre, sql in _VIEWS.items():
            con.execute(f"DROP VIEW IF EXISTS {nombre}")
            con.execute(f"CREATE VIEW {nombre} AS {sql}")
    except Exception:
        con.rollback()
        raise
    con.commit()


def _mtime_iso(path: Path) -> str | None:
    """Fecha de última modificación de un archivo, en ISO UTC, o None si no está."""
    if not path.exists():
        return None
    marca = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return marca.isoformat(timespec="seconds")


def medir_frescura(
    admin: Path, bochica: Path, layout: Path, *, ahora: dt.datetime | None = None
) -> dict[str, object]:
    """Mide la antigüedad de las tres fuentes (DEC-043).

    Solo `admin` y `bochica` cuentan para el umbral: son las que el
    scheduler refresca cada hora. El layout es un documento manual y es
    normal que tenga días o semanas — se registra su fecha pero no marca
    nada.

    Args:
        admin: Ruta al Excel del sistema administrativo.
        bochica: Ruta al Excel de Bochica.
        layout: Ruta al layout de bodega.
        ahora: Momento de referencia (inyectable para tests). Default: UTC now.

    Returns:
        Diccionario con las 3 fechas ISO, `fuente_mas_vieja_h` (la peor de
        las dos automáticas) y el flag `datos_desactualizados`.
    """
    referencia = ahora or dt.datetime.now(tz=dt.timezone.utc)
    fechas = {
        "admin_actualizado_en": _mtime_iso(admin),
        "bochica_actualizado_en": _mtime_iso(bochica),
        "layout_actualizado_en": _mtime_iso(layout),
    }

    edades = []
    faltantes = []
    for clave in ("admin_actualizado_en", "bochica_actualizado_en"):
        valor = fechas[clave]
        if valor is None:
            faltantes.append(clave)
        else:
            edades.append((referencia - dt.datetime.fromisoformat(valor)).total_seconds() / 3600)

    mas_vieja = max(edades) if edades else None
    # Una fuente ausente es peor que una vieja: marca desactualizado sin
    # importar qué tan fresca esté la otra.
    desactualizado = bool(faltantes) or mas_vieja is None or mas_vieja > UMBRAL_DESACTUALIZADO_H
    if desactualizado:
        logger.warning(
            "medir_frescura: fuentes no confiables (más vieja: %s h, umbral %s h, "
            "faltantes: %s) — probable descarga fallida; el dashboard lo va a advertir",
            f"{mas_vieja:.1f}" if mas_vieja is not None else "n/a",
            UMBRAL_DESACTUALIZADO_H,
            faltantes or "ninguna",
        )

    return {**fechas, "fuente_mas_vieja_h": mas_vieja, "datos_desactualizados": int(desactualizado)}


def _resumir(comparacion: pd.DataFrame, anomalias: pd.DataFrame) -> dict[str, object]:
    """Calcula los agregados de la corrida que van a `inventario_corridas`."""
    negativos = comparacion[comparacion["picking_estimado"] < 0]
    return {
        "referencias": len(comparacion),
        "disponible_venta": float(comparacion["disponible_venta"].sum()),
        "vendido_no_alistado": float(comparacion["vendido_no_alistado"].sum()),
        "inventario_teorico": float(comparacion["inventario_teorico"].sum()),
        "bochica_altura": float(comparacion["bochica_altura"].sum()),
        "bochica_picking": float(comparacion["bochica_picking"].sum()),
        "bochica_paso": float(comparacion["bochica_paso"].sum()),
        "picking_estimado": float(comparacion["picking_estimado"].sum()),
        "referencias_negativas": len(negativos),
        "unidades_negativas": float(negativos["picking_estimado"].sum()),
        "anomalias_filas": len(anomalias),
        "anomalias_unidades": float(anomalias["cantidad"].sum()) if len(anomalias) else 0.0,
    }


def persistir(
    comparacion: pd.DataFrame,
    anomalias: pd.DataFrame,
    frescura: dict[str, object],
    db_path: str | None = None,
    catalogo: pd.DataFrame | None = None,
    hallazgos: list | None = None,
    salud: pd.DataFrame | None = None,
    abc: pd.DataFrame | None = None,
) -> int:
    """Escribe el snapshot de la corrida, reemplazando el anterior.

    Todo ocurre en una sola transacción `BEGIN IMMEDIATE` (DEC-019): un
    dashboard leyendo concurrentemente ve el snapshot anterior completo
    hasta el COMMIT, nunca una tabla a medio poblar.

    Args:
        comparacion: Resultado de `inventario.comparacion.comparar()`.
        anomalias: Resultado de `inventario.comparacion.anomalias_layout()`.
        frescura: Resultado de `medir_frescura()`.
        db_path: Ruta a pedidos.db. Default: `comun.get_db_path()`.
        catalogo: Resultado de `construir_catalogo_productos()` (DEC-045).
            Si es None, `catalogo_productos` se deja intacta — así un
            problema del cruce de inventario no borra el puente que
            necesita la vista de Pedidos.
        hallazgos: Resultado de `inventario.hallazgos.detectar_todos()`
            (DEC-047). Si es None, `tareas_hallazgos` queda intacta.
        salud: Resultado de `inventario.salud.calcular_salud()` (DEC-049).
            Si es None, `inventario_salud` queda intacta.
        abc: Resultado de `inventario.clasificacion.calcular_clasificacion()`
            (DEC-050). Si es None, `inventario_abc` queda intacta.

    Returns:
        El `id` de la corrida registrada en `inventario_corridas`.
    """
    resumen = _resumir(comparacion, anomalias)
    ejecutado_en = dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds")
    fila_corrida = {"ejecutado_en": ejecutado_en, **frescura, **resumen}

    con = sqlite3.connect(db_path or get_db_path(), timeout=30)
    try:
        init_schema(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            columnas = ", ".join(fila_corrida)
            marcas = ", ".join("?" * len(fila_corrida))
            cur = con.execute(
                f"INSERT INTO inventario_corridas ({columnas}) VALUES ({marcas})",
                tuple(fila_corrida.values()),
            )
            corrida_id = int(cur.lastrowid or 0)

            # Snapshot: solo la última corrida vive en estas dos tablas
            # (DEC-043 — el detalle por referencia de cada corrida serían
            # ~12,5M filas/año). El historial de agregados va en
            # inventario_corridas, que sí acumula.
            con.execute("DELETE FROM inventario_comparacion")
            con.execute("DELETE FROM inventario_anomalias")

            con.executemany(
                f"INSERT INTO inventario_comparacion "
                f"({', '.join(_COLUMNAS_COMPARACION)}, corrida_id) "
                f"VALUES ({', '.join('?' * len(_COLUMNAS_COMPARACION))}, ?)",
                [
                    (*_normalizar(fila), corrida_id)
                    for fila in comparacion[list(_COLUMNAS_COMPARACION)].itertuples(index=False)
                ],
            )
            if len(anomalias):
                con.executemany(
                    f"INSERT INTO inventario_anomalias "
                    f"({', '.join(_COLUMNAS_ANOMALIAS)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_ANOMALIAS))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in anomalias[list(_COLUMNAS_ANOMALIAS)].itertuples(index=False)
                    ],
                )

            # DEC-047: los hallazgos de calidad de datos. El DELETE total
            # es lo que hace que una tarea resuelta desaparezca: si el
            # detector ya no encuentra casos, no se vuelve a insertar.
            if hallazgos is not None:
                con.execute("DELETE FROM tareas_hallazgos")
                con.executemany(
                    """INSERT INTO tareas_hallazgos
                       (clave, titulo, explicacion, categoria, prioridad, origen,
                        unidad, cantidad, detalle, medido_en, corrida_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            h.clave,
                            h.titulo,
                            h.explicacion,
                            h.categoria,
                            h.prioridad,
                            h.origen,
                            h.unidad,
                            h.cantidad,
                            h.filas.to_json(orient="split", index=False),
                            ejecutado_en,
                            corrida_id,
                        )
                        for h in hallazgos
                    ],
                )

            # DEC-049: cobertura y movimiento por referencia.
            if salud is not None:
                con.execute("DELETE FROM inventario_salud")
                con.executemany(
                    f"INSERT INTO inventario_salud "
                    f"({', '.join(_COLUMNAS_SALUD)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_SALUD))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in salud[list(_COLUMNAS_SALUD)].itertuples(index=False)
                    ],
                )

            # DEC-050: clasificación ABC-XYZ.
            if abc is not None:
                con.execute("DELETE FROM inventario_abc")
                con.executemany(
                    f"INSERT INTO inventario_abc "
                    f"({', '.join(_COLUMNAS_ABC)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_ABC))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in abc[list(_COLUMNAS_ABC)].itertuples(index=False)
                    ],
                )

            # DEC-045: el puente producto↔pedido. Va en la misma transacción
            # para que el dashboard nunca lo vea a medio poblar.
            if catalogo is not None:
                con.execute("DELETE FROM catalogo_productos")
                con.executemany(
                    f"INSERT INTO catalogo_productos "
                    f"({', '.join(_COLUMNAS_CATALOGO)}, corrida_id) "
                    f"VALUES ({', '.join('?' * len(_COLUMNAS_CATALOGO))}, ?)",
                    [
                        (*_normalizar(fila), corrida_id)
                        for fila in catalogo[list(_COLUMNAS_CATALOGO)].itertuples(index=False)
                    ],
                )
        except Exception:
            con.rollback()
            raise
        con.commit()
    finally:
        con.close()

    logger.info(
        "persistir: corrida %d — %d referencias, %d anomalías, desactualizado=%s",
        corrida_id,
        resumen["referencias"],
        resumen["anomalias_filas"],
        frescura["datos_desactualizados"],
    )
    return corrida_id


def _normalizar(fila: tuple) -> tuple:
    """Convierte los tipos de numpy/pandas a tipos que sqlite3 acepta.

    `numpy.bool_` y `numpy.int64` no son adaptables por sqlite3 y
    revientan con InterfaceError; `NaN` pasa a NULL.
    """
    valores = []
    for v in fila:
        if isinstance(v, bool):
            valores.append(int(v))
        elif v is None or (isinstance(v, float) and pd.isna(v)):
            valores.append(None)
        elif hasattr(v, "item"):  # escalares de numpy
            valores.append(v.item())
        elif pd.isna(v):
            valores.append(None)
        else:
            valores.append(v)
    return tuple(valores)


def main() -> int:
    """Carga las tres fuentes, calcula la comparación y la persiste.

    Composition root del paso: es lo único que `actualizar_pedidos.bat`
    necesita invocar. AUD-B7: retorna el exit code, `sys.exit` vive en
    `__main__`.

    Returns:
        0 si la corrida se persistió, 1 si faltó una fuente o falló la
        escritura.
    """
    from inventario.clasificacion import calcular_clasificacion
    from inventario.comparacion import anomalias_layout, calcular_vendido_no_alistado, comparar
    from inventario.hallazgos import detectar_todos
    from inventario.layout import (
        RUTA_LAYOUT_DEFAULT,
        cargar_layout,
        clasificar_ubicaciones,
        solo_layout,
    )
    from inventario.normalizador import cargar_admin, cargar_bochica, filtrar_alcance_admin
    from inventario.salud import calcular_salud
    from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
    from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

    try:
        frescura = medir_frescura(ADMIN_XLSX, BOCHICA_XLSX, RUTA_LAYOUT_DEFAULT)

        catalogo_completo = cargar_admin(ADMIN_XLSX)
        # DEC-045: el puente usa el catálogo SIN filtrar por alcance — acá
        # interesa nombrar cualquier producto que aparezca en un pedido,
        # incluidas arenas y otros almacenes.
        catalogo = construir_catalogo_productos(catalogo_completo)

        admin = filtrar_alcance_admin(catalogo_completo)
        layout = cargar_layout()
        bochica = solo_layout(clasificar_ubicaciones(cargar_bochica(BOCHICA_XLSX), layout))

        comparacion = comparar(admin, bochica, calcular_vendido_no_alistado())
        anomalias = anomalias_layout(bochica)

        # DEC-047: los detectores leen el estado actual de la DB y del
        # catálogo recién descargado, así que cada corrida refleja lo más
        # reciente del sistema administrativo.
        with sqlite3.connect(get_db_path(), timeout=30) as lectura:
            hallazgos = detectar_todos(catalogo_completo, lectura)
            # DEC-049: usa el catálogo ya filtrado por alcance, para que la
            # salud y el cruce hablen del mismo universo.
            salud = calcular_salud(admin, lectura)
            abc = calcular_clasificacion(admin, lectura)

        corrida_id = persistir(
            comparacion,
            anomalias,
            frescura,
            catalogo=catalogo,
            hallazgos=hallazgos,
            salud=salud,
            abc=abc,
        )
    except FileNotFoundError as e:
        logger.error("inventario: falta una fuente — %s", e)
        return 1
    except Exception:
        logger.exception("inventario: la corrida falló sin persistir nada")
        return 1

    logger.info(
        "inventario: corrida %d persistida en pedidos.db (%d hallazgos de calidad)",
        corrida_id,
        len(hallazgos),
    )
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
