"""
entregas.py — El compromiso de entrega y su cumplimiento (DEC-095).

`pedidos.hora_entrega` es la franja **pactada**, no el momento en que se
entregó (DEC-093). Cruzada contra el evento `Entrega` de
`registro_operaciones` da la mitad *on time* de OTIF, que `CLAUDE.md` daba por
imposible "sin fecha comprometida en el origen".

**El campo tiene tres formatos y el origen los fue cambiando**, cosa que la
auditoría de DEC-094 no había visto porque solo miró el de la franja:

| Formato | Ejemplo | Cuándo | Filas |
|---|---|---|---|
| `punto` | `2026-03-05 14:00` | feb → may 2026 | 11.046 |
| `franja` | `2026-08-01 08:00 ~ 09:00` | may 2026 → hoy | 9.430 |
| sin hora | `Cualquier hora` | ene → jun 2026 | 239 |

Que el hueco contra la entrega real sea el mismo antes y después del cambio de
formato —130 h en `punto`, 99 h en `franja`— es lo que descarta que sea un
artefacto de parseo: el origen cambió cómo lo escribe y la distancia no se
movió.

**Dos escalas, las dos correctas, como en DEC-069.** Publicar solo la estricta
haría ver un problema donde puede haber una convención:

- **Por día** — se entregó el día pactado o antes. Es el estándar del sector y
  el que tolera que una franja de las 8 se cumpla a las 11.
- **Por ventana** — se entregó dentro de la franja (o antes del instante, en el
  formato `punto`). Es la lectura estricta.

`Cualquier hora` **no es un compromiso** y queda fuera de las dos: contarlo
como incumplido inventaría una promesa que nadie hizo.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import NamedTuple

# Los tres formatos que emite el origen.
TIPO_FRANJA = "franja"
TIPO_PUNTO = "punto"
TIPO_SIN_HORA = "sin_hora"

# Veredictos de la escala por día.
A_TIEMPO = "A tiempo"
TARDE = "Tarde"

# Veredictos de la escala por ventana.
EN_VENTANA = "En la ventana"
ANTES = "Antes de la ventana"
FUERA = "Fuera de la ventana"

# Texto exacto con que el origen dice "sin hora comprometida".
_SIN_HORA = "cualquier hora"

_RE_FRANJA = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})$")
_RE_PUNTO = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})$")


class Compromiso(NamedTuple):
    """Lo que se le prometió al cliente, ya parseado.

    `inicio` y `fin` son iguales en el formato `punto` — un instante es una
    ventana de ancho cero, y tratarlo así evita ramificar en cada consumidor.
    Ambos son `None` cuando no hay hora comprometida.
    """

    tipo: str
    fecha: date | None
    inicio: datetime | None
    fin: datetime | None

    @property
    def es_fechado(self) -> bool:
        """True si hay una fecha contra la cual medir cumplimiento."""
        return self.fecha is not None


def parsear_compromiso(texto: str | None) -> Compromiso | None:
    """Interpreta `hora_entrega` en cualquiera de sus tres formatos.

    Args:
        texto: Valor crudo de `pedidos.hora_entrega`.

    Returns:
        El compromiso parseado, o `None` si el campo está vacío o trae un
        placeholder del origen (`-`). `None` significa "no hay dato", que no es
        lo mismo que `Cualquier hora` —"hay dato y dice que no hay hora"—.
    """
    if texto is None:
        return None
    limpio = texto.strip()
    if limpio in ("", "-", "--"):
        return None

    if limpio.lower() == _SIN_HORA:
        return Compromiso(TIPO_SIN_HORA, None, None, None)

    m = _RE_FRANJA.match(limpio)
    if m:
        dia = date.fromisoformat(m.group(1))
        return Compromiso(
            TIPO_FRANJA,
            dia,
            datetime.fromisoformat(f"{m.group(1)} {m.group(2)}"),
            datetime.fromisoformat(f"{m.group(1)} {m.group(3)}"),
        )

    m = _RE_PUNTO.match(limpio)
    if m:
        dia = date.fromisoformat(m.group(1))
        instante = datetime.fromisoformat(limpio)
        return Compromiso(TIPO_PUNTO, dia, instante, instante)

    # Formato desconocido: se devuelve None en vez de adivinar. El detector de
    # vocabulario (DEC-081) es el que tiene que avisar que apareció uno nuevo.
    return None


def _a_datetime(momento: str | datetime | None) -> datetime | None:
    """Normaliza el momento de entrega, que llega como texto desde SQLite."""
    if momento is None:
        return None
    if isinstance(momento, datetime):
        return momento
    texto = str(momento).strip().replace("T", " ")[:19]
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        return None


def dias_de_desvio(
    compromiso: Compromiso | None, entregado_en: str | datetime | None
) -> int | None:
    """Días entre la fecha pactada y la de entrega. Negativo = se adelantó.

    `None` cuando no hay contra qué medir: sin compromiso fechado, o sin
    entrega registrada todavía.
    """
    entrega = _a_datetime(entregado_en)
    if compromiso is None or not compromiso.es_fechado or entrega is None:
        return None
    assert compromiso.fecha is not None  # es_fechado ya lo garantiza
    return (entrega.date() - compromiso.fecha).days


def clasificar_por_dia(
    compromiso: Compromiso | None, entregado_en: str | datetime | None
) -> str | None:
    """Escala del sector: se cumplió si se entregó el día pactado o antes."""
    desvio = dias_de_desvio(compromiso, entregado_en)
    if desvio is None:
        return None
    return A_TIEMPO if desvio <= 0 else TARDE


def clasificar_por_ventana(
    compromiso: Compromiso | None, entregado_en: str | datetime | None
) -> str | None:
    """Escala estricta: se cumplió si cayó dentro de la franja comprometida.

    En el formato `punto` la ventana tiene ancho cero, así que solo `Antes` y
    `Fuera` son alcanzables — entregar en el minuto exacto es posible pero
    excepcional, y no hace falta un caso especial para eso.
    """
    entrega = _a_datetime(entregado_en)
    if compromiso is None or not compromiso.es_fechado or entrega is None:
        return None
    assert compromiso.inicio is not None and compromiso.fin is not None
    if entrega < compromiso.inicio:
        return ANTES
    if entrega <= compromiso.fin:
        return EN_VENTANA
    return FUERA


def horas_de_atraso(
    compromiso: Compromiso | None, entregado_en: str | datetime | None
) -> float | None:
    """Horas transcurridas desde el cierre de la ventana. 0 si no hubo atraso."""
    entrega = _a_datetime(entregado_en)
    if compromiso is None or not compromiso.es_fechado or entrega is None:
        return None
    assert compromiso.fin is not None
    atraso = (entrega - compromiso.fin).total_seconds() / 3600
    return max(0.0, atraso)


def esta_vencido(
    compromiso: Compromiso | None,
    entregado_en: str | datetime | None,
    ahora: datetime | None = None,
) -> bool:
    """True si la ventana ya cerró y la entrega **todavía no se registró**.

    Es la mitad accionable del análisis: no mira el pasado sino la promesa que
    se está incumpliendo ahora mismo. Un pedido ya entregado nunca está
    vencido, por tarde que haya llegado.
    """
    if entregado_en is not None and _a_datetime(entregado_en) is not None:
        return False
    if compromiso is None or not compromiso.es_fechado:
        return False
    assert compromiso.fin is not None
    return compromiso.fin < (ahora or datetime.now())
