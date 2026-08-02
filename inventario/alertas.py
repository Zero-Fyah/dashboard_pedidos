"""
alertas.py — Centro de excepciones: qué requiere acción hoy (DEC-059).

Los módulos del dashboard responden preguntas ("¿alcanza el inventario?",
"¿qué tan exacto es?"), pero para saber si hay algo que atender había que
entrar a los seis y buscar. Este módulo invierte eso: recorre el estado
recién calculado y devuelve **solo lo que está fuera de lo normal**, con su
severidad.

**Alertas por entidad y alertas agregadas.** Un quiebre es de una referencia
concreta y se lista una por una. "1.013 posiciones clase A sin conteo" es
una sola alerta, no mil: mil filas idénticas no son mil problemas, son un
problema de cobertura. Confundirlos convierte el centro de alertas en ruido,
que es exactamente lo que se pretende evitar.

**La antigüedad sale de la persistencia, no del cálculo.** Cada alerta trae
una `clave` estable; `persistencia.py` conserva la fecha en que apareció por
primera vez. Así se puede decir "esto lleva 12 días abierto" sin que este
módulo tenga que recordar nada, y una alerta que deja de emitirse queda
marcada como resuelta por su propia ausencia — el mismo mecanismo de
auto-cierre de los hallazgos de calidad (DEC-047).
"""

import logging

import pandas as pd

from comun import (
    ESTADO_CONTEO_CONFIRMADO,
    ESTADO_CONTEO_PENDIENTE,
    VIGENCIA_ACTIVO,
    es_conteo_prioritario,
)

logger = logging.getLogger("inventario.alertas")

CRITICA = "Crítica"
ALTA = "Alta"
MEDIA = "Media"

# Orden de atención. Es el que usa la página para ordenar y el que decide
# qué sube al scorecard.
ORDEN_SEVERIDAD = (CRITICA, ALTA, MEDIA)

# Una referencia sin salida por más de este tiempo, pero con stock, es
# capital inmovilizado. Coincide con el umbral que ya usa `salud.py`.
DIAS_SIN_MOVIMIENTO = 180

_COLUMNAS = ["clave", "tipo", "severidad", "entidad", "detalle", "valor", "modulo"]


def _fila(clave, tipo, severidad, entidad, detalle, valor, modulo):
    return {
        "clave": clave,
        "tipo": tipo,
        "severidad": severidad,
        "entidad": entidad,
        "detalle": detalle,
        "valor": float(valor) if valor is not None else None,
        "modulo": modulo,
    }


def _quiebres(salud: pd.DataFrame, abc: pd.DataFrame) -> list[dict]:
    """Referencias sin stock pero con demanda reciente.

    Solo se alertan las de clase A y B: un quiebre en clase C con demanda
    marginal no compite por la atención de nadie, y meterlo acá diluiría
    los que sí importan.

    **Solo catálogo vigente (DEC-067).** Un quiebre de producto descontinuado
    no es un quiebre: nadie repone lo que ya no se vende. Sin este filtro, 2
    de las 7 alertas críticas apuntaban a producto muerto (`PJ51`, `PJ32-27`).

    El filtro es por la marca del admin, **no por el nombre**: las averías se
    venden por Outlet y varias están vigentes. `PRA AVERIA` sigue alertando y
    está bien — vendió 74 unidades en 90 días y quedó en cero.

    Contraste deliberado con `_inmovilizado()`, que **sí** mira el
    descontinuado: ahí la pregunta es de capital parado, y producto muerto con
    stock es exactamente eso. La vigencia importa cuando la pregunta es de
    abastecimiento, no cuando es de capital.
    """
    if salud.empty:
        return []
    if "vigencia" in salud.columns:
        salud = salud[salud["vigencia"] == VIGENCIA_ACTIVO]
        if salud.empty:
            return []
    clases = (
        abc[abc["nivel"] == "referencia"].set_index("clave")["abc"].to_dict()
        if not abc.empty
        else {}
    )
    filas = []
    for r in salud[salud["estado"] == "Quiebre"].itertuples(index=False):
        clase = clases.get(r.referencia)
        if clase not in ("A", "B"):
            continue
        filas.append(
            _fila(
                f"quiebre:{r.referencia}",
                "Quiebre de stock",
                CRITICA if clase == "A" else ALTA,
                r.referencia,
                f"Clase {clase} · sin disponible · {r.demanda_90d:,.0f} unidades vendidas "
                f"en 90 días".replace(",", "."),
                r.demanda_90d,
                "Salud",
            )
        )
    return filas


