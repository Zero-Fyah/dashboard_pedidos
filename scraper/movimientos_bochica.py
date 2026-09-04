"""
movimientos_bochica.py — Movimientos de inventario del sistema de bodega BOCHICA.

Carga el log de traslados entre ubicaciones que Bochica expone en su propia
vista de movimientos: recibo (`PU_1_1`) → altura, altura → altura, y
altura → Picking (reabastecimiento). A diferencia del inventario que ya
descarga `scraper/bochica.py` (snapshot completo del catálogo, DEC-039),
esta fuente es un log de eventos, no un estado — mismo tipo de fuente que
`scraper/cambios_inventario.py` para el sistema administrativo, y este
módulo sigue su mismo patrón (tabla propia, clave natural, upsert).

**No cubre el descuento de Picking** (la venta/consumo desde picking): esa
sigue siendo la brecha de origen que motiva `picking_estimado = teórico −
altura` en `inventario/comparacion.py` (DEC-041). Esta fuente no la cierra;
en el mejor caso permite validar esa estimación cruzando cuánto entra a
Picking por reabastecimiento contra lo que la fórmula infiere.

Vocabulario de `tipo` medido en la carga inicial (38.056 filas,
2026-01-02 a 2026-08-25):

- `traslado` (35.289 con cantidad, 7 sin — quedan NULL, mismo criterio que
  el resto de placeholders): movimiento real entre ubicaciones.
- `error` (2.397 sin cantidad, 340 CON cantidad): intento de traslado. Que
  340 casos sí traigan cantidad muestra que "error" no siempre implica
  "sin efecto en el inventario" — este módulo no asume nada al cargar:
  guarda `tipo` y `cantidad` tal cual vienen, y la clasificación de
  impacto se decide en el análisis (`inventario/`), no acá.
- `reintegro` (23 filas): origen sintético (`REINTEGRO-<numero>`, no una
  ubicación física de Bochica). Verificado contra el layout real
  (`inventario/layout.py::clasificar_ubicaciones`): las 23 caen en
  destinos `Picking` — consistente con que el reintegro no toca altura,
  medido sobre esta carga, no asumido de la descripción del Arquitecto.

El scraper diario reutiliza el patrón de `cambios_inventario.py` (decisión
confirmada por el Arquitecto sobre la alternativa de un gate fijo a las
23:00): captura "ayer completo", autoverificado con `ya_cargado()`, sin
condición de hora en el `.bat`. La actividad medida de bodega es 0 entre
las 22:00 y las 05:00 (carga inicial, 38.056 filas) — "ayer completo" y
"hoy hasta las 23:00" son el mismo dato, y el patrón de "ayer" es más
robusto: se autocorrige en el siguiente ciclo horario si uno falla, un
gate fijo a una hora no.

`archivar_snapshot_bochica()` cierra la segunda mitad del pedido — una
fotografía diaria del inventario de Bochica para arrancar la jornada
siguiente y quedar en histórico. No dispara una descarga nueva: copia el
snapshot que `scraper.bochica` ya trae fresco en cada ciclo horario
(DEC-039), apoyada en la misma medición de actividad nula 22:00-05:00, así
que el snapshot del primer ciclo del día siguiente equivale al cierre del
día anterior. Retención de 30 días (mismo criterio que la rotación de logs,
DEC-028) — 7,0 MB por archivo medido, ~2,5 GB/año sin purgar.

`descargar_movimientos_bochica()` navega Montacargas > Movimientos (misma
URL y mismo login de dos capas que `scraper.bochica`), filtra Bodega Origen
y Bodega Destino a MOSQUERA y el rango a un solo día, y lee la tabla en
pantalla celda por celda — esta vista no tiene botón "Exportar" como
`Cambios de inventario` (confirmado por el Arquitecto sobre el DOM real).

**Validado en vivo dos veces (2026-08-25, manual, fuera del scheduler —
fecha de Colombia; los `log_event()` de esa corrida muestran 2026-08-26
porque su timestamp es UTC, mismo desfase que ya documentó AUD-B6):**
251 filas leídas para el 2026-08-25, idéntico a lo persistido en la carga
inicial para ese mismo día (243 traslado + 8 error). Un "252" observado
antes en pantalla no se reprodujo en ninguna de las tres lecturas
posteriores (dos extracciones + una lectura directa de `#movCount`, todas
en 251) — se trató como una lectura puntual distinta, no como un bug.

**Auditoría del 2026-08-25 (agente scraping-specialist) encontró dos
riesgos de diseño reales y ya corregidos:** (1) las celdas se mapeaban por
posición sin validar el encabezado real de `#movTabla` — mismo patrón que
BUG-019/DEC-122, un reordenamiento silencioso del origen mapearía cada
celda al campo equivocado sin error; ahora se compara `thead th` contra
`_COLUMNAS_FUENTE` y se aborta si difiere. (2) el wait de `#movLoadingMsg`
podía resolver antes de que la tabla terminara de poblarse si el indicador
tardaba en aparecer (`wait_for(state="hidden")` no espera una transición,
solo evalúa el estado actual) — ahora la espera exige además que
`#movCount` tenga contenido, y se agregó una verificación cruzada que
compara ese conteo contra las filas leídas, con WARNING si no coinciden.

**Re-validado en vivo con las dos correcciones aplicadas:** la primera
corrida con el chequeo de encabezado dio un falso positivo real —
`&lt;th&gt;` se ve en MAYÚSCULAS por CSS (`text-transform`) y
`all_inner_texts()` devuelve el texto renderizado, no el crudo del DOM.
Comparación pasada a insensible a mayúsculas; segunda corrida: encabezado
válido, sin advertencia de conteo, 251 filas — tercera coincidencia
consecutiva contra el mismo dato persistido.
"""

