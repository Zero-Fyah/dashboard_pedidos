"""
conteos.py — Ingesta de conteos físicos desde Excel (DEC-058).

Cierra el ciclo del plan de inventario: hasta acá el dashboard **emitía**
trabajo (la cola de conteo) pero no podía **recibir** el resultado, así que
la misma posición volvía a salir aunque se hubiera contado el día anterior,
y no había forma de calcular exactitud ni avance.

El flujo es deliberadamente de papel-y-Excel, que es como el área va a
arrancar:

1. El dashboard emite la hoja de conteo (formato del Anexo A del plan).
2. Alguien cuenta y llena la columna de cantidad física.
3. El archivo se guarda en `data/conteos/`.
4. El scheduler lo levanta en la siguiente corrida.

**Por qué la hoja no lleva la cantidad del sistema.** Un conteo que muestra
lo esperado deja de ser un conteo: quien cuenta ancla en ese número y
confirma en vez de verificar. El plan ya usa el concepto de conteo ciego en
su sección de auditoría cruzada. La comparación se hace acá, contra el
inventario de Bochica del momento de la ingesta.

**La carpeta es el origen de verdad, y por eso el conteo se puede
deshacer.** La tabla `conteos` es un espejo de lo que hay en
`data/conteos/`: se reconstruye entera en cada corrida. Eso da la
operación que faltaba —**sacar el archivo deshace su conteo**— y mantiene
la base siempre reconciliable con el disco.

La primera versión acumulaba y nunca borraba, para no perder un conteo.
Estaba mal planteado: en un proceso de papel el registro durable **es el
Excel**, no la fila derivada. Acumular no protegía el dato, solo impedía
corregirlo — y con cuatro asistentes llenando hojas, cargar una mal es
cuestión de tiempo.

Para que quitar un archivo no sea una pérdida silenciosa:

- Mover la hoja a `data/conteos/anulados/` la saca del cálculo **sin
  borrarla del disco**. Es el gesto recomendado; borrarla también funciona.
- `conteos_archivos` **sí** acumula: deja constancia permanente de cada
  archivo que alguna vez se ingirió, cuántas filas traía y cuándo dejó de
  verse. Si una hoja desaparece, queda el rastro de que existió.

⚠️ `data/conteos/` no está versionada y es el único original de los
conteos: necesita respaldo propio.
"""

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from comun import (
    COLUMNAS_CONTEO_REQUERIDAS,
    COLUMNAS_HOJA_CONTEO,
    ESTADO_CONTEO_COINCIDE,
    ESTADO_CONTEO_CONFIRMADO,
    ESTADO_CONTEO_DESCARTADO,
    ESTADO_CONTEO_PENDIENTE,
    ESTADOS_CICLO_CONTEO,
    TOLERANCIA_CONTEO_DEFECTO,
    TOLERANCIA_POR_CLASE,
)

logger = logging.getLogger("inventario.conteos")

RUTA_CONTEOS_DEFAULT = Path(__file__).parent.parent / "data" / "conteos"

HOJA_CONTEO = "conteo"

# Mover una hoja acá la excluye del cálculo sin borrarla del disco.
SUBCARPETA_ANULADOS = "anulados"

# El contrato de columnas vive en `comun/`: el dashboard emite la hoja y
# este módulo la lee de vuelta, así que declararlo dos veces dejaría que un
# cambio en el generador rompiera al lector en silencio (DEC-022, DRY).
COLUMNAS_REQUERIDAS = COLUMNAS_CONTEO_REQUERIDAS
COLUMNAS_OPCIONALES = tuple(
    c for c in COLUMNAS_HOJA_CONTEO if c not in COLUMNAS_CONTEO_REQUERIDAS and c != "referencia"
)
TOLERANCIA_DEFECTO = TOLERANCIA_CONTEO_DEFECTO

_COLUMNAS = [
    "ubicacion",
    "id_especificacion",
    "fecha",
    "contado_por",
    "cantidad_contada",
    "cantidad_sistema",
    "diferencia",
    "diferencia_pct",
    "clase",
    "tipo",
    "causa",
    "tolerancia",
    "exacta",
    "hallazgo",
    "lote",
    "vencimiento",
    "actividad_origen",
    "observacion",
    "archivo",
    # DEC-070: cierre del ciclo. `intento` numera los conteos de la misma
    # línea por fecha; `estado_ciclo` dice en qué quedó la discrepancia.
    "intento",
    "estado_ciclo",
    "dias_abierto",
]


