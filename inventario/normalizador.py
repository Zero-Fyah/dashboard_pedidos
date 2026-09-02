"""
normalizador.py — Carga y cruce de los Excel de inventario (DEC-039).

Normaliza los dos documentos fuente (admin_inventario.xlsx,
bochica_inventario.xlsx) a un esquema común y los cruza por el
identificador de producto compartido (`ID de especificación` en el
admin, `ID Producto` en Bochica — confirmado 71,5% de cruce, ver
docs/decisions.md DEC-039 pregunta 1).

La clasificación picking/altura vive en `inventario/layout.py` (DEC-041);
acá está el recorte de alcance del lado del catálogo administrativo.
"""

import logging
from pathlib import Path

import pandas as pd

from comun import VIGENCIA_ACTIVO, VIGENCIA_DESCONTINUADO

logger = logging.getLogger("inventario.normalizador")

# Placeholders de Bochica que no representan inventario real (DEC-039
# pregunta 2, decisión 2026-07-23) — contenedores reutilizables y
# artefactos de migración/zonas de tránsito, no posiciones físicas.
BOCHICA_ID_PLACEHOLDER = "0"
BOCHICA_UBICACIONES_PLACEHOLDER = frozenset({"_MIGRADO", "subbodega"})

# Alcance de la comparación del lado admin (DEC-041).
#
# `Arena` es la categoría de mercancía recibida por peso: medido sobre los
# archivos reales, está 100% fuera del layout (893.070 unidades en Bochica
# contra 50 dentro), mientras el resto de categorías está ≥98,8% dentro.
# Es el discriminador correcto — NO el prefijo de familia: `PRA*` (Arena)
# está 100% fuera del layout y `PR0`-`PR7` (Areneras) 100% dentro, así que
# filtrar por la familia `PR` se llevaría ambas.
CATEGORIA_RECIBIDA_POR_PESO = "Arena"

# CEDI Mosquera = almacén Bogotá del sistema administrativo (confirmado
# por el Arquitecto, DEC-041). Excluida `Arena`, Bogotá concentra el
# 99,76% de las unidades: el filtro es correcto y de bajo impacto.
ALMACEN_BODEGA = "Bogotá"

# Definición operativa de avería (DEC-041, confirmada por el Arquitecto).
# Medido sobre el catálogo real: hoy el patrón de referencia está
# íntegramente contenido en la categoría, así que la unión equivale a
# filtrar solo por `Outlet %`. Se mantiene como unión a propósito —
# documenta la intención y aguanta que entre una avería fuera de esa
# categoría.
CATEGORIA_AVERIA = "Outlet %"
_PATRON_AVERIA = r"\sAVER[ÍI]A$"

# Los valores crudos de `producto_activo` en el export del admin (DEC-065).
# Vocabulario del sistema origen, así que vive acá; las etiquetas que se
# publican hacia el dashboard son `comun.VIGENCIA_*`, porque son contrato
# entre etapas. Qué significa cada uno —y cómo se estableció— está en
# `vigencia_por_referencia()`.
#
# El origen cambió de vocabulario entre el 2026-08-19 y el 2026-08-20, el
# mismo cambio que retiró el punto final de dos encabezados (DEC-122): pasó
# de las etiquetas mal traducidas `Fue`/`No hay` a las literales `Sí`/`No`.
# Medido igual que en DEC-065 para no asumir por la etiqueta: `Sí` trae
# 55,3% de filas con inventario > 0 contra 9,4% de `No` — mismo patrón que
# `Fue` (69%) y `No hay` (6%), así que es el mismo corte con nombre nuevo.
# Se aceptan las dos parejas para no romper si algún archivo viejo se
# reprocesa.
ADMIN_ACTIVO = "Fue"
ADMIN_DESCONTINUADO = "No hay"
ADMIN_ACTIVO_SI = "Sí"
ADMIN_DESCONTINUADO_NO = "No"
_VALORES_ACTIVO = frozenset({ADMIN_ACTIVO, ADMIN_ACTIVO_SI})
_VALORES_DESCONTINUADO = frozenset({ADMIN_DESCONTINUADO, ADMIN_DESCONTINUADO_NO})