import logging
import re
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from playwright.async_api import Page

from comun import get_db_path
from scraper.bochica import DESTINO_DEFAULT as _SNAPSHOT_BOCHICA_VIVO
from scraper.bochica import frame_con_selector, login_app_bochica, pagina_con_sesion_guardada
from scraper.config import CONFIG, log_event

logger = logging.getLogger("scraper.movimientos_bochica")

_TABLA = """
    CREATE TABLE IF NOT EXISTS movimientos_bochica (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha_operacion    TEXT    NOT NULL,
        bodega_origen      TEXT    NOT NULL,
        ubicacion_origen   TEXT    NOT NULL,
        bodega_destino     TEXT    NOT NULL,
        ubicacion_destino  TEXT    NOT NULL,
        sku_id             TEXT    NOT NULL,
        cantidad           REAL,
        lote               TEXT,
        vencimiento        TEXT,
        responsable        TEXT    NOT NULL,
        tipo               TEXT    NOT NULL,
        extraido_en        TEXT    NOT NULL,
        corrida_id         INTEGER,
        origen             TEXT    NOT NULL DEFAULT 'backfill'
    )
"""

# Clave natural: la fuente no expone un ID de movimiento propio (mismo
# problema que movimientos_inventario). Verificado sin colisiones sobre las
# 38.056 filas de la carga inicial, con y sin `cantidad` en la clave — se
# deja fuera porque es NULL en ~6% de las filas (7 traslados + 2.397
# errores) y SQLite trata cada NULL como distinto en un índice UNIQUE, lo
# que le quitaría poder de-duplicador justo donde más se necesita.
_INDICE_UNICO = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_movimientos_bochica_unico
    ON movimientos_bochica
        (fecha_operacion, ubicacion_origen, ubicacion_destino, sku_id, responsable, tipo)
"""

_INDICE_FECHA = """
    CREATE INDEX IF NOT EXISTS idx_movimientos_bochica_fecha
    ON movimientos_bochica (fecha_operacion)
