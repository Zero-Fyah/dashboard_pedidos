"""
Módulo común — dashboard_pedidos
================================
Utilidades puras y constantes de dominio compartidas entre las tres etapas
(scraper, ETL, dashboard).

AUD-M5 (auditoría 2026-07-01): solo stdlib y sin efectos secundarios de
import — importar este módulo no crea directorios de log, no abre handlers,
no ejecuta load_dotenv() y no requiere Playwright ni ninguna dependencia
pesada. Cualquier etapa puede importarlo de forma segura.

AUD-M8 (auditoría 2026-07-01): este módulo es el único origen de verdad
para las listas de estados de subpedido. El scraper parametriza sus queries
desde estas constantes, el ETL genera los literales SQL de sus VIEWs desde
ellas y el dashboard deriva sus filtros de las mismas.
"""

from pathlib import Path

# ─────────────────────────────────────────────
# CONSTANTES DE DOMINIO — estados de subpedido
# ─────────────────────────────────────────────
# Todos los estados se almacenan aquí en minúsculas: las comparaciones
# se hacen siempre via LOWER() en SQL o .lower() en Python.

# Un pedido se considera cerrado cuando TODOS sus subpedidos están en
# alguno de estos estados. Los pedidos cerrados no se vuelven a procesar
# en modo incremental (regla de negocio #2 de docs/integral.md).
ESTADOS_CERRADOS: frozenset[str] = frozenset(
    {
        "completado",
        "cancelado",
        "comentado",
    }
)

# Referencia histórica: estados del sistema que fijan cantidades en el
# flujo operacional. Conservado como documentación del dominio.
# con_cantidades usa ESTADOS_CERRADOS desde la resolución de BUG-005
# opción B (docs/decisions.md).
ESTADOS_FIJAN_CANTIDADES: frozenset[str] = frozenset(
    {
        "pendiente de confirmación",
        "pendiente de envío (pago inmediato)",
        "pendiente de envío (crédito)",
        "pendiente de envío (contra entrega)",
        "pendiente de entrega",
        "enviado",
        "período contable",
        "completado",
        "cancelado",
        "comentado",
    }
)

# Estados considerados "activos" para el inventario comprometido
# (VIEW v_inventario_comprometido del ETL). Tupla — no frozenset — para
# que los literales SQL generados sean determinísticos entre corridas.
ESTADOS_ACTIVOS_INVENTARIO: tuple[str, ...] = (
    "pendiente de confirmación",
    "pendiente de pago (pago inmediato)",
    "pendiente de pago (crédito)",
    "pendiente de pago (contra entrega)",
    "pendiente de recolección",
    "aprobación de pagos",
    "pendiente de envío (pago inmediato)",
    "pendiente de envío (crédito)",
    "pendiente de envío (contra entrega)",
    "pendiente de entrega",
    "en inspección",
    # DEC-039 (2026-07-24): confirmado por el Arquitecto — estado
    # transitorio real, controlado por el área de inventarios. El
    # subpedido queda comprometido y físicamente ya tocado por
    # alistamiento mientras se resuelve: si la mercancía aparece, vuelve
    # a alistamiento (re-pick); si no aparece, se aprueba el faltante y
    # avanza a inspección. Antes sin clasificar (CLAUDE.md pendiente).
    "auditoría de faltantes",
)

# Dominio completo de estados conocidos — para checks defensivos (AUD-M8).
# Un estado presente en DB que no esté aquí indica que el sistema origen
# agregó o renombró estados: las VIEWs podrían estar excluyéndolo en
# silencio y hay que actualizar las listas de arriba.
ESTADOS_CONOCIDOS: frozenset[str] = (
    ESTADOS_CERRADOS | ESTADOS_FIJAN_CANTIDADES | frozenset(ESTADOS_ACTIVOS_INVENTARIO)
)

# Familias de producto (DEC-041, confirmado por el Arquitecto 2026-07-25).
# La familia son los DOS PRIMEROS CARACTERES de la referencia — incluidas
# las averías, que son referencias propias dentro de la familia de origen
# ("PJ91 AVERIA" → "PJ"). Tupla — orden determinístico para SQL y para el
# orden de los filtros del dashboard.
# `PW` todavía no tiene existencias (19 referencias, 0 unidades al
# 2026-07-25) pero está próxima a ingresar: se incluye desde ahora para
# que el día que llegue mercancía no requiera cambio de código.
FAMILIAS_PRODUCTO: tuple[str, ...] = (
    "PA",
    "PB",
    "PC",
    "PH",
    "PJ",
    "PO",
    "PP",
    "PR",
    "PS",
    "PW",
)


