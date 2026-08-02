"""
comun/reposicion.py — Punto de reorden y cuánto pedir (DEC-071).

El dashboard sabía **qué falta** (81 referencias en quiebre, 33 en riesgo)
pero no **cuándo pedir ni cuánto**, que es el salto de ver inventario a
gestionarlo. Este módulo cierra esa brecha con lo que ya está medido.

**El lead time no existe en los datos y no se inventa.** No hay tabla de
compras ni de proveedores en el pipeline (C.1/C.2 del plan siguen
bloqueados), así que el plazo de reposición entra **como parámetro** y toda
cifra que sale de acá está condicionada a él. La página lo pide explícito
en vez de esconder un default en el código: un punto de reorden calculado
con un plazo inventado es peor que no tenerlo, porque parece un dato.

**Lo que sí sale de la medición.** La variabilidad de la demanda —el
insumo del stock de seguridad— es el coeficiente de variación que
`clasificacion.py` ya calcula para el eje XYZ: 948 referencias lo tienen.
No hace falta estimarlo de nuevo ni suponerlo.

Fórmula, la estándar de gestión de inventarios:

    punto_reorden = demanda_diaria × lead_time + stock_seguridad
    stock_seguridad = Z × cv × demanda_diaria × √lead_time

`Z` sale del nivel de servicio por clase ABC: se protege más lo que más
pesa. La raíz del plazo es la convención habitual — la desviación de la
demanda acumulada sobre L días crece con √L, no con L.

**Por qué vive en `comun/` y no en `inventario/`.** El cálculo depende de
un parámetro que el usuario mueve en pantalla (el plazo), así que no se
puede precalcular en el scheduler y publicar como VIEW — el patrón normal
de este proyecto (DEC-043). Y el dashboard **no importa `inventario/`**:
nunca lo hizo, y DEC-022 lo prohíbe explícitamente. `comun/` es la única
dependencia que las tres etapas tienen permitido compartir, así que la
función pura vive acá y la consumen la página y los tests.

Es el primer submódulo de `comun/` que depende de pandas. `import comun`
sigue siendo solo stdlib: este módulo se importa aparte.
"""

import logging
import math

import pandas as pd

from comun import NIVEL_SERVICIO_Z, Z_SERVICIO_DEFECTO

logger = logging.getLogger("comun.reposicion")

# Sin `cv` medido no se puede estimar la variabilidad. Se usa un valor
# conservador en vez de asumir demanda perfectamente estable (cv=0), que
# daría stock de seguridad cero justo en las referencias de las que menos
# se sabe. 0,5 es el cv mediano del catálogo con datos.
CV_DEFECTO = 0.5

_COLUMNAS = [
    "referencia",
    "clase",
    "demanda_diaria",
    "cv",
    "disponible",
    "stock_seguridad",
    "punto_reorden",
    "cobertura_dias",
    "estado_reposicion",
    "sugerido_pedir",
]

ESTADO_PEDIR_YA = "Pedir ya"
ESTADO_PEDIR_PRONTO = "Por debajo del punto"
ESTADO_OK = "Suficiente"


def calcular_reposicion(
    salud: pd.DataFrame,
    abc: pd.DataFrame,
    *,
    lead_time_dias: int,
    dias_cobertura_objetivo: int,
) -> pd.DataFrame:
    """Punto de reorden y cantidad sugerida por referencia.

    Solo se calcula para referencias **con demanda**: un punto de reorden
    sobre demanda cero es cero, y llenaría la tabla de filas que no piden
    ninguna decisión.

    Args:
        salud: Resultado de `salud.calcular_salud()`, ya filtrado a las
            referencias vigentes por el consumidor.
        abc: Resultado de `clasificacion.calcular_clasificacion()`, de
            donde salen la clase y el `cv` de la demanda.
        lead_time_dias: Plazo de reposición en días. **Es un parámetro del
            negocio, no un dato medido** — ver el docstring del módulo.
        dias_cobertura_objetivo: Cuántos días de venta se quiere tener
            encima al reponer. Define la cantidad sugerida.

    Returns:
        Una fila por referencia con demanda, ordenada por urgencia.
    """
    if salud.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    df = salud[["referencia", "demanda_diaria", "disponible"]].copy()
    df["demanda_diaria"] = pd.to_numeric(df["demanda_diaria"], errors="coerce").fillna(0.0)
    df["disponible"] = pd.to_numeric(df["disponible"], errors="coerce").fillna(0.0)
    df = df[df["demanda_diaria"] > 0]
    if df.empty:
        return pd.DataFrame(columns=_COLUMNAS)

    if not abc.empty and "nivel" in abc.columns:
        refs = abc[abc["nivel"] == "referencia"][["clave", "abc", "cv"]].rename(
            columns={"clave": "referencia", "abc": "clase"}
        )
        df = df.merge(refs.drop_duplicates("referencia"), on="referencia", how="left")
    else:
        df["clase"] = None
        df["cv"] = None

    df["cv"] = pd.to_numeric(df["cv"], errors="coerce").fillna(CV_DEFECTO)
    df["_z"] = df["clase"].map(NIVEL_SERVICIO_Z).fillna(Z_SERVICIO_DEFECTO)

    raiz_lt = math.sqrt(max(lead_time_dias, 0))
    df["stock_seguridad"] = df["_z"] * df["cv"] * df["demanda_diaria"] * raiz_lt
    df["punto_reorden"] = df["demanda_diaria"] * lead_time_dias + df["stock_seguridad"]
    df["cobertura_dias"] = df["disponible"] / df["demanda_diaria"]

    # Cuánto pedir: llevar el stock al objetivo más lo que se consume
    # durante el plazo. Nunca negativo — si ya sobra, no se pide.
    objetivo = df["demanda_diaria"] * (lead_time_dias + dias_cobertura_objetivo)
    df["sugerido_pedir"] = (objetivo + df["stock_seguridad"] - df["disponible"]).clip(lower=0)

    df["estado_reposicion"] = ESTADO_OK
    df.loc[df["disponible"] <= df["punto_reorden"], "estado_reposicion"] = ESTADO_PEDIR_PRONTO
    # Sin stock y con demanda: ya se está perdiendo venta, no es "pronto".
    df.loc[df["disponible"] <= 0, "estado_reposicion"] = ESTADO_PEDIR_YA

    df = df.sort_values(["estado_reposicion", "sugerido_pedir"], ascending=[True, False])

    logger.info(
        "calcular_reposicion: %d referencias con demanda · lead time %d d · "
        "%d para pedir ya · %d bajo el punto de reorden",
        len(df),
        lead_time_dias,
        int((df["estado_reposicion"] == ESTADO_PEDIR_YA).sum()),
        int((df["estado_reposicion"] == ESTADO_PEDIR_PRONTO).sum()),
    )
    return df[_COLUMNAS]