"""

# DEC-122 costó un incidente real por asumir en vez de avisar cuando el
# origen agrega vocabulario nuevo. Acá no se bloquea la carga —el tipo
# igual se guarda— pero sí se avisa fuerte para que no pase inadvertido.
_TIPOS_CONOCIDOS = frozenset({"traslado", "error", "reintegro"})

_COLUMNAS_FUENTE = {
    "Fecha": "fecha_operacion",
    "Bodega Origen": "bodega_origen",
    "Ubicación Origen": "ubicacion_origen",
    "Bodega Destino": "bodega_destino",
    "Ubicación Destino": "ubicacion_destino",
    "Producto": "sku_id",
    "Cantidad": "cantidad",
    "Lote": "lote",
    "Vencimiento": "vencimiento",
    "Responsable": "responsable",
    "Tipo": "tipo",
}

_COLUMNAS_TABLA = (
    "fecha_operacion",
    "bodega_origen",
    "ubicacion_origen",
    "bodega_destino",
    "ubicacion_destino",
    "sku_id",
    "cantidad",
    "lote",
    "vencimiento",
    "responsable",
    "tipo",
    "extraido_en",
    "corrida_id",
    "origen",
)

# "-" y "—" (raya, U+2014) conviven en la misma columna — mismo patrón que
# ya se resolvió para `Cambios de inventario` del sistema administrativo.
_PLACEHOLDERS = frozenset({"-", "—"})

CARPETA_HISTORICO_BOCHICA = (
    Path(__file__).parent.parent / "data" / "inventario" / "historico_bochica"
)

# Mismo criterio que la rotación de logs (DEC-028): 7,0 MB medidos por
# snapshot, ~2,5 GB/año sin purgar en un equipo de 8 GB.
RETENCION_HISTORICO_DIAS = 30

# Única bodega en alcance (DEC-039) — mismo criterio que el resto del
# cruce de inventario, que solo opera Mosquera.
BODEGA_MOVIMIENTOS = "MOSQUERA"

DESTINO_DIR_DIARIO = Path(__file__).parent.parent / "data" / "inventario" / "movimientos_diarios"


def init_schema(db_path: str) -> None:
    """Crea movimientos_bochica y sus índices si no existen. Idempotente.

    Args:
        db_path: Ruta al archivo SQLite.
    """
    with sqlite3.connect(db_path) as con:
        con.execute(_TABLA)
        con.execute(_INDICE_UNICO)
        con.execute(_INDICE_FECHA)
        con.commit()


def ya_cargado(db_path: str, fecha: date) -> bool:
    """True si ya hay al menos un movimiento cargado para esa fecha.

    Pensado para el futuro scraper diario — mismo gate de "una vez al día"
    que `cambios_inventario.ya_capturado()`. Sin consumidor todavía porque
    el scraper diario no está construido.

    Args:
        db_path: Ruta al archivo SQLite.
        fecha: Fecha a verificar.

    Returns:
        True si ya existen movimientos de esa fecha en la base.
    """
    with sqlite3.connect(db_path) as con:
        fila = con.execute(
            "SELECT 1 FROM movimientos_bochica WHERE fecha_operacion LIKE ? LIMIT 1",
            (f"{fecha.isoformat()}%",),
        ).fetchone()
    return fila is not None


def _a_nulo(serie: pd.Series) -> pd.Series:
    """Convierte los placeholders de la fuente ('-', '—') a NaN real."""
    return serie.where(~serie.isin(_PLACEHOLDERS))


def _parsear_fecha(serie: pd.Series) -> pd.Series:
    """Convierte 'D/M/YYYY, H:MM:SS a. m./p. m.' a ISO 'YYYY-MM-DD HH:MM:SS'.

    Formato confirmado sobre las 38.056 filas de la carga inicial: día y
    mes sin cero a la izquierda, AM/PM con puntos y espacios ("a. m.", no
    "a.m."), un solo separador decimal de hora (segundos incluidos).
    """
    normalizado = serie.str.replace(r"(?i)a\.\s*m\.", "AM", regex=True).str.replace(
        r"(?i)p\.\s*m\.", "PM", regex=True
    )
    return pd.to_datetime(normalizado, format="%d/%m/%Y, %I:%M:%S %p").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _normalizar_fila(fila: tuple) -> tuple:
    """Convierte tipos de numpy/pandas a tipos que sqlite3 acepta.

    Tercera copia de esta misma lógica (la primera en
    `inventario.persistencia._normalizar()`, la segunda en
    `scraper.cambios_inventario._normalizar_fila()`). Sigue sin moverse a
    `comun/`: ese módulo es deliberadamente solo-stdlib (AUD-M5) para que
    cualquier etapa pueda importarlo sin arrastrar pandas, y esta función
    depende de `pd.isna()`. La tercera aparición confirma el patrón, no
    invalida la razón de no compartirlo.
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


