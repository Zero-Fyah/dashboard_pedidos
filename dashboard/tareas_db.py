"""
Capa de datos del módulo de Tareas (DEC-046).

Seguimiento de las actividades de limpieza, organización y estandarización
de la información del sistema administrativo.

**Base separada, a propósito.** Es el único dato del proyecto que el
dashboard escribe y el único que no se puede regenerar: todo lo de
`pedidos.db` se reconstruye re-scrapeando, una tarea escrita a mano no.
Además `pedidos.db` ya tiene tres escritores cada hora (scraper, ETL e
inventario) y sumarle escrituras interactivas invitaría a
`database is locked` en plena corrida.

⚠️ `data/` está gitignored: estas tareas **no tienen respaldo automático**.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "tareas.db"

ESTADOS = ["Pendiente", "En progreso", "Bloqueada", "Completada"]
PRIORIDADES = ["Alta", "Media", "Baja"]
CATEGORIAS = [
    "Nombres y descripciones",
    "Códigos y referencias",
    "Estados",
    "Ubicaciones",
    "Montos y placeholders",
    "Otro",
]

COLUMNAS = ["id", "titulo", "detalle", "categoria", "prioridad", "estado", "origen"]

_CREATE_META = """
    CREATE TABLE IF NOT EXISTS meta (
        clave TEXT PRIMARY KEY,
        valor TEXT
    )
"""

_CREATE = """
    CREATE TABLE IF NOT EXISTS tareas (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        titulo        TEXT NOT NULL,
        detalle       TEXT,
        categoria     TEXT,
        prioridad     TEXT,
        estado        TEXT,
        origen        TEXT,
        creada_en     TEXT,
        actualizada_en TEXT
    )
