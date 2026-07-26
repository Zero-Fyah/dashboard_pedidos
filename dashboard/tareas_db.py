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

# Tareas MANUALES: las que no se pueden detectar midiendo los datos —
# requieren criterio o son trabajo del proyecto, no del sistema
# administrativo. Las inconsistencias medibles viven en
# `inventario/hallazgos.py` y se actualizan solas (DEC-047).
# Se siembran una sola vez: si el Arquitecto borra alguna, no reaparece.
_SEMILLA: list[tuple[str, str, str, str, str]] = [
    (
        "Estandarizar nombres de producto",
        "El mismo producto aparece con nombres distintos entre el sistema "
        "administrativo y los pedidos. Sin nombres uniformes, agrupar por "
        "producto en un reporte parte el mismo artículo en varias filas. No "
        "tiene detector automático: requiere criterio para decidir cuál es el "
        "nombre correcto.",
        "Nombres y descripciones",
        "Alta",
        "DEC-045",
    ),
    (
        "Semántica de placeholders en la columna descuento",
        "~1,14M valores no convertibles: placeholders '-' (≈920k) y etiquetas "
        "tipo 'Promoción30%' / 'Tipo de cambio3%' (≈165k) que no son montos. "
        "Es el 96% del costo estacionario del ETL — procesar basura que nunca "
        "va a convertir. Requiere decisión de negocio, no es una corrección "
        "mecánica.",
        "Montos y placeholders",
        "Media",
        "AUD-M1 / DEC-020",
    ),
    (
        "Normalizar el estado a minúsculas en el scraper",
        "Hoy las comparaciones usan LOWER() en SQL, correcto con el 100% de "
        "los datos actuales. A largo plazo conviene normalizar con .lower() en "
        "la persistencia del scraper y migrar lo existente. Es trabajo del "
        "proyecto, no del sistema administrativo.",
        "Estados",
        "Baja",
        "HAL-011",
    ),
    (
        "Explicar el picking_estimado negativo (PR11A y familia PS)",
        "El inventario de altura (fuente confiable) supera al teórico sin "
        "contar picking: −105.910 unidades agregadas, 162 referencias en "
        "negativo. PR11A concentra −270.940. Hipótesis sin verificar: el admin "
        "no publica como disponible mercancía que sí está en altura, o el "
        "conteo de altura arrastra error en las estibas completas.",
        "Ubicaciones",
        "Alta",
        "DEC-043",
    ),
    (
        "Incorporar las ubicaciones fuera del layout",
        "47% de las unidades de Bochica (3,2M) están en ubicaciones que el "
        "layout no cubre: buckets Q/R1/YU/Z, prefijos PU* y nombres de ciudad. "
        "Es mercancía recibida por peso. Queda fuera del cruce hasta tener "
        "controlado el flujo de unidades.",
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


def hay_manuales_pendientes() -> bool:
    """True si queda alguna tarea manual sin completar.

    La usa `app.py` para decidir si el menú muestra la sección de Tareas.
    Nunca levanta: si la base no se puede leer, se asume que hay tareas —
    esconder el módulo por un error transitorio sería peor que mostrarlo
    vacío.
    """
    try:
        df = listar_tareas()
    except Exception:
        return True
    return bool((df["estado"] != "Completada").any())