def cargar_movimientos(
    ruta: Path,
    db_path: str,
    *,
    origen: str = "backfill",
    corrida_id: int | None = None,
) -> int:
    """Parsea el archivo de movimientos de Bochica y hace upsert en la tabla.

    Acepta `.txt`/`.csv` tabulado (formato de la carga inicial, pegado
    desde la vista de Bochica) o `.xlsx` (formato esperado si el futuro
    scraper diario exporta como el resto de descargas de Bochica).
    `INSERT OR IGNORE` sobre la clave natural — seguro de reejecutar sobre
    el mismo archivo sin duplicar.

    No filtra ni reinterpreta `tipo`: `error` y `reintegro` se guardan tal
    cual vienen, con su `cantidad` real si la traen. Decidir qué impacto
    tiene cada tipo en el cálculo de inventario es responsabilidad del
    consumidor en `inventario/`, no de la carga.

    Args:
        ruta: Ruta al archivo descargado/exportado.
        db_path: Ruta al archivo SQLite.
        origen: "backfill" (carga histórica única) o "diario" (captura
            automática, `main()`).
        corrida_id: Identificador de corrida a registrar, si aplica.

    Returns:
        Cuántas filas se insertaron de verdad (excluye las ya existentes).
    """
    if ruta.suffix.lower() in (".txt", ".csv"):
        df = pd.read_csv(ruta, sep="\t", dtype=str, encoding="utf-8")
    else:
        df = pd.read_excel(ruta, sheet_name=0, dtype=str)

    df = df.rename(columns=_COLUMNAS_FUENTE)

    tipos_nuevos = set(df["tipo"].dropna().unique()) - _TIPOS_CONOCIDOS
    if tipos_nuevos:
        logger.warning(
            "cargar_movimientos: tipo(s) no reconocido(s) en la fuente: %s — "
            "se cargan igual, sin clasificar su impacto (revisar antes de "
            "consumirlos en inventario/)",
            sorted(tipos_nuevos),
        )

    df["sku_id"] = df["sku_id"].astype(str).str.strip()
    df["fecha_operacion"] = _parsear_fecha(df["fecha_operacion"])

    cantidad_sin_placeholder = _a_nulo(df["cantidad"])
    df["cantidad"] = pd.to_numeric(cantidad_sin_placeholder, errors="coerce")
    # Mismo criterio que el aviso de tipos nuevos: un valor que no es
    # placeholder conocido ni numérico (separador de miles, texto suelto)
    # no debe convertirse en NULL en silencio.
    no_convertidos = cantidad_sin_placeholder.notna() & df["cantidad"].isna()
    if no_convertidos.any():
        logger.warning(
            "cargar_movimientos: %d valor(es) de cantidad no son placeholder "
            "ni número — quedan NULL, muestra: %s",
            int(no_convertidos.sum()),
            sorted(set(cantidad_sin_placeholder[no_convertidos].astype(str)))[:5],
        )

    df["lote"] = _a_nulo(df["lote"])
    df["vencimiento"] = _a_nulo(df["vencimiento"])
    df["extraido_en"] = pd.Timestamp.now().isoformat(timespec="seconds")
    df["origen"] = origen
    df["corrida_id"] = corrida_id

    filas = [
        _normalizar_fila(fila)
        for fila in df[list(_COLUMNAS_TABLA)].itertuples(index=False, name=None)
    ]

    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys = ON")
        cursor = con.executemany(
            f"""
            INSERT OR IGNORE INTO movimientos_bochica
                ({", ".join(_COLUMNAS_TABLA)})
            VALUES ({", ".join("?" * len(_COLUMNAS_TABLA))})
            """,
            filas,
        )
        con.commit()
        insertadas = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    log_event(
        "movimientos_bochica_cargado",
        msg=f"{ruta.name}: {insertadas} de {len(filas)} filas nuevas (origen={origen})",
    )
    return insertadas