def _sobrantes(comparacion: pd.DataFrame) -> list[dict]:
    """Altura por encima del teórico: hubo movimiento físico sin registrar.

    **Agregado, no una alerta por referencia.** El primer intento las listó
    una por una y dieron 174 filas — el 83% del centro de alertas —
    sepultando los 27 quiebres, que sí son accionables uno a uno.

    Un sobrante que lleva meses ahí no es una excepción del día: es una
    condición conocida (DEC-051), con página propia y seguimiento de
    tendencia. Y atender uno individualmente es ir a contar esa posición,
    que es justo lo que la cola de conteo ya prioriza por clase y valor.
    Duplicarlo acá no agrega una decisión, agrega ruido.
    """
    if comparacion.empty or "sobrante_altura" not in comparacion.columns:
        return []
    sobra = comparacion[comparacion["sobrante_altura"] > 0]
    if sobra.empty:
        return []
    unidades = sobra["sobrante_altura"].sum()
    return [
        _fila(
            "sobrante:total",
            "Sobrante físico",
            ALTA,
            "Altura",
            f"{len(sobra):,} referencias con {unidades:,.0f} unidades por encima del "
            f"teórico · mayor: {sobra.loc[sobra['sobrante_altura'].idxmax(), 'referencia']}".replace(
                ",", "."
            ),
            unidades,
            "Inventario",
        )
    ]


def _discrepancias(conteos: pd.DataFrame) -> list[dict]:
    """Conteos fuera de tolerancia que todavía nadie resolvió.

    Clase A es Crítica sin importar el monto: el plan de inventario exige
    doble verificación para **cualquier** ajuste de clase A.

    **DEC-070: solo las que siguen abiertas.** Antes alertaba toda
    discrepancia para siempre, incluidas las ya recontadas y las ya
    ajustadas — una alerta que no se apaga cuando el problema se resuelve
    enseña a ignorar el panel.
    """
    if conteos.empty:
        return []
    malas = conteos[conteos["exacta"] == 0]
    if "estado_ciclo" in malas.columns:
        malas = malas[
            malas["estado_ciclo"].isin([ESTADO_CONTEO_PENDIENTE, ESTADO_CONTEO_CONFIRMADO])
        ]
    if malas.empty:
        return []
    filas = []
    for r in malas.itertuples(index=False):
        clase = str(r.clase or "")[:1]
        filas.append(
            _fila(
                f"discrepancia:{r.ubicacion}:{r.id_especificacion}:{r.fecha}",
                "Discrepancia de conteo",
                CRITICA if clase == "A" else ALTA,
                r.ubicacion,
                f"{r.hallazgo} de {abs(r.diferencia):,.0f} unidades · contó {r.contado_por}".replace(
                    ",", "."
                ),
                abs(r.diferencia),
                "Plan de conteo",
            )
        )
    return filas


def _cobertura_de_conteo(ubicaciones: pd.DataFrame, conteos: pd.DataFrame) -> list[dict]:
    """Posiciones de clase alta que nunca se contaron.

    **Agregada a propósito.** Hoy son más de mil: listarlas una por una
    sepultaría todo lo demás. Lo accionable es el número y la tendencia,
    no el detalle — ese vive en la cola de conteo, que es donde se trabaja.
    """
    if ubicaciones.empty:
        return []
    altura = ubicaciones[ubicaciones["tipo"] == "Altura"]
    # DEC-067: el criterio vive en comun/ y se aplica sobre `clase_posicion`.
    # Antes era un startswith local acá y un isin(["A","B"]) sobre otra columna
    # en la cola de conteo — dos definiciones del mismo concepto, 1.817 contra
    # 1.516 posiciones.
    criticas = altura[altura["clase_posicion"].map(es_conteo_prioritario)]
    contadas = set(conteos["ubicacion"]) if not conteos.empty else set()
    pendientes = criticas[~criticas["ubicacion"].isin(contadas)]["ubicacion"].nunique()
    if not pendientes:
        return []
    return [
        _fila(
            "cobertura:altura_ab",
            "Cobertura de conteo",
            MEDIA,
            "Altura, clases A y B",
            f"{pendientes:,} posiciones sin ningún conteo registrado".replace(",", "."),
            pendientes,
            "Plan de conteo",
        )
    ]


def _inmovilizado(salud: pd.DataFrame) -> list[dict]:
    """Stock que lleva medio año sin salir. Capital quieto, y ocupa posición."""
    if salud.empty or "dias_sin_salida" not in salud.columns:
        return []
    quieto = salud[(salud["dias_sin_salida"] > DIAS_SIN_MOVIMIENTO) & (salud["disponible"] > 0)]
    return [
        _fila(
            f"inmovilizado:{r.referencia}",
            "Sin movimiento",
            MEDIA,
            r.referencia,
            f"{r.disponible:,.0f} unidades · {r.dias_sin_salida:,.0f} días sin salida".replace(
                ",", "."
            ),
            r.disponible,
            "Salud",
        )
        for r in quieto.itertuples(index=False)
    ]


