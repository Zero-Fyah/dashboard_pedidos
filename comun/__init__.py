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
)

# Dominio completo de estados conocidos — para checks defensivos (AUD-M8).
# Un estado presente en DB que no esté aquí indica que el sistema origen
# agregó o renombró estados: las VIEWs podrían estar excluyéndolo en
# silencio y hay que actualizar las listas de arriba.
ESTADOS_CONOCIDOS: frozenset[str] = (
    ESTADOS_CERRADOS | ESTADOS_FIJAN_CANTIDADES | frozenset(ESTADOS_ACTIVOS_INVENTARIO)
)

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
