"""
integraciones/sheets_cliente.py — escribe a Google Sheets con una cuenta de
servicio (pedido del Arquitecto, 2026-09-14).

Reemplaza el contenido completo de cada hoja en cada corrida — no es un
log histórico, es la foto de ahora. La hora de captura queda en la
primera fila, antes del encabezado, para que nunca se pueda leer la tabla
sin saber a qué momento corresponde.

Solo scope de Sheets (no Drive): las hojas se abren por ID, ya compartidas
manualmente con la cuenta de servicio — no hace falta buscarlas por nombre
ni crear archivos nuevos, así que no se pidió la Drive API.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv
from gspread.utils import rowcol_to_a1

from integraciones.sheets_arena import (
    inventario_disponible_arena,
    pedidos_previos_picking_arena,
)

load_dotenv()

_ALCANCES = ["https://www.googleapis.com/auth/spreadsheets"]
_OFFSET_CO_H = 5
DB_PATH = Path(__file__).parent.parent / "data" / "pedidos.db"


def _hoy_colombia_str() -> str:
    ahora = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=_OFFSET_CO_H)
    return ahora.strftime("%Y-%m-%d %H:%M:%S")


def _cliente() -> gspread.Client:
    ruta = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
    return gspread.service_account(filename=ruta, scopes=_ALCANCES)


def escribir_hoja(gc: gspread.Client, id_hoja: str, df: pd.DataFrame) -> int:
    """Reemplaza la primera pestaña de la hoja `id_hoja` con `df`.

    Fila 1: marca de tiempo de captura (hora Colombia). Fila 2: encabezados
    (nombres de columna de `df`). Desde la fila 3: los datos. NaN se
    convierte a cadena vacía — la API de Sheets no acepta `NaN` como valor.
    Lo que sobre de la escritura anterior (filas o columnas) se limpia
    después de escribir, nunca antes.

    Args:
        gc: Cliente ya autenticado (`_cliente()`).
        id_hoja: ID de la hoja de cálculo (el segmento largo de su URL).
        df: Datos a escribir — se usa tal cual, con sus nombres de columna
            actuales como encabezado.

    Returns:
        Cantidad de filas de datos escritas (sin contar timestamp/encabezado).
    """
    hoja = gc.open_by_key(id_hoja).sheet1
    marca = [f"Actualizado: {_hoy_colombia_str()} (hora Colombia, UTC-5)"]
    encabezado = df.columns.tolist()
    datos = df.where(df.notna(), "").values.tolist()
    valores = [marca, encabezado, *datos]
    # Auditoría 2026-09-25: se escribe PRIMERO y después se limpia solo lo
    # que sobra de la foto anterior. Antes era clear() + update(): si la
    # escritura fallaba (la semana del 2026-09-19 hubo un corte de conexión
    # con Google), la hoja quedaba vacía hasta el ciclo siguiente. Ahora un
    # fallo deja la foto anterior, que declara su propia hora en la fila 1.
    hoja.update(valores, "A1", value_input_option="USER_ENTERED")
    filas, columnas = len(valores), max(len(encabezado), 1)
    sobrantes = []
    if hoja.row_count > filas:
        sobrantes.append(f"A{filas + 1}:{rowcol_to_a1(hoja.row_count, hoja.col_count)}")
    if hoja.col_count > columnas:
        sobrantes.append(f"{rowcol_to_a1(1, columnas + 1)}:{rowcol_to_a1(filas, hoja.col_count)}")
    if sobrantes:
        hoja.batch_clear(sobrantes)
    return len(datos)


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        inventario = inventario_disponible_arena(con)
        pedidos = pedidos_previos_picking_arena(con)
    finally:
        con.close()

    gc = _cliente()
    n_inv = escribir_hoja(gc, os.environ["SHEETS_ID_INVENTARIO_ARENA"], inventario)
    n_ped = escribir_hoja(gc, os.environ["SHEETS_ID_PEDIDOS_PREVIOS_PICKING"], pedidos)
    print(
        f"Sheets actualizados — inventario: {n_inv} filas · "
        f"pedidos previos a picking: {n_ped} filas."
    )


if __name__ == "__main__":
    main()