def cargar_conteos(ruta: Path | None = None) -> pd.DataFrame:
    """Lee todos los Excel de conteo de la carpeta y los apila.

    Un archivo ilegible o con columnas faltantes **no tumba a los demás**:
    se registra y se sigue. El área va a estar aprendiendo el formato en las
    primeras semanas, y perder los conteos buenos por un archivo malo sería
    el peor momento para ser estricto.

    Args:
        ruta: Carpeta con los `.xlsx`. Default: `data/conteos/`.

    Returns:
        Un DataFrame con las filas de todos los archivos y la columna
        `archivo` para trazar el origen. Vacío si no hay ninguno.
    """
    carpeta = ruta or RUTA_CONTEOS_DEFAULT
    if not carpeta.exists():
        logger.info("cargar_conteos: no existe %s — todavía no hay conteos", carpeta)
        return pd.DataFrame(columns=[*COLUMNAS_REQUERIDAS, "archivo"])

    partes = []
    # `glob` no recursivo: lo que esté en `anulados/` queda fuera solo.
    archivos = sorted(carpeta.glob("*.xlsx"))
    for archivo in archivos:
        if archivo.name.startswith("~$"):  # temporal de Excel abierto
            continue
        try:
            df = pd.read_excel(archivo)
            df.columns = [str(c).strip().lower() for c in df.columns]
            faltantes = [c for c in COLUMNAS_REQUERIDAS if c not in df.columns]
            if faltantes:
                logger.warning(
                    "cargar_conteos: %s ignorado — le faltan columnas %s",
                    archivo.name,
                    faltantes,
                )
                continue
            df["archivo"] = archivo.name
            partes.append(df)
        except Exception:
            logger.exception("cargar_conteos: %s ilegible; se continúa", archivo.name)

    if not partes:
        logger.info("cargar_conteos: %d archivos en %s, ninguno utilizable", len(archivos), carpeta)
        return pd.DataFrame(columns=[*COLUMNAS_REQUERIDAS, "archivo"])

    crudo = pd.concat(partes, ignore_index=True)
    logger.info(
        "cargar_conteos: %d filas desde %d archivo(s)", len(crudo), crudo["archivo"].nunique()
    )
    return crudo


def inventariar_archivos(crudo: pd.DataFrame, ruta: Path | None = None) -> pd.DataFrame:
    """Qué hojas se están ingiriendo ahora mismo.

    Alimenta `conteos_archivos`, que **sí** acumula: es el rastro de que un
    archivo existió. Sin él, mover una hoja a `anulados/` la haría
    desaparecer del sistema sin dejar constancia, y nadie podría reconstruir
    después por qué el IRA cambió de un día para el otro.

    Args:
        crudo: Resultado de `cargar_conteos()`.
        ruta: Carpeta de conteos, para leer la fecha de cada archivo.

    Returns:
        Una fila por archivo presente, con sus filas y su fecha de archivo.
    """
    columnas = ["archivo", "filas", "modificado_en", "visto_en"]
    if crudo.empty or "archivo" not in crudo.columns:
        return pd.DataFrame(columns=columnas)

    carpeta = ruta or RUTA_CONTEOS_DEFAULT
    ahora = dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds")
    filas = []
    for nombre, grupo in crudo.groupby("archivo"):
        destino = carpeta / str(nombre)
        modificado = (
            dt.datetime.fromtimestamp(destino.stat().st_mtime, tz=dt.timezone.utc).isoformat(
                timespec="seconds"
            )
            if destino.exists()
            else None
        )
        filas.append(
            {
                "archivo": nombre,
                "filas": len(grupo),
                "modificado_en": modificado,
                "visto_en": ahora,
            }
        )
    return pd.DataFrame(filas, columns=columnas)


