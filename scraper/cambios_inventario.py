"""
cambios_inventario.py — Captura diaria de "Cambios de inventario" (TASK-001).

Extrae y persiste el log de movimientos de inventario (ventas, devoluciones,
entradas, salidas manuales) de la vista "Cambios de inventario" del sistema
administrativo — misma sesión y usuario que el resto del scraper
(login() de extractores.py), pantalla distinta.

A diferencia de scraper/inventario.py y scraper/bochica.py (snapshot
completo del catálogo, DEC-039), esta fuente es un log incremental: cada
corrida trae solo el día anterior y las filas se acumulan sin sobrescribir.
Por eso extracción y persistencia viven en el mismo módulo, sin el cruce de
fuentes que justificó separar Bochica/admin en dos módulos.

El gate de "una vez al día" vive en main() (ya_capturado()), no en el
scheduler: el .bat llama a este módulo en cada corrida horaria sin
condición, y es esta función la que decide si ya hay trabajo hecho.
"""

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from playwright.async_api import Download, Page

from comun import get_db_path
from scraper.config import CONFIG, log_event
from scraper.extractores import SEL_BTN_BUSCAR, SEL_LISTA_FILAS, login

DESTINO_DIR = Path(__file__).parent.parent / "data" / "inventario" / "cambios_diarios"

# almacen es nullable: 4 de 1.134.916 filas del histórico lo traen vacío
# (hallazgo del análisis previo). antes_ajuste también: ~0,73% de los
# movimientos "Entrada" no traen saldo previo.
_TABLA = """
    CREATE TABLE IF NOT EXISTS movimientos_inventario (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        sku_id             TEXT    NOT NULL,
        referencia         TEXT    NOT NULL,
        nombre_producto    TEXT,
        nombre_sku         TEXT,
        almacen            TEXT,
        tipo_cambio        TEXT    NOT NULL,
        valor_ajustado     REAL    NOT NULL,
        antes_ajuste       REAL,
        despues_ajuste     REAL    NOT NULL,
        operador           TEXT    NOT NULL,
        fecha_operacion    TEXT    NOT NULL,
        extraido_en        TEXT    NOT NULL,
        corrida_id         INTEGER,
        origen             TEXT    NOT NULL DEFAULT 'diario'
    )
"""

# Clave natural: la fuente no expone un ID de movimiento propio (hallazgo
# del análisis previo). Colisiona en 347 filas del histórico sin `almacen`
# en la clave (eventos "actualización de producto" replicados una vez por
# almacén) — con almacen, baja a 12 duplicados reales sobre 1,13M filas.
_INDICE_UNICO = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_movimientos_inventario_unico
    ON movimientos_inventario
        (sku_id, fecha_operacion, tipo_cambio, valor_ajustado, almacen)
"""

_INDICE_FECHA = """
    CREATE INDEX IF NOT EXISTS idx_movimientos_inventario_fecha
    ON movimientos_inventario (fecha_operacion)