def archivar_snapshot_bochica(
    fecha: date,
    *,
    origen: Path | None = None,
    carpeta_historico: Path | None = None,
) -> Path | None:
    """Archiva una copia fechada del snapshot de Bochica como "cierre del día".

    No dispara una descarga nueva: copia el archivo que `scraper.bochica` ya
    trae fresco en cada ciclo horario (DEC-039). Válido porque la actividad
    de bodega medida es 0 entre las 22:00 y las 05:00 (carga inicial,
    38.056 filas) — el snapshot del primer ciclo del día siguiente equivale
    al cierre del día que se está archivando.

    Best-effort, mismo criterio que el respaldo de conteos (DEC-120): si el
    snapshot vivo no existe o la copia falla, se avisa por WARNING y no se
    interrumpe la captura de movimientos que lo dispara.

    Args:
        fecha: Día que representa el snapshot (típicamente "ayer").
        origen: Ruta al snapshot vivo. Default: el que descarga
            `scraper.bochica`.
        carpeta_historico: Carpeta destino. Default: `CARPETA_HISTORICO_BOCHICA`.

    Returns:
        La ruta archivada, o None si no había snapshot que copiar o la
        copia falló.
    """
    origen = origen or _SNAPSHOT_BOCHICA_VIVO
    carpeta_historico = carpeta_historico or CARPETA_HISTORICO_BOCHICA

    if not origen.exists():
        logger.warning(
            "archivar_snapshot_bochica: %s no existe — nada que archivar para %s",
            origen,
            fecha.isoformat(),
        )
        return None

    carpeta_historico.mkdir(parents=True, exist_ok=True)
    destino = carpeta_historico / f"bochica_inventario_{fecha.isoformat()}.xlsx"
    try:
        shutil.copy2(origen, destino)
    except OSError as exc:
        logger.warning("archivar_snapshot_bochica: copia a %s falló: %s", destino, exc)
        return None

    _purgar_historico_viejo(carpeta_historico, fecha)
    return destino


def _purgar_historico_viejo(carpeta: Path, hoy: date) -> None:
    """Elimina del histórico los snapshots con más de RETENCION_HISTORICO_DIAS.

    La fecha límite se lee del nombre del archivo
    (`bochica_inventario_YYYY-MM-DD.xlsx`), no de su fecha de modificación
    en disco — así una copia o un respaldo externo no reinicia la cuenta.
    """
    limite = hoy - timedelta(days=RETENCION_HISTORICO_DIAS)
    for archivo in carpeta.glob("bochica_inventario_*.xlsx"):
        try:
            fecha_archivo = date.fromisoformat(archivo.stem.removeprefix("bochica_inventario_"))
        except ValueError:
            continue
        if fecha_archivo < limite:
            try:
                archivo.unlink()
            except OSError as exc:
                logger.warning("_purgar_historico_viejo: no se pudo borrar %s: %s", archivo, exc)