def evaluar_conteos(
    crudo: pd.DataFrame, ubicaciones: pd.DataFrame, *, hoy: dt.date | None = None
) -> pd.DataFrame:
    """Compara cada conteo contra el sistema y decide si está dentro de tolerancia.

    Args:
        crudo: Resultado de `cargar_conteos()`.
        ubicaciones: Resultado de `ubicaciones.calcular_ubicaciones()`, que
            aporta la cantidad del sistema y la clase de cada línea.
        hoy: Fecha de referencia para la antigüedad del ciclo (DEC-070),
            inyectable para tests.

    Returns:
        Un DataFrame por `(ubicacion, id_especificacion, fecha, contado_por)`
        con la diferencia, el veredicto de exactitud y el estado del ciclo
        de cierre (DEC-070).
    """
    if crudo.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    df = crudo.copy()
    for columna in COLUMNAS_OPCIONALES:
        if columna not in df.columns:
            df[columna] = None

    df["ubicacion"] = df["ubicacion"].astype(str).str.strip().str.upper()
    df["id_especificacion"] = df["id_especificacion"].astype(str).str.strip()
    df["contado_por"] = df["contado_por"].astype(str).str.strip()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["cantidad_contada"] = pd.to_numeric(df["cantidad_contada"], errors="coerce")

    invalidas = df["fecha"].isna() | df["cantidad_contada"].isna()
    if invalidas.any():
        logger.warning(
            "evaluar_conteos: %d filas descartadas por fecha o cantidad ilegible",
            int(invalidas.sum()),
        )
        df = df[~invalidas]
    if df.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    sistema = ubicaciones[["ubicacion", "id_especificacion", "cantidad", "clase", "tipo"]].copy()
    sistema["ubicacion"] = sistema["ubicacion"].astype(str).str.strip().str.upper()
    sistema["id_especificacion"] = sistema["id_especificacion"].astype(str).str.strip()
    df = df.merge(
        sistema.rename(columns={"cantidad": "cantidad_sistema"}),
        on=["ubicacion", "id_especificacion"],
        how="left",
    )

    # Contar algo que el sistema no tiene en esa posición es un sobrante, no
    # un error de la hoja: cantidad esperada cero, diferencia completa.
    df["cantidad_sistema"] = df["cantidad_sistema"].fillna(0.0)
    df["diferencia"] = df["cantidad_contada"] - df["cantidad_sistema"]
    # Con sistema en cero cualquier diferencia es infinita en términos
    # relativos; se deja sin porcentaje en vez de propagar un inf.
    df["diferencia_pct"] = (df["diferencia"] / df["cantidad_sistema"].replace(0, pd.NA)).abs()

    clase_base = df["clase"].fillna("").astype(str).str[0]
    df["tolerancia"] = clase_base.map(TOLERANCIA_POR_CLASE).fillna(TOLERANCIA_DEFECTO)
    # Coincidencia exacta o dentro de tolerancia. El caso sistema-en-cero
    # solo pasa si lo contado también es cero.
    df["exacta"] = (
        ((df["diferencia"] == 0) | (df["diferencia_pct"] <= df["tolerancia"]))
        .fillna(False)
        .astype(int)
    )

    # La causa solo aplica donde hubo diferencia: dejarla en un conteo que
    # coincide ensuciaría el Pareto con causas de nada.
    df["causa"] = df["causa"].where(df["exacta"] == 0)

    df["hallazgo"] = df["hallazgo"].fillna(
        pd.Series(
            [
                "Coincide" if e else ("Sobrante" if d > 0 else "Faltante")
                for e, d in zip(df["exacta"], df["diferencia"], strict=True)
            ],
            index=df.index,
        )
    )

    resultado = df.drop_duplicates(
        subset=["ubicacion", "id_especificacion", "fecha", "contado_por"], keep="last"
    )
    resultado = _cerrar_ciclo(resultado, ubicaciones, hoy=hoy)
    logger.info(
        "evaluar_conteos: %d conteos · %d exactos (%.1f%%) · %d posiciones · %d sin match en sistema",
        len(resultado),
        int(resultado["exacta"].sum()),
        resultado["exacta"].mean() * 100,
        resultado["ubicacion"].nunique(),
        int((resultado["cantidad_sistema"] == 0).sum()),
    )
    return resultado[_COLUMNAS]