def familia_de(referencia: str | None) -> str | None:
    """Extrae la familia de producto de una referencia (DEC-041).

    Args:
        referencia: Referencia del catálogo, ej. "PJ91" o "PJ91 AVERIA".

    Returns:
        Los 2 primeros caracteres en mayúscula si corresponden a una
        familia conocida, o None si la referencia es vacía o su prefijo
        no está en `FAMILIAS_PRODUCTO` (referencias legadas o con
        espacios al inicio — ver pendientes de DEC-041).
    """
    if not referencia:
        return None
    prefijo = str(referencia).strip()[:2].upper()
    return prefijo if prefijo in FAMILIAS_PRODUCTO else None


# Acciones de staff que cuentan para el rendimiento de operadores
# (VIEW v_rendimiento_operadores del ETL — HAL-008, extraídas en Fase 6).
# Verificadas contra registro_operaciones en DB real el 2026-07-17: son
# exactamente las 4 acciones existentes. Tupla — no frozenset — para SQL
# determinístico. Si el sistema origen renombra una acción, el check
# etl_view_vacia solo detecta el caso "todas desaparecieron": mantener
# esta lista alineada con el origen.
ACCIONES_RENDIMIENTO: tuple[str, ...] = (
    "Alistamiento sin diferencia",
    "Alistamiento con faltantes",
    "Inspección sin diferencia",
    "Inspección con diferencia",
)


# ─────────────────────────────────────────────
# UTILIDADES PURAS
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# PLACEHOLDERS DEL SISTEMA ORIGEN (DEC-025)
# ─────────────────────────────────────────────
# La SPA usa texto en vez de dejar la celda vacía. La semántica depende
# de la columna: en `descuento` el guion significa "descuento de cero";
# en el resto significa "no aplica" (p. ej. precio_descuento='-' es "sin
# descuento aplicado", no "precio cero" — ver DEC-025).

PLACEHOLDER_GUION: frozenset[str] = frozenset({"-", "--"})

# Ausencia de dato sin ambigüedad: '' (celda vacía) y 'g' (la unidad de
# peso renderizada sin número; el cero real se escribe '0g').
PLACEHOLDER_SIN_DATO: frozenset[str] = frozenset({"", "g"})

# Únicas columnas donde el guion se interpreta como cero (regla de
# negocio 2026-07-18). En las demás, NULL.
COLUMNAS_GUION_ES_CERO: frozenset[str] = frozenset({"descuento"})


def es_placeholder(val: str | None) -> bool:
    """Indica si el texto es un placeholder conocido del sistema origen.

    Permite al ETL distinguir una ausencia esperada de un formato
    inesperado, y no emitir WARNING por la primera (DEC-025).

    Args:
        val: Texto crudo leído de la DB.

    Returns:
        True si es None o uno de los placeholders conocidos.
    """
    if val is None:
        return True
    texto = str(val).strip()
    return texto in PLACEHOLDER_GUION or texto in PLACEHOLDER_SIN_DATO


def normalizar_numerico(val: str | None, *, guion_es_cero: bool = False) -> float | None:
    """Convierte a float aplicando la semántica de placeholders (DEC-025).

    Se distingue de `to_num()` en que aplica **reglas de negocio**, no
    solo formato: `to_num` sigue siendo el parser puro que usa el scraper
    para cantidades.

    Args:
        val: Texto a convertir.
        guion_es_cero: True solo para las columnas de
            `COLUMNAS_GUION_ES_CERO`, donde '-' significa cero.

    Returns:
        Valor float, 0.0 si el guion representa cero en esa columna, o
        None si el dato está ausente o no es convertible.
    """
    if val is None:
        return None
    texto = str(val).strip()
    if texto in PLACEHOLDER_GUION:
        return 0.0 if guion_es_cero else None
    if texto in PLACEHOLDER_SIN_DATO:
        return None
    return to_num(texto)