"""

# Backlog real, medido y documentado en docs/decisions.md. Se siembra una
# sola vez (ver `_sembrar`): si el Arquitecto borra alguna, no reaparece.
_SEMILLA: list[tuple[str, str, str, str, str]] = [
    (
        "Estandarizar nombres de producto",
        "El mismo producto aparece con nombres distintos entre el sistema "
        "administrativo y los pedidos. Sin nombres uniformes, agrupar por "
        "producto en un reporte parte el mismo artículo en varias filas.",
        "Nombres y descripciones",
        "Alta",
        "DEC-045",
    ),
    (
        "Códigos de barras asociados a múltiples IDs",
        "663 códigos de barras del catálogo apuntan a 2 o más ID de "
        "especificación. El caso extremo cuelga de 21 referencias distintas "
        "(misma arena vendida por unidad, tonelada y corporativo). Impide "
        "usar el código de barras como llave de producto.",
        "Códigos y referencias",
        "Alta",
        "DEC-045",
    ),
    (
        "Unificar cómo se escribe la Especificación entre sistemas",
        "El mismo producto se escribe 'PRESENTACION: PR13 10KG MANZANA' en "
        "pedidos y 'Presentación: PR13, 10KG, MANZANA; ' en el catálogo. "
        "Solo el 32% cruza aun normalizando mayúsculas, tildes y comas. "
        "Resolverlo habilita la llave más precisa para el ID de producto.",
        "Nombres y descripciones",
        "Alta",
        "DEC-045",
    ),
    (
        "Clasificar el estado 'Entregado sin liquidar'",
        "737 subpedidos, aparecido el 2026-07-24. No está en ninguna lista de "
        "comun/, así que el ETL lo reporta como desconocido en cada corrida. "
        "730 de 737 ya tienen inicio_inspeccion y su registro llega hasta "
        "'Entrega': la mercancía ya salió. Hace que 600 pedidos figuren como "
        "activos e infla 'Días abierto'. Ojo: marcarlo cerrado haría que el "
        "scraper deje de seguirlo hasta Completado.",
        "Estados",
        "Alta",
        "Sesión 2026-07-25",
    ),
    (
        "Referencias con espacio inicial y familia 'ER'",
        "2 referencias del catálogo empiezan con espacio (' P…', 1.428 "
        "unidades en Bochica) y hay una familia 'ER' de 1 sola referencia. "
        "Con la regla familia = 2 primeros caracteres caen fuera de "
        "FAMILIAS_PRODUCTO y quedan en un cubo sin clasificar.",
        "Códigos y referencias",
        "Baja",
        "DEC-041",
    ),
    (
        "Semántica de placeholders en la columna descuento",
        "~1,14M valores no convertibles: placeholders '-' (≈920k) y etiquetas "
        "tipo 'Promoción30%' / 'Tipo de cambio3%' (≈165k) que no son montos. "
        "Es el 96% del costo estacionario del ETL — procesar basura que nunca "
        "va a convertir.",
        "Montos y placeholders",
        "Media",
        "AUD-M1 / DEC-020",
    ),
    (
        "Normalizar el estado a minúsculas en el scraper",
        "Hoy las comparaciones usan LOWER() en SQL, correcto con el 100% de "
        "los datos actuales (ningún estado con mayúscula no-ASCII). A largo "
        "plazo conviene normalizar con .lower() en la persistencia del "
        "scraper y migrar lo existente, para no depender de LOWER() en cada "
        "consulta.",
        "Estados",
        "Baja",
        "HAL-011",
    ),
    (
        "Placeholder '-' como Estado en 3 subpedidos",
        "Confirmado en vivo que NO es bug del scraper: el sistema "
        "administrativo muestra literalmente '-' en la columna Estado de "
        "esos 3 subpedidos, y también en su 'Forma de pago'. Hipótesis: "
        "nunca tuvieron forma de pago registrada al crearse. Los deja como "
        "activos indefinidamente en el dashboard.",
        "Estados",
        "Baja",
        "DEC-040",
    ),
    (
        "Explicar el picking_estimado negativo (PR11A y familia PS)",
        "El inventario de altura (fuente confiable) supera al teórico sin "
        "contar picking: −105.910 unidades agregadas, 162 referencias en "
        "negativo. PR11A concentra −270.940. Hipótesis sin verificar: el "
        "admin no publica como disponible mercancía que sí está en altura "
        "(reservas, tránsito, inactivos), o el conteo de altura arrastra "
        "error en las posiciones de estiba completa.",
        "Ubicaciones",
        "Alta",
        "DEC-043",
    ),
    (
        "Incorporar las ubicaciones fuera del layout",
        "47% de las unidades de Bochica (3,2M) están en ubicaciones que el "
        "layout no cubre: buckets Q/R1/YU/Z, prefijos PU* y nombres de "
        "ciudad. Es mercancía recibida por peso que se convierte a unidades "
        "en un proceso aparte. Queda fuera del cruce hasta tener controlado "
        "el flujo de unidades.",
        "Ubicaciones",
        "Media",
        "DEC-041",
    ),
]


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)


def init_db() -> None:
    """Crea la tabla si no existe y siembra el backlog la primera vez."""
    con = _conn()
    try:
        con.execute(_CREATE_META)
        con.execute(_CREATE)
        con.commit()
        _sembrar(con)
    finally:
        con.close()


def _sembrar(con: sqlite3.Connection) -> None:
    """Carga el backlog documentado, una sola vez en la vida de la base.

    La marca `sembrado` en `meta` —y no "¿está vacía la tabla?"— es
    deliberada: si el Arquitecto borra **todas** las tareas porque no le
    sirven, no deben reaparecer en la siguiente carga de la página. Con la
    condición de tabla vacía volverían, que es justo lo contrario.
    """
    ya = con.execute("SELECT valor FROM meta WHERE clave='sembrado'").fetchone()
    if ya:
        return
    ahora = _ahora()
    con.executemany(
        """INSERT INTO tareas
           (titulo, detalle, categoria, prioridad, estado, origen, creada_en, actualizada_en)
           VALUES (?, ?, ?, ?, 'Pendiente', ?, ?, ?)""",
        [(t, d, c, p, o, ahora, ahora) for t, d, c, p, o in _SEMILLA],
    )
    con.execute("INSERT INTO meta (clave, valor) VALUES ('sembrado', ?)", (ahora,))
    con.commit()


def _ahora() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def listar_tareas() -> pd.DataFrame:
    """Todas las tareas, ordenadas por estado de avance y prioridad.

    Sin `st.cache_data` a propósito: la tabla tiene decenas de filas y
    cachearla haría que un cambio recién guardado no se viera hasta que
    expire el TTL.
    """
    init_db()
    con = _conn()
    try:
        df = pd.read_sql(
            """SELECT id, titulo, detalle, categoria, prioridad, estado, origen
               FROM tareas
               ORDER BY CASE estado
                          WHEN 'En progreso' THEN 0
                          WHEN 'Bloqueada'   THEN 1
                          WHEN 'Pendiente'   THEN 2
                          ELSE 3 END,
                        CASE prioridad
                          WHEN 'Alta'  THEN 0
                          WHEN 'Media' THEN 1
                          ELSE 2 END,
                        id""",
            con,
        )
    finally:
        con.close()
    return df


def guardar_tareas(df: pd.DataFrame) -> tuple[int, int, int]:
    """Sincroniza la tabla con lo que quedó en el editor.

    Actualiza por `id` en vez de borrar y reinsertar todo, para conservar
    `creada_en` y que los ids no bailen entre guardados.

    Args:
        df: DataFrame salido de `st.data_editor`. Las filas nuevas traen
            `id` nulo; las que el usuario borró simplemente no están.

    Returns:
        `(nuevas, actualizadas, borradas)`.
    """
    init_db()
    ahora = _ahora()
    con = _conn()
    try:
        existentes = {r[0] for r in con.execute("SELECT id FROM tareas")}
        vistos: set[int] = set()
        nuevas = actualizadas = 0

        for fila in df.to_dict("records"):
            titulo = str(fila.get("titulo") or "").strip()
            if not titulo:
                continue  # fila vacía del editor: se ignora en vez de romper
            valores = (
                titulo,
                _texto(fila.get("detalle")),
                _texto(fila.get("categoria")),
                _texto(fila.get("prioridad")) or "Media",
                _texto(fila.get("estado")) or "Pendiente",
                _texto(fila.get("origen")),
            )
            id_fila = fila.get("id")
            if pd.isna(id_fila) or id_fila is None or int(id_fila) not in existentes:
                con.execute(
                    """INSERT INTO tareas
                       (titulo, detalle, categoria, prioridad, estado, origen,
                        creada_en, actualizada_en)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*valores, ahora, ahora),
                )
                nuevas += 1
            else:
                con.execute(
                    """UPDATE tareas SET titulo=?, detalle=?, categoria=?, prioridad=?,
                                         estado=?, origen=?, actualizada_en=?
                       WHERE id=?""",
                    (*valores, ahora, int(id_fila)),
                )
                vistos.add(int(id_fila))
                actualizadas += 1

        borradas = existentes - vistos
        if borradas:
            con.executemany("DELETE FROM tareas WHERE id=?", [(i,) for i in borradas])
        con.commit()
    finally:
        con.close()
    return nuevas, actualizadas, len(borradas)


def _texto(valor: object) -> str:
    """Normaliza a str, tratando NaN/None como cadena vacía."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    return str(valor).strip()


def resumen() -> dict[str, int]:
    """Conteo por estado — para los indicadores del encabezado."""
    df = listar_tareas()
    conteo = df["estado"].value_counts().to_dict()
    return {
        "total": len(df),
        **{e: int(conteo.get(e, 0)) for e in ESTADOS},
    }
