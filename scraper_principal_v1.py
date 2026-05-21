"""
scraper_principal.py
====================
Scraper principal — extrae pedidos, subpedidos y líneas de productos
y los guarda en SQLite.

Uso:
    # Carga histórica completa
    python scraper_principal.py --desde 2025-05-01 --hasta 2025-05-19

    # Actualización incremental (solo pedidos activos)
    python scraper_principal.py --modo incremental
"""

import sqlite3
import logging
import argparse
import time
from datetime import date, datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

CONFIG = {
    "url_login":      "https://your-admin-system.com/login",
    "url_post_login": "https://your-admin-system.com/platform",
    "url_pedidos":    "https://your-admin-system.com/country/CO/orders/parent-orders",
    "url_detalle":    "https://your-admin-system.com/country/CO/orders/parent-orders/detail/",
    "usuario":        "fallback_user",       
    "clave":          "fallback_pass",         
    "db_path":        "pedidos.db",
    "log_path":       "scraper.log",
    "pausa":          2,                  # segundos entre pedidos
    "max_reintentos": 3,
    "estados_cerrados": ["cancelado", "entregado", "cerrado", "anulado"],
}

SEL = {
    "btn_mas_filtros": "button.el-button.is-link.expand-toggle span",
    "inputs_fecha":    ".el-range-input",
    "btn_buscar":      "button.el-button.el-button--primary span",
    "filas_pedido":    "div.el-table__body-wrapper tbody tr",
    "num_pedido":      "td:nth-child(2) span.value",
    "btn_next":        "button.btn-next.is-last",
    "info_items":      "div.info-item",
    "expand_icon":     "div.el-table__expand-icon",
    "tbody_subped":    "div.el-scrollbar__wrap--hidden-default table tbody",
    "expanded_cell":   "td.el-table__expanded-cell",
}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_path"], encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos (
            id_pedido           TEXT PRIMARY KEY,
            fecha               TEXT,
            servicio_cliente    TEXT,
            vendedor            TEXT,
            forma_pago          TEXT,
            comprobante         TEXT,
            nombre_empresa      TEXT,
            nit                 TEXT,
            metodo_entrega      TEXT,
            destinatario        TEXT,
            telefono            TEXT,
            direccion_envio     TEXT,
            observaciones       TEXT,
            scraping_completo   INTEGER DEFAULT 0,
            actualizado_en      TEXT
        );

        CREATE TABLE IF NOT EXISTS subpedidos (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            id_pedido               TEXT,
            numero_subpedido        TEXT,
            tipo_subpedido          TEXT,
            estado                  TEXT,
            inicio_alistamiento     TEXT,
            alistamiento_completado TEXT,
            alistador               TEXT,
            inicio_inspeccion       TEXT,
            inspeccion_completada   TEXT,
            inspector               TEXT,
            FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
        );

        CREATE TABLE IF NOT EXISTS lineas_pedido (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            id_pedido           TEXT,
            numero_subpedido    TEXT,
            tipo_subpedido      TEXT,
            nombre_producto     TEXT,
            referencia          TEXT,
            codigo_barras       TEXT,
            presentacion        TEXT,
            almacen             TEXT,
            cantidad_comprada   REAL,
            cantidad_entregada  REAL,
            precio_unitario     TEXT,
            descuento           TEXT,
            precio_descuento    TEXT,
            monto_pagar         TEXT,
            monto_final         TEXT,
            iva                 TEXT,
            peso_total          TEXT,
            observaciones       TEXT,
            FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
        );

        CREATE TABLE IF NOT EXISTS errores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            id_pedido   TEXT,
            momento     TEXT,
            detalle     TEXT
        );
    """)
    con.commit()
    log.info(f"Base de datos lista: {db_path}")
    return con


def guardar_pedido_cabecera(con, id_pedido: str) -> None:
    """Registra el ID del pedido para procesarlo después."""
    con.execute("""
        INSERT OR IGNORE INTO pedidos (id_pedido, scraping_completo)
        VALUES (?, 0)
    """, (id_pedido,))
    con.commit()


def guardar_detalle_pedido(con, datos: dict) -> None:
    con.execute("""
        INSERT INTO pedidos (
            id_pedido, fecha, servicio_cliente, vendedor, forma_pago,
            comprobante, nombre_empresa, nit, metodo_entrega,
            destinatario, telefono, direccion_envio, observaciones,
            scraping_completo, actualizado_en
        ) VALUES (
            :id_pedido, :fecha, :servicio_cliente, :vendedor, :forma_pago,
            :comprobante, :nombre_empresa, :nit, :metodo_entrega,
            :destinatario, :telefono, :direccion_envio, :observaciones,
            0, :actualizado_en
        )
        ON CONFLICT(id_pedido) DO UPDATE SET
            fecha               = excluded.fecha,
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
    """, {**datos, "actualizado_en": datetime.now().isoformat()})
    con.commit()


def guardar_subpedidos_y_lineas(con, id_pedido: str, subpedidos: list) -> None:
    # Elimina datos anteriores del pedido (para actualizaciones)
    con.execute("DELETE FROM subpedidos    WHERE id_pedido = ?", (id_pedido,))
    con.execute("DELETE FROM lineas_pedido WHERE id_pedido = ?", (id_pedido,))

    for sp in subpedidos:
        con.execute("""
            INSERT INTO subpedidos (
                id_pedido, numero_subpedido, tipo_subpedido, estado,
                inicio_alistamiento, alistamiento_completado, alistador,
                inicio_inspeccion, inspeccion_completada, inspector
            ) VALUES (
                :id_pedido, :numero_subpedido, :tipo_subpedido, :estado,
                :inicio_alistamiento, :alistamiento_completado, :alistador,
                :inicio_inspeccion, :inspeccion_completada, :inspector
            )
        """, {**sp, "id_pedido": id_pedido})

        for linea in sp["lineas"]:
            con.execute("""
                INSERT INTO lineas_pedido (
                    id_pedido, numero_subpedido, tipo_subpedido,
                    nombre_producto, referencia, codigo_barras, presentacion,
                    almacen, cantidad_comprada, cantidad_entregada,
                    precio_unitario, descuento, precio_descuento,
                    monto_pagar, monto_final, iva, peso_total, observaciones
                ) VALUES (
                    :id_pedido, :numero_subpedido, :tipo_subpedido,
                    :nombre_producto, :referencia, :codigo_barras, :presentacion,
                    :almacen, :cantidad_comprada, :cantidad_entregada,
                    :precio_unitario, :descuento, :precio_descuento,
                    :monto_pagar, :monto_final, :iva, :peso_total, :observaciones
                )
            """, {**linea, "id_pedido": id_pedido,
                  "numero_subpedido": sp["numero_subpedido"],
                  "tipo_subpedido":   sp["tipo_subpedido"]})

    con.execute("""
        UPDATE pedidos SET scraping_completo = 1, actualizado_en = ?
        WHERE id_pedido = ?
    """, (datetime.now().isoformat(), id_pedido))
    con.commit()


def registrar_error(con, id_pedido: str, detalle: str) -> None:
    con.execute(
        "INSERT INTO errores (id_pedido, momento, detalle) VALUES (?, ?, ?)",
        (id_pedido, datetime.now().isoformat(), detalle)
    )
    con.commit()


def pedidos_pendientes(con, modo: str) -> list[str]:
    if modo == "incremental":
        placeholders = ",".join("?" * len(CONFIG["estados_cerrados"]))
        # Busca pedidos sin completar o con subpedidos en estado no cerrado
        rows = con.execute(f"""
            SELECT DISTINCT p.id_pedido FROM pedidos p
            LEFT JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 0
               OR (s.estado IS NOT NULL AND LOWER(s.estado) NOT IN ({placeholders}))
        """, CONFIG["estados_cerrados"]).fetchall()
    else:
        rows = con.execute(
            "SELECT id_pedido FROM pedidos WHERE scraping_completo = 0"
        ).fetchall()
    return [r["id_pedido"] for r in rows]


# ─────────────────────────────────────────────
# SCRAPING — funciones
# ─────────────────────────────────────────────

def login(page) -> None:
    log.info("Iniciando sesión...")
    page.goto(CONFIG["url_login"])
    page.wait_for_load_state("networkidle")
    page.locator("input[type='email'], input[type='text']").first.fill(CONFIG["usuario"])
    page.locator("input[type='password']").first.fill(CONFIG["clave"])
    page.locator("button[type='submit'], form button").first.click()
    page.wait_for_url(CONFIG["url_post_login"], timeout=15000)
    log.info("✅ Login exitoso")

    # Cambio de idioma si está en chino
    time.sleep(1)
    btn_idioma = page.query_selector(".lang-btn")
    if btn_idioma and "中文" in btn_idioma.inner_text():
        btn_idioma.click()
        time.sleep(0.8)
        for op in page.query_selector_all(".el-dropdown-menu__item"):
            if "西班牙" in op.inner_text():
                op.click()
                time.sleep(1)
                log.info("  Idioma cambiado a español")
                break


def obtener_lista_pedidos(page, fecha_desde: str, fecha_hasta: str) -> list[str]:
    log.info(f"Consultando pedidos del {fecha_desde} al {fecha_hasta}...")
    page.goto(CONFIG["url_pedidos"])
    page.wait_for_load_state("networkidle")

    page.click(SEL["btn_mas_filtros"])
    time.sleep(1)

    page.wait_for_selector(".el-range-input", timeout=15000)
    time.sleep(1)
    inputs_fecha = page.query_selector_all(".el-range-input")
    inputs_fecha[0].click()
    inputs_fecha[0].fill(fecha_desde)
    page.keyboard.press("Tab")
    inputs_fecha[1].click()
    inputs_fecha[1].fill(fecha_hasta)
    page.keyboard.press("Enter")
    page.click("#app > div > div > div > main > div > div > div.hq-search-form > div > div > button.el-button.el-button--primary > span")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    todos_los_ids = []
    pagina_actual = 1

    while True:
        filas = page.query_selector_all(
            "#app > div > div > div > main > div > div > div:nth-child(4) > div > div > table > tbody > tr"
        )
        ids_pagina = []
        for fila in filas:
            el = fila.query_selector("td:nth-child(2) > div > div:nth-child(1) > span.value")
            if el:
                ids_pagina.append(el.inner_text().strip())

        todos_los_ids.extend(ids_pagina)
        log.info(f"  Página {pagina_actual} — {len(ids_pagina)} pedidos")

        btn_next = page.query_selector(SEL["btn_next"])
        if btn_next:
            clases = btn_next.get_attribute("class") or ""
            if "disabled" in clases or btn_next.get_attribute("disabled") is not None:
                log.info("  Última página alcanzada.")
                break
            btn_next.click()
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            pagina_actual += 1
        else:
            break

    log.info(f"Total IDs obtenidos: {len(todos_los_ids)}")
    return todos_los_ids


def extraer_info_general(page) -> dict:
    """Extrae los datos del card de información general del pedido."""
    datos = {
        "id_pedido": "", "fecha": "", "servicio_cliente": "", "vendedor": "",
        "forma_pago": "", "comprobante": "", "nombre_empresa": "", "nit": "",
        "metodo_entrega": "", "destinatario": "", "telefono": "",
        "direccion_envio": "", "observaciones": "",
    }

    # Mapa de etiquetas del sistema → nombre de columna
    mapa = {
        "número de pedido":     "id_pedido",
        "fecha del pedido":     "fecha",
        "servicio al cliente":  "servicio_cliente",
        "vendedor":             "vendedor",
        "forma de pago":        "forma_pago",
        "comprobante":          "comprobante",
        "nombre de la empresa": "nombre_empresa",
        "nit":                  "nit",
        "método de entrega":    "metodo_entrega",
        "destinatario":         "destinatario",
        "teléfono de contacto": "telefono",
        "dirección de envío":   "direccion_envio",
        "observaciones del pedido": "observaciones",
    }

    items = page.query_selector_all(SEL["info_items"])
    for item in items:
        label_el = item.query_selector(".info-label")
        value_el = item.query_selector(".info-value")
        if not label_el or not value_el:
            continue
        label = label_el.inner_text().strip().lower().rstrip("：:")
        value = value_el.inner_text().strip()
        if label in mapa:
            datos[mapa[label]] = value

    return datos


def extraer_subpedidos(page) -> list[dict]:
    """Expande todos los subpedidos y extrae sus datos y líneas de productos."""

    # Expande los que no están expandidos
    iconos = page.query_selector_all(SEL["expand_icon"])
    for icono in iconos:
        clases = icono.get_attribute("class") or ""
        if "el-table__expand-icon--expanded" not in clases:
            icono.scroll_into_view_if_needed()
            time.sleep(0.3)
            icono.click(force=True)
            try:
                page.wait_for_selector(SEL["expanded_cell"], timeout=8000)
            except PlaywrightTimeout:
                pass
            time.sleep(0.5)

    # Re-lee las filas después de expandir
    filas = page.query_selector_all(
        "div.el-scrollbar__wrap--hidden-default table tbody tr"
    )

    def txt(el, selector, default=""):
        found = el.query_selector(selector) if el else None
        return found.inner_text().strip() if found else default

    subpedidos = []
    for fila in filas:

        # ── Fila cabecera del subpedido ──────────────────────────
        if fila.query_selector("td.el-table__expand-column"):
            raw = txt(fila, "span.child-order-id")
            # Separa "Accesorios + 166657" → tipo y número
            if " + " in raw:
                partes = raw.split(" + ", 1)
                tipo_sub = partes[0].strip()
                num_sub  = partes[1].strip()
            else:
                tipo_sub = raw
                num_sub  = raw

            celdas = fila.query_selector_all("td")
            estado           = txt(celdas[3], ".el-tag__content") if len(celdas) > 3 else ""
            ini_alist        = txt(celdas[4], "div.cell")         if len(celdas) > 4 else ""
            alist_comp       = txt(celdas[5], "div.cell")         if len(celdas) > 5 else ""
            alistador        = txt(celdas[6], "div.cell")         if len(celdas) > 6 else ""
            ini_insp         = txt(celdas[7], "div.cell")         if len(celdas) > 7 else ""
            insp_comp        = txt(celdas[8], "div.cell")         if len(celdas) > 8 else ""
            inspector        = txt(celdas[9], "div.cell")         if len(celdas) > 9 else ""

            subpedidos.append({
                "numero_subpedido":        num_sub,
                "tipo_subpedido":          tipo_sub,
                "estado":                  estado,
                "inicio_alistamiento":     ini_alist,
                "alistamiento_completado": alist_comp,
                "alistador":               alistador,
                "inicio_inspeccion":       ini_insp,
                "inspeccion_completada":   insp_comp,
                "inspector":               inspector,
                "lineas":                  [],
            })

        # ── Fila de contenido expandido ──────────────────────────
        elif fila.query_selector("td.el-table__expanded-cell") and subpedidos:
            tipo_sub = subpedidos[-1]["tipo_subpedido"]
            num_sub  = subpedidos[-1]["numero_subpedido"]

            for prod_row in fila.query_selector_all("div.goods-table-row"):
                cols = prod_row.query_selector_all("div.goods-col")

                def col(i):
                    return cols[i].inner_text().strip() if i < len(cols) else ""

                info_col = cols[1] if len(cols) > 1 else None

                nombre     = txt(info_col, ".goods-name")
                referencia = txt(info_col, ".sn-tag")
                cod_barras = txt(info_col, ".goods-barcode").replace("Código de barras:", "").strip()
                presentac  = txt(info_col, ".goods-specs span")

                # Cantidad comprada y entregada como números
                def to_num(val):
                    try:
                        return float(val.replace(".", "").replace(",", "."))
                    except ValueError:
                        return 0.0

                subpedidos[-1]["lineas"].append({
                    "nombre_producto":   nombre,
                    "referencia":        referencia,
                    "codigo_barras":     cod_barras,
                    "presentacion":      presentac,
                    "almacen":           col(2),
                    "cantidad_comprada": to_num(col(3)),
                    "cantidad_entregada":to_num(col(4)),
                    "precio_unitario":   col(6),
                    "descuento":         col(7),
                    "precio_descuento":  col(8),
                    "monto_pagar":       col(9),
                    "monto_final":       col(10),
                    "iva":               col(11),
                    "peso_total":        col(12),
                    "observaciones":     col(13),
                })

    return subpedidos


def procesar_pedido(page, id_pedido: str) -> tuple[dict, list]:
    page.goto(f"{CONFIG['url_detalle']}{id_pedido}")
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

    info    = extraer_info_general(page)
    subpeds = extraer_subpedidos(page)
    return info, subpeds


# ─────────────────────────────────────────────
# ORQUESTADOR PRINCIPAL
# ─────────────────────────────────────────────

def correr_scraper(fecha_desde: str, fecha_hasta: str, modo: str) -> None:
    con = init_db(CONFIG["db_path"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=50)
        page    = browser.new_page()

        try:
            login(page)

            # Paso 1 — obtener lista de IDs
            ids = obtener_lista_pedidos(page, fecha_desde, fecha_hasta)
            for id_pedido in ids:
                guardar_pedido_cabecera(con, id_pedido)

            # Paso 2 — procesar pedidos pendientes
            pendientes = pedidos_pendientes(con, modo)
            total = len(pendientes)
            log.info(f"Pedidos a procesar: {total}")

            for i, id_pedido in enumerate(pendientes, 1):
                log.info(f"[{i}/{total}] Procesando {id_pedido}...")
                exito = False

                for intento in range(1, CONFIG["max_reintentos"] + 1):
                    try:
                        info, subpeds = procesar_pedido(page, id_pedido)
                        guardar_detalle_pedido(con, info)
                        guardar_subpedidos_y_lineas(con, id_pedido, subpeds)
                        total_lineas = sum(len(sp["lineas"]) for sp in subpeds)
                        log.info(f"  ✓ {len(subpeds)} subpedidos | {total_lineas} productos")
                        exito = True
                        break
                    except PlaywrightTimeout as e:
                        log.warning(f"  Intento {intento} — timeout")
                        time.sleep(3)
                    except Exception as e:
                        log.warning(f"  Intento {intento} — error: {e}")
                        time.sleep(3)

                if not exito:
                    log.error(f"  ✗ Falló tras {CONFIG['max_reintentos']} intentos")
                    registrar_error(con, id_pedido, "Máximo de reintentos alcanzado")

                time.sleep(CONFIG["pausa"])

        except Exception as e:
            log.critical(f"Error crítico: {e}", exc_info=True)
        finally:
            browser.close()
            con.close()

    # Resumen final
    con2 = sqlite3.connect(CONFIG["db_path"])
    ok      = con2.execute("SELECT COUNT(*) FROM pedidos WHERE scraping_completo=1").fetchone()[0]
    errores = con2.execute("SELECT COUNT(*) FROM errores").fetchone()[0]
    lineas  = con2.execute("SELECT COUNT(*) FROM lineas_pedido").fetchone()[0]
    con2.close()

    log.info("─" * 50)
    log.info(f"Pedidos completados : {ok}")
    log.info(f"Pedidos con error   : {errores}")
    log.info(f"Líneas de productos : {lineas}")
    log.info("─" * 50)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper de pedidos Calabaza Pets")
    parser.add_argument("--desde",  default="2025-05-01",          help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--hasta",  default=date.today().isoformat(), help="Fecha fin YYYY-MM-DD")
    parser.add_argument("--modo",   choices=["completo", "incremental"], default="completo")
    args = parser.parse_args()

    log.info(f"Iniciando | modo={args.modo} | {args.desde} → {args.hasta}")
    correr_scraper(args.desde, args.hasta, args.modo)