async def descargar_movimientos_bochica(page: Page, fecha: date, destino: Path) -> Path:
    """Extrae los movimientos de Bochica de un día y los guarda como .xlsx.

    A diferencia de `bochica.descargar_inventario_global()` (botón de
    descarga real), la vista Montacargas > Movimientos no expone
    exportación: hay que leer la tabla en pantalla y reconstruir el archivo
    acá, celda por celda, en el mismo orden de columnas que trae la fuente.

    Asume que `page` ya pasó por `pagina_con_sesion_guardada()` y
    `login_app_bochica()`. Abre el menú lateral y el grupo "Montacargas" si
    están colapsados (misma señal de clase `open` que usa
    `descargar_inventario_global()` para el sidebar), navega a
    "Movimientos", filtra Bodega Origen y Bodega Destino a
    `BODEGA_MOVIMIENTOS` y el rango de fecha a un solo día.

    Args:
        page: Página Playwright con las dos sesiones (Google + app) activas.
        fecha: Día a filtrar (mismo valor en "desde" y "hasta").
        destino: Ruta donde guardar el .xlsx reconstruido.

    Returns:
        La misma ruta `destino`.

    Raises:
        RuntimeError: Si el filtro de fecha no quedó aplicado, o si el
            encabezado real de la tabla no coincide con el esperado.
    """
    frame = await frame_con_selector(page, "#movBodegaOrigen", CONFIG["ELEM_TIMEOUT_MS"])

    sidebar_class = await frame.locator("#sidebar").get_attribute("class") or ""
    if "open" not in sidebar_class:
        await frame.locator("button.hamburger").click()

    grupo = frame.locator("#navgroup-montacargas")
    grupo_class = await grupo.get_attribute("class") or ""
    if "open" not in grupo_class:
        await frame.locator("button.nav-parent", has_text="Montacargas").click()

    # Acotado al grupo ya abierto, no a todo el frame: "Movimientos" es un
    # texto genérico y el sidebar tiene módulos con nombres parecidos
    # (Hablador, Ubicar comparten data-module con este). La colisión real
    # de "Buscar" más abajo ya mostró que varios módulos coexisten ocultos
    # en el mismo DOM — acotar el contenedor evita repetirla acá.
    await grupo.locator("button.nav-child", has_text="Movimientos").click(
        timeout=CONFIG["ELEM_TIMEOUT_MS"]
    )

    await frame.locator("#movBodegaOrigen").select_option(BODEGA_MOVIMIENTOS)
    await frame.locator("#movBodegaDestino").select_option(BODEGA_MOVIMIENTOS)

    # <input type="date"> toma siempre el valor ISO por su .value, sin
    # importar el formato día/mes/año que muestra el selector nativo del
    # navegador — no es el mismo caso que el daterange de texto libre de
    # cambios_inventario.py.
    fecha_iso = fecha.isoformat()
    await frame.locator("#movFechaDesde").fill(fecha_iso)
    await frame.locator("#movFechaHasta").fill(fecha_iso)

    valores = [
        await frame.locator(sel).input_value() for sel in ("#movFechaDesde", "#movFechaHasta")
    ]
    if valores != [fecha_iso, fecha_iso]:
        raise RuntimeError(
            f"El filtro de fecha no quedó aplicado (esperado {[fecha_iso, fecha_iso]}, "
            f"leído {valores}) — abortando para no traer un rango distinto al pedido."
        )

    # Todos los módulos del sidebar coexisten en el DOM (ocultos por CSS, no
    # removidos): "button.btn-orange" con texto "Buscar" matchea 5 botones
    # de módulos distintos (Conteo, Recepción, Puerto, Lotes y este). El
    # onclick exacto es el único selector que no colisiona — mismo criterio
    # que descargar_inventario_global() usa para su botón de descarga.
    await frame.locator("button[onclick='movBuscar()']").click()

    # Espera sobre dos señales combinadas, no solo el indicador de carga:
    # wait_for(state="hidden") resuelve apenas la condición es cierta EN EL
    # MOMENTO en que empieza a evaluarse, no espera una transición
    # visible→oculto. Si el indicador tarda en aparecer más de lo esperado,
    # un wait_for(hidden) aislado podría resolver antes de que la tabla
    # termine de poblarse (mismo modo de falla que BUG-016: el render real
    # queda un paso detrás del wait). Exigir además que #movCount tenga
    # contenido ata la espera al dato que la búsqueda produce, no a un
    # proxy de UI.
    await frame.wait_for_function(
        """() => {
            const loading = document.getElementById('movLoadingMsg');
            const cargando = loading && getComputedStyle(loading).display !== 'none';
            const count = document.getElementById('movCount');
            return !cargando && !!count && count.textContent.trim().length > 0;
        }""",
        timeout=CONFIG["LISTADO_TIMEOUT_S"] * 1000,
    )

    # El encabezado real de la tabla debe coincidir en orden con
    # _COLUMNAS_FUENTE: las celdas se mapean por posición más abajo, y un
    # reordenamiento silencioso del origen (mismo riesgo que BUG-019 y
    # DEC-122) mapearía cada celda al campo equivocado sin ningún error.
    # Comparación insensible a mayúsculas: los <th> se ven en mayúsculas por
    # CSS (text-transform) y `all_inner_texts()` devuelve el texto
    # renderizado, no el crudo del DOM — confirmado en vivo (2026-08-25),
    # no una suposición.
    encabezados = await frame.locator("#movTabla thead th").all_inner_texts()
    esperado = list(_COLUMNAS_FUENTE)
    if [h.strip().upper() for h in encabezados] != [e.upper() for e in esperado]:
        raise RuntimeError(
            f"El encabezado de #movTabla cambió — esperado {esperado}, "
            f"leído {encabezados}. Abortando para no mapear celdas a la "
            "columna equivocada (ver DEC-122)."
        )

    filas_dom = await frame.locator("#movTbody tr").all()
    filas = [await fila.locator("td").all_inner_texts() for fila in filas_dom]

    # Bochica renderiza un día sin movimientos como una única fila con una
    # sola celda (colspan), ej. "Sin resultados para los filtros aplicados."
    # — no es un error de la fuente, es un resultado real (confirmado en
    # vivo 2026-09-03 contra 2026-08-30, día sin actividad de bodega:
    # #movCount decía "0 movimientos encontrados"). Sin este chequeo,
    # pd.DataFrame(filas, columns=_COLUMNAS_FUENTE) revienta con "N columns
    # passed, passed data had 1 columns" y esa fecha queda sin poder
    # registrarse nunca — ya_cargado() nunca ve una fila con esa
    # fecha_operacion, así que el .bat reintenta cada hora y falla cada hora
    # hasta que "ayer" avanza al día siguiente (incidente real: 19 fallos
    # consecutivos el 2026-08-31 tratando de capturar el 2026-08-30).
    if len(filas) == 1 and len(filas[0]) != len(_COLUMNAS_FUENTE):
        placeholder = filas[0][0] if filas[0] else ""
        logger.info(
            "descargar_movimientos_bochica: %s sin movimientos (placeholder de la "
            "fuente: %r) — se registran 0 filas, no es un error",
            fecha_iso,
            placeholder,
        )
        filas = []

    # Verificación cruzada contra el propio conteo de la fuente. No bloquea
    # la carga —podría ser un desfase inocuo de la UI— pero deja evidencia
    # en el log si alguna vez se pierde una fila en el render en vez de
    # asumir en silencio que #movCount y la tabla siempre coinciden.
    texto_count = (await frame.locator("#movCount").inner_text()).strip()
    match_count = re.search(r"\d+", texto_count)
    if match_count and int(match_count.group()) != len(filas):
        logger.warning(
            "descargar_movimientos_bochica: %s dice %r pero se leyeron %d filas — "
            "posible fila perdida en el render, revisar antes de confiar en esta carga",
            fecha_iso,
            texto_count,
            len(filas),
        )

    df = pd.DataFrame(filas, columns=list(_COLUMNAS_FUENTE))
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(destino, sheet_name="Sheet1", index=False)

    log_event(
        "movimientos_bochica_descargado",
        msg=f"{fecha_iso}: {len(filas)} filas leídas de la tabla en pantalla, guardadas en {destino}",
    )
    return destino


