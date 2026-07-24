"""
normalizador.py — Carga y cruce de los Excel de inventario (DEC-039).

Normaliza los dos documentos fuente (admin_inventario.xlsx,
bochica_inventario.xlsx) a un esquema común y los cruza por el
identificador de producto compartido (`ID de especificación` en el
admin, `ID Producto` en Bochica — confirmado 71,5% de cruce, ver
docs/decisions.md DEC-039 pregunta 1).

Deja listo el inventario cruzado salvo la clasificación picking/altura,
que necesita el layout de bodega (documento 3, aún pendiente) — no
inventar esa lógica acá.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("inventario.normalizador")

# Placeholders de Bochica que no representan inventario real (DEC-039
# pregunta 2, decisión 2026-07-23) — contenedores reutilizables y
# artefactos de migración/zonas de tránsito, no posiciones físicas.
BOCHICA_ID_PLACEHOLDER = "0"
BOCHICA_UBICACIONES_PLACEHOLDER = frozenset({"_MIGRADO", "subbodega"})


def cargar_admin(path: Path) -> pd.DataFrame:
    """Carga y normaliza el Excel de inventario del sistema administrativo.

    Args:
        path: Ruta al .xlsx descargado por scraper/inventario.py.

    Returns:
        DataFrame con columnas snake_case; `id_especificacion` (str) es
        la llave de cruce contra `cargar_bochica()`.
    """
    df = pd.read_excel(path)
    df = df.rename(
        columns={
            "Identificación del producto": "id_producto_admin",
            "ID de especificación": "id_especificacion",
            "Nombre comercial": "nombre_comercial",
            "Inventario": "inventario",
            "Referencia del producto.": "referencia",
            "ALMACEN": "almacen",
            "Categoria del producto": "categoria",
            "Especificación": "especificacion",
            "Código de barras.": "codigo_barras",
            "Precio": "precio",
            "Producto activo o inactivo": "producto_activo",
            "Descuento": "descuento",
            "IVA": "iva",
        }
    )
    df["id_especificacion"] = df["id_especificacion"].astype(str)
    columnas = [
        "id_especificacion",
        "id_producto_admin",
        "nombre_comercial",
        "referencia",
        "almacen",
        "categoria",
        "especificacion",
        "codigo_barras",
        "inventario",
        "precio",
        "producto_activo",
        "descuento",
        "iva",
    ]
    return df[columnas]


def cargar_bochica(path: Path, *, excluir_placeholders: bool = True) -> pd.DataFrame:
    """Carga y normaliza el Excel de inventario global de Bochica.

    Args:
        path: Ruta al .xlsx descargado por scraper/bochica.py.
        excluir_placeholders: Si True (default), descarta las filas de
            contenedores reutilizables (`ID Producto='0'`) y de
            ubicaciones de tránsito/migración (`_MIGRADO`, `subbodega`)
            — ver docs/decisions.md DEC-039 pregunta 2.

    Returns:
        DataFrame con columnas snake_case; `id_especificacion` (str) es
        la llave de cruce contra `cargar_admin()`. `ubicacion` se
        conserva tal cual (sin normalizar mayúsculas) — la ambigüedad
        de mayúscula/minúscula sigue sin resolver, ver DEC-039.
    """
    df = pd.read_excel(path)
    df = df.rename(
        columns={
            "ID Producto": "id_especificacion",
            "Referencia": "referencia",
            "Especificación": "especificacion",
            "Ubicación": "ubicacion",
            "BL": "bl",
            "Cantidad": "cantidad",
            "Lote": "lote",
            "Fecha Vencimiento": "fecha_vencimiento",
        }
    )
    df["id_especificacion"] = df["id_especificacion"].astype(str)

    if excluir_placeholders:
        total = len(df)
        mascara_placeholder = (df["id_especificacion"] == BOCHICA_ID_PLACEHOLDER) | (
            df["ubicacion"].isin(BOCHICA_UBICACIONES_PLACEHOLDER)
        )
        excluidas = int(mascara_placeholder.sum())
        df = df[~mascara_placeholder]
        if excluidas:
            logger.info(
                "cargar_bochica: excluidas %d/%d filas placeholder (%s)",
                excluidas,
                total,
                path,
            )

    columnas = [
        "id_especificacion",
        "referencia",
        "especificacion",
        "ubicacion",
        "bl",
        "cantidad",
        "lote",
        "fecha_vencimiento",
    ]
    return df[columnas]


def cruzar_inventarios(df_admin: pd.DataFrame, df_bochica: pd.DataFrame) -> pd.DataFrame:
    """Cruza el inventario del admin con el de Bochica por `id_especificacion`.

    Outer join deliberado (no inner): las filas de Bochica sin match en
    el admin (códigos legados fuera de catálogo, ~28,5% medido) quedan
    visibles con las columnas del admin en NaN, en vez de descartarse en
    silencio — ver DEC-039 pregunta 1.

    Args:
        df_admin: Resultado de `cargar_admin()`.
        df_bochica: Resultado de `cargar_bochica()`.

    Returns:
        DataFrame cruzado con sufijos `_admin`/`_bochica` en las
        columnas duplicadas (`referencia`, `especificacion`) y una
        columna `_merge` (pandas) indicando el origen de cada fila:
        "both", "left_only" (solo admin) o "right_only" (solo Bochica).
    """
    return df_admin.merge(
        df_bochica,
        on="id_especificacion",
        how="outer",
        suffixes=("_admin", "_bochica"),
        indicator=True,
    )


def resumen_cruce(df_cruzado: pd.DataFrame) -> dict[str, int]:
    """Cuenta filas por origen tras `cruzar_inventarios()` — para logging/diagnóstico.

    Args:
        df_cruzado: Resultado de `cruzar_inventarios()`.

    Returns:
        Diccionario con las claves "both", "solo_admin", "solo_bochica".
    """
    conteo = df_cruzado["_merge"].value_counts()
    return {
        "both": int(conteo.get("both", 0)),
        "solo_admin": int(conteo.get("left_only", 0)),
        "solo_bochica": int(conteo.get("right_only", 0)),
    }


if __name__ == "__main__":
    import sys

    from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
    from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not ADMIN_XLSX.exists() or not BOCHICA_XLSX.exists():
        print(
            f"Faltan archivos fuente. Corré antes:\n"
            f"  python -m scraper.inventario  (-> {ADMIN_XLSX})\n"
            f"  python -m scraper.bochica     (-> {BOCHICA_XLSX})"
        )
        sys.exit(1)

    admin = cargar_admin(ADMIN_XLSX)
    bochica = cargar_bochica(BOCHICA_XLSX)
    cruzado = cruzar_inventarios(admin, bochica)

    resumen = resumen_cruce(cruzado)
    print(f"Admin: {len(admin)} filas | Bochica: {len(bochica)} filas (tras exclusiones)")
    print(f"Cruce: {resumen}")
