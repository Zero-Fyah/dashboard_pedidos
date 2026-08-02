"""
motivos.py — Clasificación de los motivos de cancelación (DEC-085).

`registro_operaciones.referencia` guarda, en las filas de `Cancelar pedido`,
el motivo que escribió a mano quien canceló. Son **2.718 cadenas distintas
sobre 5.784 eventos**, y normalizarlas (tildes, mayúsculas, emojis,
puntuación) solo baja a 2.569: agrupar por texto exacto no sirve, porque son
frases y no un catálogo.

Lo que sí funciona es clasificar por palabras, porque el vocabulario está
concentrado en unas pocas: `pago` (2.781 apariciones), `reintegra` (2.419),
`excede` (1.830), `límite` (1.535).

**Vive en `comun/` y no en `inventario/`** por la misma razón que
`comun/reposicion.py` (DEC-071): el dashboard lo consume directamente y no
puede importar `inventario/` (DEC-022).

**No se fuerza la cobertura.** Queda un 19,2% en `Otro`, del que un tercio es
de enero —el registro de operaciones arranca el 2026-01-23 y antes la columna
traía un ID numérico, no un motivo— y el resto es cola genuina ("se retracta",
"modalidad errónea"). Estirar las reglas para bajar ese número las volvería
frágiles sin mover ninguna conclusión: la causa dominante ya queda aislada.
"""

import re
import unicodedata

CAUSA_FALTA_DE_PAGO = "Falta de pago"
CAUSA_SIN_MOTIVO = "Sin motivo registrado"
CAUSA_OTRO = "Otro / sin clasificar"

# Fecha desde la que el origen escribe motivos de verdad. Antes de esto la
# columna traía el ID numérico del pedido, así que un "sin clasificar" previo
# no dice nada sobre las reglas (DEC-081 fechó el cambio igual, por vocabulario).
DESDE_MOTIVO_CONFIABLE = "2026-01-23"

# Mediana de días entre el pedido y su cancelación por falta de pago. No es un
# parámetro elegido: es lo que hace la empresa, medido —el 94% de los casos
# cae entre el día 3 y el 7, y la mediana es 4-6 en todos los meses.
DIAS_LIMITE_PAGO = 5

# El orden es la prioridad: la primera regla que casa gana. `Falta de pago`
# va primero a propósito — un motivo como "excede días de pago, comercial
# solicita reintegro" es una cancelación por pago, no una petición comercial.
_REGLAS: tuple[tuple[str, str], ...] = (
    (CAUSA_FALTA_DE_PAGO, r"\b(pago|paga|pagar|abono|consign\w*|mora|cartera)\b"),
    (CAUSA_FALTA_DE_PAGO, r"\bexcede\w*\b|\bexceder\b|\blimite de tiempo\b|dias transcurridos"),
    (
        "Error de captura",
        r"\berror\b|incorrect|\berrad|duplicad|\bprueba\b|\btest\b|subio mal|mal subido|"
        r"selecciono mal|equivocad",
    ),
    ("Devolución", r"devoluci|\bdevuelve\b"),
    (
        "Cambio de pedido",
        r"subir[ae]? (un )?(otro|nuevo)|subira otro|nuevo pedido|sera modificado|modificar|"
        r"adicion de productos|cambio de pedido",
    ),
    ("Petición del cliente", r"peticion (de |del )?(la )?client|cliente (solicita|pide|indica)"),
    (
        "Petición comercial",
        r"peticion (de |del )?(la )?comercial|comercial (indica|solicita|informa)",
    ),
    ("Regla comercial", r"monto minimo|no cumple"),
    (
        "Faltante de producto",
        r"faltan? producto|sin (stock|inventario|existencia)|no hay (stock|inventario)",
    ),
    (
        "Cambio logístico",
        r"cambio de bodega|metodo de entrega|cambio de sede|cambio de direccion",
    ),
)

_COMPILADAS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (causa, re.compile(patron)) for causa, patron in _REGLAS
)

_NO_ALFANUM = re.compile(r"[^a-z0-9 ]+")
_ESPACIOS = re.compile(r"\s+")


def normalizar_motivo(texto: object) -> str:
    """Deja el motivo comparable: sin tildes, minúsculas, sin emojis ni signos.

    Los motivos vienen con emojis (`🔴⚡`, `🍀🐼❄️`), iniciales de quien canceló
    pegadas al final (`... SE REINTEGRA / Edd`) y mayúsculas inconsistentes.
    Nada de eso cambia el significado, así que se descarta antes de clasificar.

    Args:
        texto: El valor crudo de `referencia`. Tolera `None` y no-cadenas.

    Returns:
        El texto normalizado, o cadena vacía si no había nada.
    """
    plano = unicodedata.normalize("NFKD", str(texto or ""))
    sin_tildes = "".join(c for c in plano if not unicodedata.combining(c)).lower()
    return _ESPACIOS.sub(" ", _NO_ALFANUM.sub(" ", sin_tildes)).strip()


def clasificar_motivo(texto: object) -> str:
    """Asigna una causa al motivo de cancelación.

    Args:
        texto: El valor crudo de `referencia`; se normaliza internamente.

    Returns:
        El nombre de la causa. `Sin motivo registrado` si venía vacío y
        `Otro / sin clasificar` si ninguna regla casó — ninguno de los dos
        es un error: son estados legítimos que la página muestra como tales.
    """
    normalizado = normalizar_motivo(texto)
    if not normalizado:
        return CAUSA_SIN_MOTIVO
    # Un motivo que es solo dígitos es el ID numérico que el origen ponía
    # antes del 2026-01-23, no un motivo. Clasificarlo como "Otro" lo haría
    # parecer un texto que las reglas no entendieron.
    if normalizado.isdigit():
        return CAUSA_SIN_MOTIVO
    for causa, patron in _COMPILADAS:
        if patron.search(normalizado):
            return causa
    return CAUSA_OTRO