def _cerrar_ciclo(
    conteos: pd.DataFrame, ubicaciones: pd.DataFrame, *, hoy: dt.date | None = None
) -> pd.DataFrame:
    """Clasifica en qué quedó cada discrepancia (DEC-070).

    El ciclo de DEC-058/060 terminaba en la detección. Este paso agrega el
    tramo que falta —recuento y ajuste— **sin pedirle a nadie que registre
    nada nuevo**, derivándolo de lo que ya está:

    - `intento`: el enésimo conteo de la misma línea, por fecha. El segundo
      **es** el recuento; no hace falta que el contador lo marque, y así no
      hay un campo más que se pueda llenar mal.
    - `estado_ciclo`:

      | Estado | Qué pasó |
      |---|---|
      | `Coincide` | El conteo dio dentro de tolerancia. Nada que cerrar. |
      | `Recuento corrige` | Un conteo posterior coincidió con el sistema: el primero estaba mal. |
      | `Recuento confirma` | Un conteo posterior repitió la diferencia: es real, falta ajustar. |
      | `Pendiente de recuento` | Hay diferencia y nadie volvió a contar. |

    **No hay estado "ajuste aplicado", y no es un olvido.** Se intentó
    deducirlo comparando lo contado contra el sistema actual, y no puede
    dispararse nunca: `cantidad_sistema` sale del mismo snapshot que "el
    sistema hoy", porque ambas tablas se reescriben enteras en cada corrida.
    Cuando una diferencia se ajusta, el conteo simplemente vuelve a evaluarse
    como `Coincide`. Ver DEC-070 para qué haría falta para distinguirlo.

    Args:
        conteos: Conteos ya evaluados contra el sistema.
        ubicaciones: Ubicaciones de la corrida actual.
        hoy: Fecha de referencia, inyectable para tests.

    Returns:
        El mismo DataFrame con `intento`, `estado_ciclo` y `dias_abierto`.
    """
    df = conteos.copy()
    if df.empty:
        for c in ("intento", "estado_ciclo", "dias_abierto"):
            df[c] = None
        return df

    referencia = hoy or dt.date.today()
    linea = ["ubicacion", "id_especificacion"]

    df = df.sort_values([*linea, "fecha"])
    df["intento"] = df.groupby(linea).cumcount() + 1

    # ¿Hubo un conteo posterior de la misma línea, y en qué quedó?
    ultimo = df.groupby(linea).agg(
        _ultimo_intento=("intento", "max"), _ultima_exacta=("exacta", "last")
    )
    df = df.merge(ultimo, on=linea, how="left")

    hubo_recuento = df["_ultimo_intento"] > 1

    df["estado_ciclo"] = ESTADO_CONTEO_PENDIENTE
    df.loc[hubo_recuento & (df["_ultima_exacta"] == 1), "estado_ciclo"] = ESTADO_CONTEO_DESCARTADO
    df.loc[hubo_recuento & (df["_ultima_exacta"] == 0), "estado_ciclo"] = ESTADO_CONTEO_CONFIRMADO
    # Coincidir manda sobre todo: no hay nada que cerrar.
    df.loc[df["exacta"] == 1, "estado_ciclo"] = ESTADO_CONTEO_COINCIDE

    dias = (pd.Timestamp(referencia) - pd.to_datetime(df["fecha"], errors="coerce")).dt.days
    abierto = df["estado_ciclo"].isin([ESTADO_CONTEO_PENDIENTE, ESTADO_CONTEO_CONFIRMADO])
    df["dias_abierto"] = dias.where(abierto)

    logger.info(
        "cerrar_ciclo: %s",
        " · ".join(
            f"{e}: {int((df['estado_ciclo'] == e).sum())}"
            for e in ESTADOS_CICLO_CONTEO
            if (df["estado_ciclo"] == e).any()
        )
        or "sin conteos",
    )
    return df.drop(columns=["_ultimo_intento", "_ultima_exacta"])


