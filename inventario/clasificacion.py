"""
clasificacion.py — ABC por consumo y matriz ABC-XYZ (DEC-050).

ABC ordena las referencias por cuánto ingreso generan; XYZ, por qué tan
predecible es su demanda. Cruzarlas da una política distinta para cada
combinación: una AX (mucho valor, demanda estable) se puede reponer
automáticamente, una CZ (poco valor, demanda errática) conviene revisarla
a mano o pedirla contra pedido.

**ABC se calcula con el ingreso real** (`monto_final_num` de
`lineas_pedido`, poblado al 100%), no con un proxy de demanda × precio de
catálogo: el monto que efectivamente se cobró ya está en la base.

Se calcula en el ciclo del scheduler y se persiste, igual que el resto del
paquete (DEC-043/049): recorre las mismas 840.000 líneas.
"""

import datetime as dt
import logging
import sqlite3

import numpy as np
import pandas as pd

from comun import familia_de

logger = logging.getLogger("inventario.clasificacion")

# Meses completos hacia atrás. El mes en curso se excluye siempre: está a
# medio transcurrir, así que subestimaría su consumo y metería una caída
# artificial en el coeficiente de variación de todas las referencias.
VENTANA_MESES = 6

# Cortes de Pareto sobre el porcentaje acumulado de ingreso.
UMBRAL_A_PCT = 80.0
UMBRAL_B_PCT = 95.0

# Cortes del coeficiente de variación de la demanda mensual.
CV_ESTABLE = 0.5
CV_VARIABLE = 1.0

SIN_CONSUMO = "Sin consumo"

# Política sugerida por celda. Es guía estándar de gestión de inventarios,
# no una regla que el sistema aplique: la decisión sigue siendo del
# Arquitecto.
POLITICAS = {
    "AX": "Reposición automática ajustada · stock de seguridad bajo · revisar seguido",
    "AY": "Reposición automática · stock de seguridad mayor por la variabilidad",
    "AZ": "Revisión manual frecuente · alto impacto y demanda impredecible",
    "BX": "Reposición periódica · parámetros estables",
    "BY": "Reposición periódica · revisar el stock de seguridad",
    "BZ": "Revisión manual · demanda errática",
    "CX": "Lote grande y poco frecuente · bajo esfuerzo de gestión",
    "CY": "Revisión manual espaciada",
    "CZ": "Contra pedido o mínimos · candidatas a depurar del catálogo",
}


def _meses_completos(hoy: dt.date, cantidad: int) -> list[str]:
    """Los N meses completos anteriores al mes en curso, como 'YYYY-MM'."""
    meses = []
    ancla = dt.date(hoy.year, hoy.month, 1)
    for _ in range(cantidad):
        ancla = (ancla - dt.timedelta(days=1)).replace(day=1)
        meses.append(ancla.strftime("%Y-%m"))
    return sorted(meses)


def calcular_clasificacion(
    df_admin: pd.DataFrame,
    con: sqlite3.Connection,
    *,
    almacen: str = "Bogotá",
    hoy: dt.date | None = None,
) -> pd.DataFrame:
    """Clasifica cada referencia en ABC, XYZ y su celda combinada.

    Args:
        df_admin: Catálogo del admin ya filtrado por alcance — se usa para
            acotar el universo al mismo de las otras vistas de inventario.
        con: Conexión de lectura a pedidos.db.
        almacen: Almacén cuyas ventas se consideran.
        hoy: Fecha de referencia (inyectable para tests).

    Returns:
        DataFrame por `referencia` con `valor_consumo`, `pct_acumulado`,
        `abc`, `unidades`, `cv`, `meses_con_venta`, `xyz`, `celda` y
        `politica`. Las referencias sin consumo en la ventana quedan con
        `abc = "Sin consumo"` y sin celda.
    """
    referencia_hoy = hoy or dt.date.today()
    meses = _meses_completos(referencia_hoy, VENTANA_MESES)

    lineas = pd.read_sql(
        """SELECT id_pedido, numero_subpedido, referencia, almacen,
                  cantidad_comprada, monto_final_num
           FROM lineas_pedido
           WHERE referencia IS NOT NULL AND referencia != ''""",
        con,
    )
    pedidos = pd.read_sql("SELECT id_pedido, fecha FROM pedidos", con)
    subpedidos = pd.read_sql("SELECT id_pedido, numero_subpedido, estado FROM subpedidos", con)

    mov = lineas.merge(pedidos, on="id_pedido").merge(
        subpedidos, on=["id_pedido", "numero_subpedido"]
    )
    # Un subpedido cancelado no es consumo: contarlo distorsionaría tanto el
    # ranking de valor como la variabilidad.
    mov = mov[(mov["almacen"] == almacen) & (mov["estado"].str.lower() != "cancelado")]
    mov["referencia"] = mov["referencia"].astype(str).str.strip()
    mov["mes"] = mov["fecha"].astype(str).str[:7]
    mov = mov[mov["mes"].isin(meses)]

    universo = sorted(set(df_admin["referencia"].astype(str).str.strip()))
    base = pd.DataFrame({"referencia": universo})

    consumo = mov.groupby("referencia", as_index=False).agg(
        valor_consumo=("monto_final_num", "sum"), unidades=("cantidad_comprada", "sum")
    )
    df = base.merge(consumo, on="referencia", how="left").fillna(
        {"valor_consumo": 0.0, "unidades": 0.0}
    )

    df = _clasificar_abc(df)
    df = df.merge(_calcular_xyz(mov, meses), on="referencia", how="left")

    con_consumo = df["abc"] != SIN_CONSUMO
    df["celda"] = np.where(
        con_consumo & df["xyz"].notna(), df["abc"].astype(str) + df["xyz"].astype(str), None
    )
    df["politica"] = df["celda"].map(POLITICAS)
    df["familia"] = df["referencia"].map(familia_de)

    logger.info(
        "calcular_clasificacion: %d referencias · ventana %s..%s · A=%d B=%d C=%d sin consumo=%d",
        len(df),
        meses[0],
        meses[-1],
        int((df["abc"] == "A").sum()),
        int((df["abc"] == "B").sum()),
        int((df["abc"] == "C").sum()),
        int((~con_consumo).sum()),
    )
    return df[
        [
            "referencia",
            "familia",
            "valor_consumo",
            "pct_valor",
            "pct_acumulado",
            "abc",
            "unidades",
            "cv",
            "meses_con_venta",
            "xyz",
            "celda",
            "politica",
        ]
    ].sort_values("valor_consumo", ascending=False)