def cargar_admin(path: Path) -> pd.DataFrame:
    """Carga y normaliza el Excel de inventario del sistema administrativo.

    Args:
        path: Ruta al .xlsx descargado por scraper/inventario.py.

    Returns:
        DataFrame con columnas snake_case; `id_especificacion` (str) es
        la llave de cruce contra `cargar_bochica()`.
    """
    # dtype=str en esta columna puntual: son enteros de 19 dígitos guardados
    # como texto en el Excel de origen. Si alguna fila llega sin valor (pasó
    # el 2026-08-10 con 16 filas de PS12), pandas no puede mezclar int64 con
    # NaN y sube la columna ENTERA a float64 — un float64 no representa un
    # entero de 19 dígitos sin perder precisión, así que el resto de los IDs
    # (buenos) se corrompen a notación científica ('2.08e+18') y el cruce con
    # Bochica por id_especificacion pasa a matchear cero filas, en silencio.
    # Forzar el dtype en la lectura evita que la inferencia numérica de
    # pandas alcance a tocar la columna.
    df = pd.read_excel(path, dtype={"ID de especificación": str})
    # El origen ha traído algunos encabezados con un punto final
    # (p. ej. "Referencia del producto.") y en algún momento entre el
    # 2026-08-19 y el 2026-08-20 dejó de hacerlo en dos de ellos, sin aviso
    # (DEC-122). Quitarlo antes de mapear vuelve el cruce tolerante a las dos
    # variantes sin tener que declararlas a mano una por una.
    df = df.rename(columns=lambda c: c.rstrip(".") if isinstance(c, str) else c)
    df = df.rename(
        columns={
            "Identificación del producto": "id_producto_admin",
            "ID de especificación": "id_especificacion",
            "Nombre comercial": "nombre_comercial",
            "Inventario": "inventario",
            "Referencia del producto": "referencia",
            "ALMACEN": "almacen",
            "Categoria del producto": "categoria",
            "Especificación": "especificacion",
            "Código de barras": "codigo_barras",
            "Peso": "peso",
            "Precio": "precio",
            "Existencias restantes": "existencias_restantes",
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
        "peso",
        "inventario",
        "existencias_restantes",
        "precio",
        "producto_activo",
        "descuento",
        "iva",
    ]
    # El mismo cambio de origen retiró "Existencias restantes" por completo
    # en algunas corridas (no solo el punto). Es dato de exhibición —hoy solo
    # lo consume `inventario/arena.py`, sin entrar en ningún cálculo— así que
    # se rellena NULL y se avisa, en vez de tumbar toda la carga por una
    # columna que nadie usa para decidir nada (DEC-122).
    if "existencias_restantes" not in df.columns:
        logger.warning(
            "cargar_admin: el origen (%s) no trae 'Existencias restantes' — columna queda NULL",
            path,
        )
        df["existencias_restantes"] = pd.NA
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


def marcar_averias(df_admin: pd.DataFrame) -> pd.Series:
    """Marca qué filas del catálogo admin son averías (DEC-041).

    Definición confirmada por el Arquitecto: categoría `Outlet %` **o**
    referencia terminada en " AVERIA"/" AVERÍA". Las averías se crean
    dentro de la familia del producto de origen ("PJ91" → "PJ91 AVERIA"),
    así que `comun.familia_de()` las clasifica bien sin caso especial.

    `Outlet %` no es exclusivamente averías (incluye ítems de fundación);
    se acepta ese margen — la estandarización de estos datos es trabajo
    posterior al dashboard, por decisión del Arquitecto.

    Args:
        df_admin: Resultado de `cargar_admin()`.

    Returns:
        Serie booleana alineada al índice de `df_admin`.
    """
    por_categoria = df_admin["categoria"] == CATEGORIA_AVERIA
    por_referencia = (
        df_admin["referencia"].astype(str).str.contains(_PATRON_AVERIA, regex=True, na=False)
    )
    solo_referencia = int((por_referencia & ~por_categoria).sum())
    if solo_referencia:
        logger.info(
            "marcar_averias: %d filas con referencia de avería fuera de la "
            "categoría '%s' — la unión ya no es redundante (DEC-041)",
            solo_referencia,
            CATEGORIA_AVERIA,
        )
    return por_categoria | por_referencia


def clasificar_vigencia(valores: pd.Series) -> pd.Series:
    """Mapea `producto_activo` (texto crudo del admin) a Activo/Descontinuado.

    Único punto de traducción de ese vocabulario (DEC-122): un valor nuevo
    cae del lado "descontinuado" por diseño — nunca se asume vigente en
    silencio — y queda registrado por log. Reusado por
    `vigencia_por_referencia()` y por `inventario.ubicaciones.
    sin_ubicacion_conocida()`, que antes de DEC-122 (corrección de
    2026-08-22) tenían cada uno su propio mapeo y solo uno se corrigió
    cuando el origen cambió de "Fue"/"No hay" a "Sí"/"No".

    Args:
        valores: Serie con los valores crudos de `producto_activo`.

    Returns:
        Serie del mismo largo/índice con `Activo` o `Descontinuado`.
    """
    valores = valores.astype(str).str.strip()
    activo = valores.isin(_VALORES_ACTIVO)

    desconocidos = set(valores) - _VALORES_ACTIVO - _VALORES_DESCONTINUADO
    if desconocidos:
        logger.warning(
            "clasificar_vigencia: valores de producto_activo fuera de los "
            "conocidos %s — se tratan como descontinuados: %s",
            sorted(_VALORES_ACTIVO | _VALORES_DESCONTINUADO),
            sorted(desconocidos),
        )
    return activo.map({True: VIGENCIA_ACTIVO, False: VIGENCIA_DESCONTINUADO})


def vigencia_por_referencia(df_admin: pd.DataFrame) -> pd.Series:
    """Clasifica cada referencia en vigente o descontinuada (DEC-065).

    El export del admin trae la vigencia en `producto_activo` con dos
    valores, `Fue` y `No hay`, que son artefactos de traducción automática
    del sistema origen (el mismo export trae una columna "Código China").
    **Cuál es cuál se estableció midiendo, no leyendo la etiqueta:**

    | Valor | Filas con inventario | Refs con demanda 90d |
    |---|---|---|
    | `Fue` | 69% | 94% |
    | `No hay` | 6% | 15% |

    La separación no deja lugar a duda, pero la etiqueta sola no la
    habría dado: "Fue" en español sugiere pasado, justo lo contrario.

    Una referencia agrupa varias especificaciones y **basta una vigente
    para que la referencia lo sea**. Es el criterio conservador: mantiene
    la referencia dentro del universo de salud en vez de esconderla.
    Medido hoy: ninguna referencia del alcance tiene marcas mezcladas, así
    que la regla no cambia ningún caso actual — cubre el que aparezca.

    Args:
        df_admin: Resultado de `cargar_admin()`, filtrado o no.

    Returns:
        Serie indexada por `referencia` con `Activo` o `Descontinuado`.
    """
    df = df_admin[["referencia", "producto_activo"]].copy()
    df["referencia"] = df["referencia"].astype(str).str.strip()
    activo = clasificar_vigencia(df["producto_activo"]) == VIGENCIA_ACTIVO

    por_referencia = activo.groupby(df["referencia"]).any()
    return por_referencia.map({True: VIGENCIA_ACTIVO, False: VIGENCIA_DESCONTINUADO}).rename(
        "vigencia"
    )


def filtrar_alcance_admin(df_admin: pd.DataFrame) -> pd.DataFrame:
    """Recorta el catálogo admin al universo comparable con el layout (DEC-041).

    Dos filtros: excluye la categoría recibida por peso (`Arena`, que vive
    100% fuera del layout) y se queda con el almacén de la bodega
    (`Bogotá` = CEDI Mosquera). Ambos descartes se loguean — nunca en
    silencio, mismo criterio que `cargar_bochica()`.

    Args:
        df_admin: Resultado de `cargar_admin()`.

    Returns:
        Subconjunto de `df_admin` comparable contra las ubicaciones del
        layout.
    """
    total = len(df_admin)
    es_peso = df_admin["categoria"] == CATEGORIA_RECIBIDA_POR_PESO
    es_bodega = df_admin["almacen"] == ALMACEN_BODEGA
    filtrado = df_admin[~es_peso & es_bodega]
    logger.info(
        "filtrar_alcance_admin: %d/%d filas conservadas (excluidas %d de categoría "
        "'%s' y %d de otros almacenes)",
        len(filtrado),
        total,
        int(es_peso.sum()),
        CATEGORIA_RECIBIDA_POR_PESO,
        int((~es_peso & ~es_bodega).sum()),
    )
    return filtrado


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