def to_num(val: str) -> float | None:
    """Convierte un string numérico en formato español a float.

    Elimina puntos de separador de miles y reemplaza la coma decimal por
    punto. También elimina prefijos monetarios (COP) y sufijos de unidad
    (g, kg, ml, l) que el scraper captura pegados al número.
    Retorna None si el valor no es convertible (nunca lanza excepción).

    Args:
        val: String a convertir, ej. "1.234,56", "200", "402g", "1.5kg".

    Returns:
        Valor float, o None si la conversión falla.
    """
    try:
        cleaned = val.strip()
        # BUG-017: capturar signo negativo antes de strip del prefijo
        # para manejar "-COP 137.706" además de "COP 137.706"
        negative = cleaned.startswith("-")
        if negative:
            cleaned = cleaned[1:]
        if cleaned.startswith("COP "):
            cleaned = cleaned[4:]
        # Eliminar sufijos de unidad pegados al número (ej: "402g", "1.5kg")
        # Orden importa: kg/ml antes que g/l para no dejar letra suelta
        for unit in ("kg", "ml", "mg", "g", "l"):
            if cleaned.lower().endswith(unit):
                cleaned = cleaned[: -len(unit)].strip()
                break
        cleaned = cleaned.replace(".", "").replace(",", ".")
        if negative:
            cleaned = "-" + cleaned
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def get_db_path() -> str:
    """Retorna la ruta absoluta a data/pedidos.db.

    La ruta se calcula relativa a la ubicación de
    este módulo, no al directorio de trabajo actual.
    Crea data/ si no existe.

    Returns:
        Ruta absoluta como string a data/pedidos.db.
    """
    path = Path(__file__).parent.parent / "data" / "pedidos.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


# ─────────────────────────────────────────────
# Hoja de conteo físico — contrato entre etapas (DEC-058)
# ─────────────────────────────────────────────
#
# El dashboard **escribe** esta hoja (la emite para ir a campo) y el
# pipeline de inventario la **lee** de vuelta desde `data/conteos/`. Es un
# contrato de datos entre dos etapas, así que vive acá y no en ninguna de
# las dos: si las columnas se declararan por separado, un cambio en el
# generador rompería el lector en silencio.
#
# Sigue el Anexo A del plan de inventario del área.

COLUMNAS_HOJA_CONTEO: tuple[str, ...] = (
    "fecha",
    "ubicacion",
    "id_especificacion",
    "referencia",
    "cantidad_contada",
    "lote",
    "vencimiento",
    "hallazgo",
    "causa",
    "contado_por",
    "actividad_origen",
    "observacion",
)

# Sin estas cinco un conteo no es interpretable, así que el archivo se
# rechaza entero. El resto es opcional: que falte el lote no invalida la
# cantidad contada.
COLUMNAS_CONTEO_REQUERIDAS: tuple[str, ...] = (
    "fecha",
    "ubicacion",
    "id_especificacion",
    "cantidad_contada",
    "contado_por",
)

# Tolerancias de exactitud por clase, sección 4 del plan. La identidad del
# SKU tiene tolerancia cero en todos los casos, pero eso se evalúa por
# hallazgo, no por diferencia de cantidad.
TOLERANCIA_POR_CLASE: dict[str, float] = {"A": 0.01, "B": 0.02, "C": 0.05}
TOLERANCIA_CONTEO_DEFECTO: float = 0.05

# Metas de IRA por clase, sección 12 del plan.
META_IRA_POR_CLASE: dict[str, float] = {"A": 95.0, "B": 90.0, "C": 85.0}


# Vocabulario cerrado de causas de discrepancia. **Cerrado a propósito**:
# en texto libre, "error de despacho", "mal despachado" y "despacho" son
# tres causas distintas para el Pareto, y el análisis mensual deja de
# poder agrupar nada. Sale de la sección 10 del plan de inventario y del
# catálogo de causas de su componente A.1.
#
# Solo aplica cuando hubo diferencia: un conteo que coincide no tiene causa
# que investigar.
CAUSAS_DISCREPANCIA: tuple[str, ...] = (
    "Error de recepción",
    "Error de despacho",
    "Movimiento no registrado",
    "Error de digitación",
    "Producto dañado o avería",
    "Mal ubicado",
    "Sin identificar",
)

# Meta de antigüedad del último conteo para clase A, sección 12 del plan.
META_ANTIGUEDAD_CONTEO_DIAS: int = 45