def _clasificar_abc(df: pd.DataFrame) -> pd.DataFrame:
    """Asigna A/B/C por porcentaje acumulado de ingreso (Pareto).

    Las referencias sin consumo no se fuerzan a C: no es que aporten poco,
    es que no aportan nada, y mezclarlas diluiría la clase C.
    """
    df = df.sort_values("valor_consumo", ascending=False).reset_index(drop=True)
    total = df["valor_consumo"].sum()

    df["pct_valor"] = (df["valor_consumo"] / total * 100) if total else 0.0
    df["pct_acumulado"] = df["pct_valor"].cumsum()

    # El corte se evalúa sobre el acumulado ANTERIOR al ítem, no el
    # posterior: la referencia que cruza la línea del 80% sigue siendo A,
    # porque es parte del grupo que llega hasta ahí. Con el acumulado
    # posterior, la que cruza cae a B — y en el extremo, una referencia que
    # concentra el 100% del ingreso quedaba clasificada como C.
    previo = df["pct_acumulado"] - df["pct_valor"]

    df["abc"] = np.where(
        df["valor_consumo"] <= 0,
        SIN_CONSUMO,
        np.where(
            previo < UMBRAL_A_PCT,
            "A",
            np.where(previo < UMBRAL_B_PCT, "B", "C"),
        ),
    )
    # El acumulado solo tiene sentido dentro de lo que sí consume.
    df.loc[df["abc"] == SIN_CONSUMO, ["pct_valor", "pct_acumulado"]] = np.nan
    return df


def _calcular_xyz(mov: pd.DataFrame, meses: list[str]) -> pd.DataFrame:
    """Coeficiente de variación de la demanda mensual, y su clase X/Y/Z.

    Los meses sin venta cuentan como cero — es la práctica estándar y es lo
    correcto: una referencia que vende solo dos meses de seis tiene demanda
    intermitente, y eso es exactamente lo que XYZ debe capturar.

    Se expone `meses_con_venta` porque con una ventana de 6 meses un
    producto **nuevo** también sale con CV alto, y ahí la variabilidad no es
    del producto sino de que antes no existía. Sin esa columna, las dos
    situaciones son indistinguibles.
    """
    if mov.empty:
        return pd.DataFrame(columns=["referencia", "cv", "meses_con_venta", "xyz"])

    piv = (
        mov.pivot_table(
            index="referencia", columns="mes", values="cantidad_comprada", aggfunc="sum"
        )
        .reindex(columns=meses)
        .fillna(0)
    )
    media = piv.mean(axis=1)
    # ddof=0: se describe la variabilidad observada de estos meses, no se
    # estima la de una población mayor.
    cv = (piv.std(axis=1, ddof=0) / media.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

    xyz = pd.DataFrame(
        {
            "referencia": piv.index,
            "cv": cv.to_numpy(),
            "meses_con_venta": (piv > 0).sum(axis=1).to_numpy(),
        }
    )
    xyz["xyz"] = np.where(
        xyz["cv"].isna(),
        None,
        np.where(xyz["cv"] <= CV_ESTABLE, "X", np.where(xyz["cv"] <= CV_VARIABLE, "Y", "Z")),
    )
    return xyz
