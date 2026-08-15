"""Test de inventario/normalizador.py contra un .xlsx real (no monkeypatch).

Va en integration/ porque el defecto que cubre solo se manifiesta a través de
la inferencia de tipos real de `pd.read_excel` sobre un archivo en disco — un
DataFrame construido a mano en memoria ya tiene el dtype que uno le da, así
que no puede reproducir el bug.

Encontrado el 2026-08-10: 16 filas de `admin_inventario.xlsx` (referencia
PS12) llegaron sin `ID de especificación`. Sin dtype forzado, pandas no puede
mezclar int64 con NaN y sube la columna ENTERA a float64 — los IDs de 19
dígitos pierden precisión y se vuelven notación científica
('2.085043262381486e+18'), lo que hizo que el cruce con Bochica por
`id_especificacion` matcheara CERO filas durante ~10 horas, en silencio
(sin excepción, sin WARNING, y sin que `datos_desactualizados` lo detectara
porque esa bandera solo mira antigüedad de archivo, no si el cruce encontró
algo).
"""

from pathlib import Path

import pandas as pd
import pytest

from inventario.normalizador import cargar_admin

pytestmark = pytest.mark.integration

# IDs reales de 19 dígitos — el rango donde float64 empieza a perder
# precisión (float64 solo representa enteros exactos hasta 2**53 ≈ 9×10^15).
_ID_LARGO_A = "2085043262381486081"
_ID_LARGO_B = "2085043262628950017"


def _escribir_admin_excel(path: Path, id_fila_2: str | None) -> None:
    df = pd.DataFrame(
        {
            "Identificación del producto": ["111", "222"],
            "ID de especificación": [_ID_LARGO_A, id_fila_2],
            "Nombre comercial": ["Producto A", "Producto B"],
            "Inventario": [10, 0],
            "Referencia del producto.": ["PA01", "PS12"],
            "ALMACEN": ["Bogotá", "Bogotá"],
            "Categoria del producto": ["Juguetes", "Aseo"],
            "Especificación": ["Color: Rojo;", "Talla: M;"],
            "Código de barras.": ["7700000000001", "7700000000002"],
            "Peso": [500, 1000],
            "Precio": [10000, 20000],
            "Existencias restantes.": [8, 0],
            "Producto activo o inactivo": ["Fue", "Fue"],
            "Descuento": ["0%", "10%"],
            "IVA": ["19%", "19%"],
        }
    )
    df.to_excel(path, index=False)


def test_cargar_admin_preserva_ids_de_19_digitos_sin_filas_vacias(tmp_path):
    ruta = tmp_path / "admin_inventario.xlsx"
    _escribir_admin_excel(ruta, _ID_LARGO_B)

    df = cargar_admin(ruta)

    assert df["id_especificacion"].tolist() == [_ID_LARGO_A, _ID_LARGO_B]


def test_cargar_admin_preserva_ids_de_19_digitos_con_una_fila_vacia(tmp_path):
    """El caso real del 2026-08-10: una fila sin ID no debe corromper las demás."""
    ruta = tmp_path / "admin_inventario.xlsx"
    _escribir_admin_excel(ruta, None)

    df = cargar_admin(ruta)

    # La fila vacía se cae a NaN -> "nan" (no matchea nada, correcto), pero
    # la fila CON id no puede perder ni un dígito ni volverse notación
    # científica.
    assert df["id_especificacion"].iloc[0] == _ID_LARGO_A
    assert "e+" not in df["id_especificacion"].iloc[0]