"""

_COLUMNAS_FUENTE = {
    "Nombre del producto": "nombre_producto",
    "Número de producto": "referencia",
    "Nombre de SKU": "nombre_sku",
    "SKU ID": "sku_id",
    "Almacén": "almacen",
    "Tipo de cambio": "tipo_cambio",
    "Valor ajustado": "valor_ajustado",
    "Antes del ajuste": "antes_ajuste",
    "Después del ajuste": "despues_ajuste",
    "Operador": "operador",
    "Hora de operación": "fecha_operacion",
}

_COLUMNAS_TABLA = (
    "sku_id",
    "referencia",
    "nombre_producto",
    "nombre_sku",
    "almacen",
    "tipo_cambio",
    "valor_ajustado",
    "antes_ajuste",
    "despues_ajuste",
    "operador",
    "fecha_operacion",
    "extraido_en",
    "corrida_id",
    "origen",
)


def init_schema(db_path: str) -> None:
    """Crea movimientos_inventario y sus índices si no existen. Idempotente.

    Args:
        db_path: Ruta al archivo SQLite.
    """
    with sqlite3.connect(db_path) as con:
        con.execute(_TABLA)
        con.execute(_INDICE_UNICO)
        con.execute(_INDICE_FECHA)
        con.commit()


def ya_capturado(db_path: str, fecha: date) -> bool:
    """True si ya hay al menos un movimiento cargado para esa fecha.

    Gate de "una vez al día" (TASK-001): cualquier corrida horaria del
    scheduler puede completar la captura; las siguientes corridas del
    mismo día encuentran esto en True y terminan sin abrir el navegador.

    Args:
        db_path: Ruta al archivo SQLite.
        fecha: Fecha a verificar (típicamente "ayer").

    Returns:
        True si ya existen movimientos de esa fecha en la base.
    """
    with sqlite3.connect(db_path) as con:
        fila = con.execute(
            "SELECT 1 FROM movimientos_inventario WHERE fecha_operacion LIKE ? LIMIT 1",
            (f"{fecha.isoformat()}%",),
        ).fetchone()
    return fila is not None


def _normalizar_fila(fila: tuple) -> tuple:
    """Convierte tipos de numpy/pandas a tipos que sqlite3 acepta.

    Mismo problema y misma solución que `inventario.persistencia._normalizar()`
    (numpy.int64/bool_ no son adaptables por sqlite3; NaN debe volverse
    NULL, no quedar como float NaN). Se duplica acá en vez de importarse
    porque esa función es privada de otro paquete y el proyecto mantiene
    scraper/ e inventario/ desacoplados (DEC-022) — candidato a moverse a
    comun/ si aparece una tercera necesidad.
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
    ruta_xlsx: Path,
    db_path: str,
    *,
    origen: str = "diario",
    corrida_id: int | None = None,
) -> int:
    """Parsea el Excel de "Cambios de inventario" y hace upsert en la tabla.

    INSERT OR IGNORE sobre la clave natural — seguro de reejecutar sobre el
    mismo archivo sin duplicar. `referencia` se normaliza con strip():
    ~10% de las referencias del histórico traían espacios en blanco al
    inicio o al final (hallazgo del análisis previo).

    Args:
        ruta_xlsx: Ruta al .xlsx descargado (hoja única "Sheet1", 11
            columnas — formato validado en el análisis previo).
        db_path: Ruta al archivo SQLite.
        origen: "diario" (captura automática) o "backfill" (carga histórica
            única). No afecta la carga, solo la trazabilidad del origen.
        corrida_id: Identificador de corrida a registrar, si aplica.

    Returns:
        Cuántas filas se insertaron de verdad (excluye las ya existentes).
    """
    df = pd.read_excel(ruta_xlsx, sheet_name="Sheet1")
    df = df.rename(columns=_COLUMNAS_FUENTE)
    df["referencia"] = df["referencia"].astype(str).str.strip()
    df["sku_id"] = df["sku_id"].astype(str)
    df["fecha_operacion"] = pd.to_datetime(df["fecha_operacion"]).dt.strftime("%Y-%m-%d %H:%M:%S")
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
            INSERT OR IGNORE INTO movimientos_inventario
                ({", ".join(_COLUMNAS_TABLA)})
            VALUES ({", ".join("?" * len(_COLUMNAS_TABLA))})
            """,
            filas,
        )
        con.commit()
        insertadas = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    log_event(
        "cambios_inventario_cargado",
        msg=f"{ruta_xlsx.name}: {insertadas} de {len(filas)} filas nuevas (origen={origen})",
    )
    return insertadas


async def descargar_cambios_inventario(page: Page, fecha: date, destino: Path) -> Path:
    """Descarga el Excel de "Cambios de inventario" filtrado a un solo día.

    Asume que `page` ya pasó por login() y su sesión está activa. Llena el
    rango de fechas (inicio = fin = `fecha`) directamente en los inputs del
    daterange, sin abrir el panel de calendario.

    Validado contra la app real: Element Plus acepta el valor tecleado sin
    abrir el calendario (confirmado en producción, 5 días consecutivos sin
    fallo desde 2026-08-14). Se relee el input después de llenarlo y se
    falla ruidoso si no quedó el valor esperado — filtrar mal esta fecha
    significaría traer el histórico completo en vez de un día,
    silenciosamente; el guard queda como defensa permanente, no por duda
    sobre si el llenado funciona.

    El clic en "Exportar" usa LISTADO_TIMEOUT_S (600s), no NAV_TIMEOUT_MS
    (45s): medido en producción, un día individual tarda 2,5-21,6s —
    bastante margen frente al límite, y muy por debajo del precedente de
    bochica.py (~111s para ~21.300 filas de otro export similar, un
    volumen mucho mayor por corrida).

    Args:
        page: Página Playwright con sesión autenticada (login()).
        fecha: Día a filtrar (mismo valor en "desde" y "hasta").
        destino: Ruta donde guardar el .xlsx.

    Returns:
        La misma ruta `destino`, tras confirmar que el archivo se guardó.

    Raises:
        RuntimeError: Si SCRAPER_URL_CAMBIOS_INVENTARIO no está configurada,
            o si el filtro de fecha no quedó aplicado.
    """
    if not CONFIG["url_cambios_inventario"]:
        raise RuntimeError(
            "SCRAPER_URL_CAMBIOS_INVENTARIO no está definida en .env — requerida "
            "para descargar los cambios de inventario del día anterior."
        )

    await page.goto(CONFIG["url_cambios_inventario"], timeout=CONFIG["NAV_TIMEOUT_MS"])

    fecha_str = fecha.isoformat()
    rango = page.locator(".el-date-editor--daterange .el-range-input")
    await rango.nth(0).fill(fecha_str)
    await rango.nth(0).press("Tab")
    await rango.nth(1).fill(fecha_str)
    await rango.nth(1).press("Enter")

    valores = [await rango.nth(i).input_value() for i in (0, 1)]
    if valores != [fecha_str, fecha_str]:
        raise RuntimeError(
            f"El filtro de fecha no quedó aplicado (esperado {[fecha_str, fecha_str]}, "
            f"leído {valores}) — abortando para no descargar el histórico completo."
        )

    await page.locator(SEL_BTN_BUSCAR).click()
    await page.wait_for_selector(SEL_LISTA_FILAS, timeout=CONFIG["ELEM_TIMEOUT_MS"])

    t0 = time.monotonic()
    async with page.expect_download(timeout=CONFIG["LISTADO_TIMEOUT_S"] * 1000) as descarga_info:
        await page.get_by_role("button", name="Exportar", exact=True).click()
    descarga: Download = await descarga_info.value
    duracion_ms = int((time.monotonic() - t0) * 1000)

    destino.parent.mkdir(parents=True, exist_ok=True)
    await descarga.save_as(destino)

    log_event(
        "cambios_inventario_descargado",
        duracion_ms=duracion_ms,
        msg=f"{fecha_str} guardado en {destino}",
    )
    return destino


async def main() -> int:
    """Punto de entrada diario: captura el día anterior si aún no está cargado.

    Se invoca sin condición en cada corrida horaria del scheduler — el gate
    real es ya_capturado(), no un cálculo de fecha/hora en el .bat.

    Returns:
        0 si terminó sin errores (incluye "no había nada que hacer"),
        1 si la descarga o la carga fallaron.
    """
    from playwright.async_api import async_playwright

    from scraper.config import CLAVE, USUARIO

    db_path = get_db_path()
    init_schema(db_path)

    ayer = date.today() - timedelta(days=1)
    if ya_capturado(db_path, ayer):
        log_event("cambios_inventario_ya_capturado", msg=f"{ayer.isoformat()} ya está en la base")
        return 0

    destino = DESTINO_DIR / f"cambios_inventario_{ayer.isoformat()}.xlsx"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=CONFIG["HEADLESS"],
                slow_mo=CONFIG["SLOW_MO"],
            )
            ctx = await browser.new_context(locale="es-CO")
            page = await ctx.new_page()
            await login(page, USUARIO, CLAVE)
            await descargar_cambios_inventario(page, ayer, destino)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 — cualquier fallo del navegador se reporta y sale 1
        log_event(
            "cambios_inventario_error",
            level="ERROR",
            msg=f"Fallo capturando {ayer.isoformat()}: {exc}",
        )
        return 1

    cargar_movimientos(destino, db_path, origen="diario")
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(main()))