async def main() -> int:
    """Punto de entrada diario: captura el día anterior si aún no está cargado.

    Mismo patrón que `cambios_inventario.main()`: sin condición de fecha/hora
    en el `.bat` — este módulo decide solo, con `ya_cargado()`, si ya hay
    trabajo hecho para "ayer" y termina de inmediato si sí.

    Además de cargar los movimientos, archiva el snapshot de Bochica de ese
    mismo día como cierre de jornada (`archivar_snapshot_bochica()`) — best
    effort, no aborta la corrida si falla.

    Returns:
        0 si terminó sin errores (incluye "no había nada que hacer"),
        1 si la descarga o la carga fallaron.
    """
    from playwright.async_api import async_playwright

    db_path = get_db_path()
    init_schema(db_path)

    ayer = date.today() - timedelta(days=1)
    if ya_cargado(db_path, ayer):
        log_event("movimientos_bochica_ya_cargado", msg=f"{ayer.isoformat()} ya está en la base")
        return 0

    destino = DESTINO_DIR_DIARIO / f"movimientos_bochica_{ayer.isoformat()}.xlsx"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CONFIG["HEADLESS"],
                slow_mo=CONFIG["SLOW_MO"],
            )
            # Aislado del resto: pagina_con_sesion_guardada() es el único
            # punto que lanza RuntimeError por sesión expirada (mensaje
            # accionable: correr --sembrar-sesion). Un evento propio evita
            # que ese caso quede mezclado bajo el mismo nombre genérico que
            # un selector roto o un timeout de red — la sesión de Bochica es
            # el punto de falla operativo más crítico del módulo.
            try:
                page = await pagina_con_sesion_guardada(browser)
            except RuntimeError as exc:
                await browser.close()
                log_event("movimientos_bochica_sesion_expirada", level="ERROR", msg=str(exc))
                return 1
            await login_app_bochica(
                page, CONFIG["bochica_app_usuario"], CONFIG["bochica_app_clave"]
            )
            await descargar_movimientos_bochica(page, ayer, destino)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 — cualquier otro fallo se reporta y sale 1
        log_event(
            "movimientos_bochica_error",
            level="ERROR",
            msg=f"Fallo capturando {ayer.isoformat()}: {exc}",
        )
        return 1

    cargar_movimientos(destino, db_path, origen="diario")
    archivar_snapshot_bochica(ayer)
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(main()))