def calcular_ira(conteos: pd.DataFrame) -> pd.DataFrame:
    """IRA por clase: líneas exactas sobre líneas contadas.

    Es la fórmula de la sección 12 del plan, con las tolerancias por clase
    de la sección 4 ya aplicadas en `evaluar_conteos()`.

    Args:
        conteos: Resultado de `evaluar_conteos()`.

    Returns:
        Una fila por clase, más una fila `Global`.
    """
    if conteos.empty:
        return pd.DataFrame(columns=["clase", "contadas", "exactas", "ira", "ultima_fecha"])

    base = conteos.copy()
    # Se agrupa por la clase base: "A" y "A (por referencia)" son la misma
    # clase a efectos de exactitud. Tomar la inicial funcionaría por
    # accidente —"Sin rotación" también empieza por S— así que el mapeo es
    # explícito y todo lo que no sea A/B/C cae en una sola cubeta.
    inicial = base["clase"].fillna("").astype(str).str[0]
    base["clase"] = inicial.where(inicial.isin(TOLERANCIA_POR_CLASE), "Sin clase")
    por_clase = base.groupby("clase", as_index=False).agg(
        contadas=("exacta", "size"), exactas=("exacta", "sum"), ultima_fecha=("fecha", "max")
    )
    total = pd.DataFrame(
        [
            {
                "clase": "Global",
                "contadas": len(base),
                "exactas": int(base["exacta"].sum()),
                "ultima_fecha": base["fecha"].max(),
            }
        ]
    )
    r = pd.concat([por_clase, total], ignore_index=True)
    r["ira"] = (r["exactas"] / r["contadas"] * 100).round(1)
    return r


def ira_por_periodo(conteos: pd.DataFrame, periodo: str = "%Y-%m") -> pd.DataFrame:
    """Serie temporal del IRA, para ver si la exactitud mejora o se degrada.

    El IRA de un corte dice cómo está hoy; la serie dice si el trabajo está
    sirviendo. Es el indicador que la sección 12 del plan pide como
    "tendencia ascendente desde la semana 4".

    Args:
        conteos: Resultado de `evaluar_conteos()`.
        periodo: Formato de agrupación de la fecha. Por defecto, mensual.

    Returns:
        Una fila por `(periodo, clase)`, más la clase `Global` de cada
        periodo.
    """
    columnas = ["periodo", "clase", "contadas", "exactas", "ira"]
    if conteos.empty:
        return pd.DataFrame(columns=columnas)

    base = conteos.copy()
    base["periodo"] = pd.to_datetime(base["fecha"], errors="coerce").dt.strftime(periodo)
    base = base.dropna(subset=["periodo"])
    if base.empty:
        return pd.DataFrame(columns=columnas)

    inicial = base["clase"].fillna("").astype(str).str[0]
    base["clase"] = inicial.where(inicial.isin(TOLERANCIA_POR_CLASE), "Sin clase")

    por_clase = base.groupby(["periodo", "clase"], as_index=False).agg(
        contadas=("exacta", "size"), exactas=("exacta", "sum")
    )
    global_ = base.groupby("periodo", as_index=False).agg(
        contadas=("exacta", "size"), exactas=("exacta", "sum")
    )
    global_["clase"] = "Global"

    r = pd.concat([por_clase, global_], ignore_index=True)
    r["ira"] = (r["exactas"] / r["contadas"] * 100).round(1)
    return r[columnas].sort_values(["periodo", "clase"]).reset_index(drop=True)


def conformidad_por_tipo(conteos: pd.DataFrame) -> pd.DataFrame:
    """Conformidad **por posición**, separando picking de altura.

    Ojo con la diferencia respecto del IRA: el plan define la Tasa de
    Conformidad en Auditoría de Picking como *posiciones auditadas sin
    discrepancia / posiciones auditadas* — a nivel de **posición**, no de
    línea. Una posición con cinco líneas y una sola mal no es "80%
    conforme": es una posición no conforme.

    Separarlo por tipo importa porque son dos gobernanzas distintas: altura
    la ejecuta Inventarios, picking es de Bodega y el indicador se le
    reporta a ella (sección 3.6 del plan).

    Args:
        conteos: Resultado de `evaluar_conteos()`.

    Returns:
        Una fila por tipo de ubicación, con posiciones auditadas y conformes.
    """
    columnas = ["tipo", "posiciones", "conformes", "conformidad", "ultima_fecha"]
    if conteos.empty or "tipo" not in conteos.columns:
        return pd.DataFrame(columns=columnas)

    por_posicion = conteos.groupby(["tipo", "ubicacion"], as_index=False).agg(
        conforme=("exacta", "min"), ultima=("fecha", "max")
    )
    r = por_posicion.groupby("tipo", as_index=False).agg(
        posiciones=("conforme", "size"),
        conformes=("conforme", "sum"),
        ultima_fecha=("ultima", "max"),
    )
    r["conformidad"] = (r["conformes"] / r["posiciones"] * 100).round(1)
    return r[columnas]
