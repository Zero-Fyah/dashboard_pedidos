"""Configuración del ciclo horario para una bodega (DEC-164).

`scraper/actualizar_pedidos.sh` recorre las bodegas en secuencia y, antes de
cada una, pregunta acá si corre, en qué modo va el scraper y qué pasos se
saltan. Lee el archivo de configuración de la bodega con `dotenv_values`,
sin cargarlo en el entorno: si el shell hiciera `source .env`, las
credenciales de esa bodega quedarían exportadas a todos los procesos que
siguen, incluidos los de la otra bodega (DEC-163, punto 4).

Variables, en el archivo de cada bodega (`.env`, `.env.bodega2`):

    CICLO_ACTIVO         1 corre, 0 se salta la bodega entera.
                         Por defecto: miempresa 1, bodega2 0 — la nueva no
                         corre hasta que el Arquitecto la active, después de
                         su carga inicial.
    SCRAPER_MODO_CICLO   incremental | cierre (DEC-164).
                         Por defecto: miempresa cierre, bodega2 incremental.
    CICLO_PASOS_APAGADOS Pasos a saltar, separados por coma o espacio
                         (nombres de `PASOS`). Por defecto, ninguno.

Uso, desde la raíz del proyecto:

    eval "$(python scripts/pasos_bodega.py bodega2)"

Imprime asignaciones de shell con valores ya validados (nunca texto del
archivo tal cual), así que el `eval` no puede ejecutar nada inesperado. Sale
con 2 si la bodega no existe o si la configuración trae un valor inválido:
un paso mal escrito no debe quedar encendido en silencio.
"""

from __future__ import annotations

import re
import sys

from dotenv import dotenv_values

from comun import BODEGA_DEFECTO, BODEGAS, BodegaIncorrectaError, ruta_bodega

# Los pasos del ciclo, con los nombres que usa `actualizar_pedidos.sh`.
PASOS: tuple[str, ...] = (
    "scraper",
    "inventario_admin",
    "bochica",
    "cambios_inventario",
    "movimientos_bochica",
    "inventario_persistencia",
    "sheets",
    "mantenimiento",
    "etl",
    "respaldo",
    "respaldo_remoto",
)
MODOS_CICLO: tuple[str, ...] = ("incremental", "cierre")

_DEFECTOS: dict[str, dict[str, str]] = {
    BODEGA_DEFECTO: {"CICLO_ACTIVO": "1", "SCRAPER_MODO_CICLO": "cierre"},
    "bodega2": {"CICLO_ACTIVO": "0", "SCRAPER_MODO_CICLO": "incremental"},
}


class ConfiguracionInvalidaError(ValueError):
    """Un valor del archivo de la bodega no es válido para el ciclo."""


def configuracion(bodega: str, valores: dict[str, str | None]) -> dict[str, str]:
    """Valida la configuración del ciclo de `bodega` a partir de su archivo.

    Args:
        bodega: Una de `BODEGAS`.
        valores: Variables del archivo de la bodega (`dotenv_values`); las
            ausentes o vacías toman el valor por defecto de la bodega.

    Returns:
        `{"activo": "0"|"1", "modo": <MODOS_CICLO>, "apagados": "<pasos>"}`.

    Raises:
        ConfiguracionInvalidaError: si algún valor no es válido.
    """
    defectos = _DEFECTOS[bodega]

    def _valor(clave: str) -> str:
        return (valores.get(clave) or "").strip() or defectos.get(clave, "")

    activo = _valor("CICLO_ACTIVO")
    if activo not in ("0", "1"):
        raise ConfiguracionInvalidaError(f"CICLO_ACTIVO debe ser 0 o 1 (llegó {activo!r})")
    modo = _valor("SCRAPER_MODO_CICLO")
    if modo not in MODOS_CICLO:
        raise ConfiguracionInvalidaError(
            f"SCRAPER_MODO_CICLO debe ser {' o '.join(MODOS_CICLO)} (llegó {modo!r})"
        )
    apagados = [p for p in re.split(r"[\s,]+", _valor("CICLO_PASOS_APAGADOS")) if p]
    desconocidos = sorted(set(apagados) - set(PASOS))
    if desconocidos:
        raise ConfiguracionInvalidaError(
            f"CICLO_PASOS_APAGADOS trae pasos desconocidos: {', '.join(desconocidos)} "
            f"(válidos: {', '.join(PASOS)})"
        )
    return {"activo": activo, "modo": modo, "apagados": " ".join(dict.fromkeys(apagados))}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in BODEGAS:
        print(f"uso: pasos_bodega.py <{'|'.join(BODEGAS)}>", file=sys.stderr)
        return 2
    bodega = argv[0]
    archivo = ruta_bodega("env", bodega)
    try:
        conf = configuracion(bodega, dotenv_values(archivo) if archivo.exists() else {})
    except (ConfiguracionInvalidaError, BodegaIncorrectaError) as e:
        print(f"pasos_bodega: {archivo.name}: {e}", file=sys.stderr)
        return 2
    # Sin archivo, la bodega que no es la por defecto no puede correr: no
    # tiene credenciales ni URLs propias, y nunca cae al `.env` de la otra.
    if bodega != BODEGA_DEFECTO and not archivo.exists():
        conf["activo"] = "0"
    print(f"CICLO_ACTIVO={conf['activo']}")
    print(f"MODO_SCRAPER={conf['modo']}")
    print(f'PASOS_APAGADOS=" {conf["apagados"]} "')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