def _frescura(frescura: dict) -> list[dict]:
    """Las fuentes no se actualizaron: todo lo de abajo habla del pasado.

    Va como Crítica porque invalida a las demás alertas — decidir sobre un
    quiebre calculado con datos de ayer es peor que no decidir.
    """
    if not frescura or not frescura.get("datos_desactualizados"):
        return []
    horas = frescura.get("fuente_mas_vieja_h")
    return [
        _fila(
            "frescura:fuentes",
            "Datos desactualizados",
            CRITICA,
            "Admin / Bochica",
            f"La fuente más vieja tiene {horas:.0f} h"
            if horas is not None
            else "Falta alguna de las fuentes",
            horas,
            "Inventario",
        )
    ]


def _calidad(hallazgos: list) -> list[dict]:
    """Los detectores de calidad de datos, resumidos como alerta.

    No duplica el módulo de Tareas: lo referencia. Cada hallazgo aporta una
    línea con su conteo, y el detalle sigue viviendo donde se trabaja.
    """
    severidades = {"Alta": ALTA, "Media": MEDIA, "Baja": MEDIA}
    return [
        _fila(
            f"calidad:{h.clave}",
            "Calidad de datos",
            severidades.get(h.prioridad, MEDIA),
            h.titulo,
            f"{h.cantidad:,} {h.unidad}".replace(",", "."),
            h.cantidad,
            "Tareas",
        )
        for h in (hallazgos or [])
    ]


def _cancelaciones(cancelaciones: pd.DataFrame) -> list[dict]:
    """Mercancía que se alistó y terminó cancelada.

    **Agregada, y no por su antigüedad sino por su rezago.** Lo accionable
    no es cada subpedido —la mayoría se canceló hace meses— sino que el
    proceso deja mercancía en estado indeterminado durante semanas: el 86%
    se cancela con más de un mes de rezago desde que cerró el alistamiento.
    """
    if cancelaciones is None or cancelaciones.empty:
        return []
    unidades = cancelaciones["unidades"].sum()
    tardias = int((cancelaciones["dias_hasta_cancelacion"] > 30).sum())
    return [
        _fila(
            "cancelaciones:alistadas",
            "Alistado y cancelado",
            MEDIA,
            "Mercancía sin despachar",
            f"{len(cancelaciones):,} subpedidos alistados que se cancelaron después "
            f"({unidades:,.0f} unidades) · {tardias} con más de un mes de rezago".replace(",", "."),
            unidades,
            "Inventario",
        )
    ]


def generar_alertas(
    salud: pd.DataFrame,
    comparacion: pd.DataFrame,
    ubicaciones: pd.DataFrame,
    conteos: pd.DataFrame,
    abc: pd.DataFrame,
    frescura: dict | None = None,
    hallazgos: list | None = None,
    cancelaciones: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Recorre el estado del sistema y devuelve lo que requiere acción.

    Args:
        salud: Resultado de `salud.calcular_salud()`.
        comparacion: Resultado de `comparacion.comparar()`.
        ubicaciones: Resultado de `ubicaciones.calcular_ubicaciones()`.
        conteos: Resultado de `conteos.evaluar_conteos()`.
        abc: Resultado de `clasificacion.calcular_clasificacion()`.
        frescura: Resultado de `persistencia.medir_frescura()`.
        hallazgos: Resultado de `hallazgos.detectar_todos()`.
        cancelaciones: Resultado de
            `cancelaciones.calcular_cancelaciones()` (DEC-063).

    Returns:
        Una fila por alerta, ordenada por severidad y magnitud.
    """
    vacio = pd.DataFrame(columns=_COLUMNAS)
    conteos = conteos if conteos is not None else vacio

    filas = [
        *_frescura(frescura or {}),
        *_quiebres(salud, abc),
        *_discrepancias(conteos),
        *_sobrantes(comparacion),
        *_cobertura_de_conteo(ubicaciones, conteos),
        *_inmovilizado(salud),
        *_cancelaciones(cancelaciones),
        *_calidad(hallazgos),
    ]
    if not filas:
        logger.info("generar_alertas: sin excepciones abiertas")
        return vacio

    df = pd.DataFrame(filas, columns=_COLUMNAS)
    df["_orden"] = df["severidad"].map({s: i for i, s in enumerate(ORDEN_SEVERIDAD)})
    df = df.sort_values(["_orden", "valor"], ascending=[True, False]).drop(columns="_orden")

    por_severidad = df["severidad"].value_counts().to_dict()
    logger.info(
        "generar_alertas: %d alertas (%s)",
        len(df),
        " · ".join(f"{s}: {por_severidad.get(s, 0)}" for s in ORDEN_SEVERIDAD),
    )
    return df.reset_index(drop=True)
