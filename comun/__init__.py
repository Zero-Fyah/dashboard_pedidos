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

import sqlite3
from pathlib import Path
from typing import Protocol

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

# Subconjunto de ESTADOS_ACTIVOS_INVENTARIO que el consolidado de Pedidos usa
# para "Solo previos a picking" (DEC-109, 2026-08-12, decisión del
# Arquitecto). Antes ese filtro usaba las 12 de ESTADOS_ACTIVOS_INVENTARIO
# completas; se angosta a las 3 en las que el pedido está comprometido pero
# la mercancía todavía no se movió de picking — ni "pendiente de entrega" ni
# "en inspección" (ya pasaron por alistamiento) ni "pendiente de
# confirmación" (todavía no comprometido) califican. Tupla, no frozenset,
# mismo motivo que ESTADOS_ACTIVOS_INVENTARIO: SQL determinístico.
ESTADOS_PREVIOS_PICKING: tuple[str, ...] = (
    "pendiente de pago (pago inmediato)",
    "pendiente de recolección",
    "aprobación de pagos",
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


# Líneas de relleno para completar el monto mínimo de pedido ($300.000,
# DEC-115) — no representan mercancía real. `nombre_producto` tal como lo
# persiste `lineas_pedido`/`detalle_diferencias`; sus referencias son
# `AV ACC`/`AV AR`/`AV AL`. Comportamiento estructural del origen: siempre
# `cantidad_comprada=1, cantidad_entregada=0` (verificado por DOM en vivo,
# pedido 2014370644) — nunca se marcan como entregadas porque no hay
# producto real detrás. Cualquier comparación de cantidades que no las
# excluya produce falsos positivos de faltante.
PRODUCTOS_RELLENO: tuple[str, ...] = ("Interno Acc", "Interno Are", "Interno Ali")


# Modalidades de venta de la categoría Arena (DEC-118), verificadas fila por
# fila contra `admin_inventario.xlsx` — no inferidas del nombre de la
# referencia por patrón, salvo Corporativo (ver abajo). `referencia` es la
# columna `Referencia del producto.` del admin (`referencia` en
# `lineas_pedido`/`inventario/normalizador.cargar_admin`).
MODALIDAD_UNIDADES = "Unidades"
MODALIDAD_TONELADA = "Tonelada"
MODALIDAD_CORPORATIVO = "Corporativo"
MODALIDAD_RESPALDO = "Respaldo"
MODALIDAD_YUMBO_HUB = "Yumbo (hub)"

# Activas para venta directa (`producto_activo == 'Fue'`, confirmado con el
# Arquitecto — el campo del origen es contraintuitivo: 'Fue' = activo,
# 'No hay' = inactivo para venta directa). Sus `Nombre comercial` dicen
# literalmente "...unidades".
ARENA_REFERENCIAS_UNIDADES: tuple[str, ...] = (
    "PRA13",
    "PRA36",
    "PRA63",
    "PRA75",
    "PRA76",
    "PRA77",
    "PRA78",
)
# Pool nacional de tonelada, distribuido por ciudad vía su propia columna
# `almacen` — no reparte a través de YUMBO TONELADA.
ARENA_REFERENCIA_TONELADA_NACIONAL = "PRA ARENA TONELADA"
# `producto_activo == 'No hay'` a propósito: no se venden por catálogo
# público, se asignan bajo solicitud del comercial.
ARENA_REFERENCIAS_RESPALDO: tuple[str, ...] = (
    "ARENA AVERIA BOGOTA",
    "ARENA AVERIA MEDELLIN",
)
# Buckets operativos del hub de Yumbo, no SKU de venta directa. YUMBO EN
# TRANSITO (nombre comercial real: "Yumbo traslados nacionales") está en 0
# en toda la historia disponible — el concepto existe en el origen pero sin
# dato usable todavía.
ARENA_REFERENCIAS_YUMBO_HUB: tuple[str, ...] = ("YUMBO TONELADA", "YUMBO EN TRANSITO")
# Las 12 ciudades donde el admin registra inventario de Arena (columna
# `almacen`), verificadas contra `admin_inventario.xlsx` — DEC-118.
ARENA_CIUDADES: tuple[str, ...] = (
    "Bogotá",
    "Medellin",
    "Cali",
    "Pereira",
    "Bucaramanga",
    "Cucuta",
    "Barranquilla",
    "Yumbo",
    "Popayan",
    "Pasto",
    "Ibague",
    "Villavicencio",
)

# Confirmadas por su propio `Nombre comercial` ("PRA-T01 test", "test猫砂") o,
# en el caso de PB008/PS0199, excluidas por decisión del Arquitecto sin
# investigar todavía qué son (no siguen el patrón PRA/ARENA de ninguna otra
# fila de la categoría).
ARENA_REFERENCIAS_EXCLUIDAS: tuple[str, ...] = ("PRA-T01", "PRA-001", "PB008", "PS0199")


def clasificar_modalidad_arena(referencia: str | None) -> str | None:
    """Modalidad de venta de una referencia de la categoría Arena (DEC-118).

    Corporativo es la única regla por patrón (`"CORPORATIVO" in referencia`)
    porque hoy son 7 ciudades y una ciudad nueva no debe requerir tocar
    código — las demás modalidades son listas cerradas, verificadas contra
    el dato real.

    Returns:
        La modalidad, o `None` si la referencia está en
        `ARENA_REFERENCIAS_EXCLUIDAS` o no se reconoce (este segundo caso
        debe registrarse como hallazgo — una referencia de Arena nueva que
        caiga acá es una modalidad real sin clasificar, no ruido).
    """
    if not referencia:
        return None
    ref = str(referencia).strip()
    if ref in ARENA_REFERENCIAS_EXCLUIDAS:
        return None
    if ref in ARENA_REFERENCIAS_UNIDADES:
        return MODALIDAD_UNIDADES
    if ref == ARENA_REFERENCIA_TONELADA_NACIONAL:
        return MODALIDAD_TONELADA
    if ref in ARENA_REFERENCIAS_RESPALDO:
        return MODALIDAD_RESPALDO
    if ref in ARENA_REFERENCIAS_YUMBO_HUB:
        return MODALIDAD_YUMBO_HUB
    if "CORPORATIVO" in ref.upper():
        return MODALIDAD_CORPORATIVO
    # Esquema anterior a la migración a `PRA ARENA TONELADA`/`PRA13`
    # nacionales (~marzo 2026, medido por vigencia en `movimientos_inventario`).
    # No aparece en `admin_inventario.xlsx` de hoy — solo en `lineas_pedido`
    # de pedidos viejos ("BOGOTA TONELADA", "CALI UNIDADES", etc.). Sin esta
    # regla, medir el balance histórico Unidades/Tonelada perdía 22,4% de las
    # ventas de Arena (verificado contra 113.807 líneas reales, DEC-118).
    ref_alta = ref.upper()
    if ref_alta.endswith(" TONELADA"):
        return MODALIDAD_TONELADA
    if ref_alta.endswith(" UNIDADES"):
        return MODALIDAD_UNIDADES
    return None


# Vigencia comercial de una referencia (DEC-065). Es un contrato entre
# etapas: `inventario/salud.py` escribe estas etiquetas en
# `inventario_salud.vigencia` y el dashboard las lee de la VIEW, así que
# viven acá y no en ninguna de las dos. Los valores crudos del export del
# admin que las originan (`Fue`/`No hay`) son vocabulario del sistema
# origen y se quedan en `inventario/normalizador.py`.
VIGENCIA_ACTIVO = "Activo"
VIGENCIA_DESCONTINUADO = "Descontinuado"


# Umbrales de cobertura, en días — **política de inventario**, no defaults
# de implementación (DEC-066, definidos por el Arquitecto el 2026-07-30).
# Viven acá porque `inventario/salud.py` clasifica con ellos y el pie de la
# página de Salud los cita al usuario: con el número en un solo lugar, la
# prosa se interpola en vez de repetirlo y no puede quedar contradiciendo
# a la clasificación.
#
# 390 es el percentil 80 de la cobertura del catálogo vigente (medido: p50
# 132, p75 304, p80 392, p90 832). El 90 anterior venía de DEC-049, antes
# de medir la distribución, y dejaba al 57% del catálogo vigente en la
# categoría — una alarma que contiene a la mayoría no informa.
#
# En el negocio, 390 días es más de un año de inventario al ritmo de venta
# de los últimos 90 días: laxo a propósito, porque la operación importa por
# mar y un ciclo de reposición largo justifica coberturas que en otra
# operación serían absurdas.
#
# Consecuencia conocida: la banda "Normal" queda ancha (15 a 390 días) y no
# distingue 30 de 300. Partirla exige una segunda definición de negocio que
# no está tomada — ver DEC-066.
COBERTURA_RIESGO_D = 15
COBERTURA_ALTA_D = 390

# Subpedidos que efectivamente se despacharon (DEC-069). Es
# `ESTADOS_CERRADOS` menos `cancelado`: un subpedido cancelado no salió
# corto, no salió. Medido, incluirlo sumaba 101 líneas al faltante que no
# son incumplimiento de despacho sino cancelaciones.
#
# `comentado` se incluye porque el proyecto lo trata como cerrado desde
# siempre, pero su definición sigue **pendiente del Arquitecto** y pesa:
# aporta 1.196 de las 4.782 líneas con faltante. Si resulta no ser un
# despacho, el fill rate por línea sube.
ESTADOS_DESPACHADOS: frozenset[str] = ESTADOS_CERRADOS - {"cancelado"}


# Vocabulario de clases de la línea SKU-posición y del conteo (DEC-067).
# Vive acá porque `inventario/` lo produce y el dashboard decide con él qué
# entra a la cola de conteo: antes había dos definiciones distintas de "A/B"
# —una por línea, otra por posición— que daban 1.516 contra 1.817 posiciones
# para el mismo concepto.
#
# Un ID que el puente de atribución no alcanza hereda la clase de su
# referencia y se marca con este sufijo, para no fingir una precisión que no
# hay (ver `inventario/ubicaciones.py`).
SUFIJO_CLASE_HEREDADA = " (por referencia)"

# Clases que obligan a visitar la posición en la Fase 3 del plan. Se compara
# contra un conjunto explícito y no con `startswith(("A","B"))`: hoy dan lo
# mismo, pero una clase futura llamada "Ampliado" o "Bloqueado" entraría sola
# a la cola de conteo sin que nadie lo note.
CLASES_CONTEO_PRIORITARIO: frozenset[str] = frozenset(
    {"A", "B", "A" + SUFIJO_CLASE_HEREDADA, "B" + SUFIJO_CLASE_HEREDADA}
)


def es_conteo_prioritario(clase: object) -> bool:
    """True si la clase obliga a contar la posición (DEC-067).

    Se aplica sobre `clase_posicion`, no sobre `clase`: la regla del plan es
    que **una sola línea A eleva toda la posición**, porque la visita física
    es a la posición y no a la línea. Contar una posición a medias no solo
    desperdicia la visita — invalida su IRA, porque no permite afirmar que la
    posición esté correcta.

    La comparación es sobre `str(clase)` y eso ya cubre los ausentes sin
    necesitar pandas acá: `None`, `nan` y `pd.NA` se convierten a `"None"`,
    `"nan"` y `"<NA>"`, que no están en el conjunto. Se probó antes con
    `clase != clase` para detectar NaN y **reventaba con `pd.NA`**, cuyo `!=`
    no devuelve un booleano.

    Args:
        clase: Valor de `clase_posicion`; tolera None, NaN y pd.NA.

    Returns:
        True si la posición entra al alcance prioritario de conteo.
    """
    return str(clase) in CLASES_CONTEO_PRIORITARIO


# DEC-065: acá vivía ACCIONES_RENDIMIENTO, la lista de 4 acciones de staff
# que HAL-008 extrajo a comun/ en la Fase 6. Su único consumidor era la
# VIEW v_rendimiento_operadores, retirada en DEC-065 por publicar una
# comparación entre personas que DEC-054 midió como inválida. Sin la view
# la constante quedaba huérfana, así que se fue con ella.
#
# Las 4 acciones verificadas contra registro_operaciones el 2026-07-17
# quedan documentadas en decisions.md (HAL-008 y DEC-065); si alguna vez
# vuelve a hacer falta la lista, son cinco líneas.


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
# Migraciones SQLite idempotentes (DEC-108)
# ─────────────────────────────────────────────
# El patrón "ALTER TABLE ... try/except OperationalError buscando la frase
# del error esperado" vivía copiado 3 veces (scraper/db.py y dos sitios de
# etl/etl_principal.py) antes de esta factorización — hallazgo de la
# auditoría de Skills del 2026-08-12
# (docs/reports/auditoria_skills_2026-08-12.md), prerrequisito del Skill
# sqlite-wal-concurrente: documentar un patrón triplicado lo perpetuaría.
#
# Protocol en vez de importar aiosqlite: AUD-M5 exige que comun/ no cargue
# dependencias pesadas ni de terceros — esto solo describe los dos métodos
# async que la función necesita, sin acoplarse a la librería concreta.


class _ConexionAsincrona(Protocol):
    async def execute(self, sql: str) -> object: ...
    async def commit(self) -> None: ...


# Frases exactas que sqlite3.OperationalError incluye cuando el DDL ya se
# aplicó antes. La comparación es case-insensitive, pero la frase se guarda
# una sola vez acá para que no haya una segunda copia que se desalinee.
DDL_ERROR_COLUMNA_DUPLICADA = "duplicate column name"
DDL_ERROR_COLUMNA_INEXISTENTE = "no such column"


async def ejecutar_ddl_idempotente(db: _ConexionAsincrona, ddl: str, error_esperado: str) -> None:
    """Ejecuta un DDL (ALTER TABLE...) tolerando que ya se haya aplicado.

    Sin esto, re-ejecutar una migración sobre una base ya migrada revienta
    con OperationalError. Solo se traga el error si su mensaje contiene
    `error_esperado` (típicamente DDL_ERROR_COLUMNA_DUPLICADA para ADD
    COLUMN, o DDL_ERROR_COLUMNA_INEXISTENTE para DROP COLUMN) — cualquier
    otro OperationalError (DB corrupta, disco lleno, tabla ausente) se
    propaga sin enmascarar (mismo criterio que AUD-B8).

    Args:
        db: Conexión aiosqlite abierta (o cualquier objeto con los mismos
            dos métodos async — no se importa aiosqlite acá, ver AUD-M5).
        ddl: Sentencia DDL completa a ejecutar, ej.
            "ALTER TABLE pedidos ADD COLUMN hora TEXT DEFAULT NULL".
        error_esperado: Fragmento del mensaje de error que indica que la
            migración ya se aplicó antes (comparación sin distinguir
            mayúsculas/minúsculas).

    Raises:
        sqlite3.OperationalError: Si el error no coincide con
            `error_esperado` — es un fallo real, no idempotencia.
    """
    try:
        await db.execute(ddl)
        await db.commit()
    except sqlite3.OperationalError as exc:
        if error_esperado.lower() not in str(exc).lower():
            raise


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


# ─────────────────────────────────────────────
# CIERRE DEL CICLO DE CONTEO (DEC-070)
# ─────────────────────────────────────────────
# El ciclo de DEC-058/060 termina en la detección: cuenta, compara, calcula
# IRA. Falta el tramo que exige cualquier norma de inventario cíclico —
# recuento, ajuste, verificación—, y sin él nadie puede responder "¿esta
# diferencia se corrigió?".
#
# El dashboard **no escribe en el sistema administrativo** (integral.md), así
# que el ajuste ocurre fuera y el pipeline no puede confirmarlo:
# `inventario_ubicaciones` y `conteos` son snapshots (DELETE + INSERT en cada
# corrida), así que `cantidad_sistema` **siempre es la de hoy**, no la del día
# del conteo. Cuando una diferencia desaparece, el conteo vuelve a evaluarse
# como `Coincide` y no hay forma de distinguir "se ajustó" de "el conteo
# estaba bien y el stock se movió".
#
# Se intentó un estado `Ajuste aplicado` comparando lo contado contra el
# sistema actual y **no puede dispararse nunca**: ambas cifras salen del mismo
# snapshot. Se eliminó en vez de dejarlo de adorno. Distinguirlo exige
# conservar la cantidad del sistema del día del conteo — ver DEC-070.
#
# Mientras tanto el cierre sí queda registrado por otra vía: la alerta
# `discrepancia:...` desaparece cuando el caso se resuelve, y `alertas`
# conserva `primera_vez` y la marca como resuelta (DEC-047/059).
ESTADO_CONTEO_COINCIDE = "Coincide"
ESTADO_CONTEO_PENDIENTE = "Pendiente de recuento"
ESTADO_CONTEO_CONFIRMADO = "Recuento confirma"
ESTADO_CONTEO_DESCARTADO = "Recuento corrige"

ESTADOS_CICLO_CONTEO: tuple[str, ...] = (
    ESTADO_CONTEO_COINCIDE,
    ESTADO_CONTEO_DESCARTADO,
    ESTADO_CONTEO_CONFIRMADO,
    ESTADO_CONTEO_PENDIENTE,
)

# ─────────────────────────────────────────────
# VOCABULARIO DEL SISTEMA ORIGEN (DEC-081)
# ─────────────────────────────────────────────
# La vista del pedido en la SPA **cambia sola**: medido sobre 7 meses, entre
# 2026-01-01 y 2026-07-31 aparecieron 18 valores nuevos en cuatro
# vocabularios distintos, y ninguno lo detectó el pipeline — se encontraron
# de casualidad al revisar el DOM.
#
# Estas listas son el **acuse de recibo**: lo que está acá se conoce y no
# alerta; lo que el origen emita y no esté, sale como hallazgo de calidad.
# Reconocer un valor nuevo es agregarlo acá, igual que con
# `ESTADOS_CONOCIDOS`.
#
# Congeladas el 2026-07-31 con lo que había en la base.
TIMELINE_CONOCIDO: frozenset[str] = frozenset(
    {
        "Alistamiento",
        "Esperando a ser recogido",  # apareció 2026-03-26
        "Listo para enviar",
        "Pendiente de pago",
        "Recibido y recibido",
        "confirmando",
        "pdt despachar",
        "pdt recibir",
        "pdt verificacion",
        "pedido cancelado",
    }
)

ACCIONES_CONOCIDAS: frozenset[str] = frozenset(
    {
        "Actualizar hora de entrega",
        "Alistamiento con faltantes",
        "Alistamiento sin diferencia",
        "Auditoría de pago",
        "Cambiar vehículo",  # apareció 2026-07-24
        "Cancelar entrega",
        "Cancelar pedido",
        "Comentario",  # apareció 2026-04-16
        "Confirmar pedido",
        "Entrega",
        "Faltantes aprobados",
        "Faltantes no aprobados",
        "Inspección con diferencia",
        "Inspección sin diferencia",
        "Modificar cantidad de entrega manualmente",
        "Pedido enviado",
        # Clave de traducción cruda que la SPA filtró a la interfaz el
        # 2026-06-22. Se declara porque existe, no porque esté bien.
        "REGISTER_UNRATED",
        "Subir comprobante de pago",
        "Usuario realizó pedido",
    }
)

CONCEPTOS_MONTO_CONOCIDOS: frozenset[str] = frozenset(
    {
        "Descuento auto-recogida",  # apareció 2026-02-28
        "Descuento empleado",  # apareció 2026-05-28
        "Descuento miembro/promoción",
        "Descuento por tasa",  # apareció 2026-02-28
        "Flete",
        "IVA accesorios",
        "IVA alimentos",
        "IVA arena para gatos",
        "Monto pagado",
        "Total IVA",
        "Total a pagar / Total final a pagar",
        "Total antes de IVA",
        "Total descuento",
        "Total después del descuento",
        "Total precio original",
    }
)

# Veredicto de `Auditoría de pago`, en `registro_operaciones.referencia`
# (DEC-083). El origen empezó a registrarlo el 2026-04-10 y nadie se enteró:
# el detector de DEC-081 vigilaba `accion`, no `referencia`.
#
# Se vigila **solo la referencia de las auditorías**, no la de todas las
# acciones: en `Cancelar pedido` la misma columna guarda el motivo en texto
# libre —con emojis y variantes de mayúsculas— y ahí no hay vocabulario
# cerrado que declarar. Acá sí: son dos valores.
VEREDICTOS_AUDITORIA: frozenset[str] = frozenset(
    {
        "Aprobado",
        "Rechazado",
    }
)


def formato_miles(valor: float, decimales: int = 0) -> str:
    """Formatea un número a la convención colombiana: punto de miles, coma decimal.

    Existe para reemplazar el idiom `f"{n:,}".replace(",", ".")`, que es
    correcto solo cuando la cadena es **exclusivamente** el número. Escrito
    junto a prosa se vuelve un error silencioso:

        >>> f"En {1234:,} pedidos, con coma".replace(",", ".")
        'En 1.234 pedidos. con coma'

    La concatenación implícita de literales ocurre antes que la llamada al
    método, así que el `.replace()` se aplica a la frase entera y se come los
    signos de puntuación. Es un defecto que ya se coló una vez en la prosa de
    la página de inventario.

    Args:
        valor: El número a formatear.
        decimales: Cuántos decimales conservar. Por defecto ninguno.

    Returns:
        El número como texto, con `.` de miles y `,` decimal.
    """
    crudo = f"{valor:,.{decimales}f}"
    return crudo.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


# ─────────────────────────────────────────────
# REPOSICIÓN (DEC-071)
# ─────────────────────────────────────────────
# Factor de seguridad por clase ABC, para el stock de seguridad del punto de
# reorden. Es la tabla estándar de la normal: se protege más lo que más pesa.
#
#   A → 98% de nivel de servicio (Z = 2,05)
#   B → 95% (Z = 1,65)
#   C → 90% (Z = 1,28)
#
# Son **valores por defecto de la práctica**, no una política acordada por el
# negocio: nadie definió todavía qué nivel de servicio quiere sostener por
# clase. Cambiarlos es una decisión de Gerencia, no de código.
NIVEL_SERVICIO_Z: dict[str, float] = {"A": 2.05, "B": 1.65, "C": 1.28}
Z_SERVICIO_DEFECTO: float = 1.28

# Días que una discrepancia puede quedar sin recuento antes de alertar. No
# sale del plan: es un default explícito, a la espera de que el área fije su
# propio acuerdo de servicio para el recuento.
DIAS_PARA_RECUENTO: int = 7
