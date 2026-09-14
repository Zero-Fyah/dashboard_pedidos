"""get_catalogo_productos() — puente id_especificacion -> id_producto/codigo_barras (DEC-045).

Cubre el hueco de cobertura encontrado en la auditoría del 2026-09-13: la
función se agregó el 2026-09-12 junto con el desglose por ID de
`dashboard/pages/inventario.py` sin ningún test.
"""

import sqlite3

import pytest

import dashboard.db as ddb


@pytest.fixture(autouse=True)
def _sin_cache():
    ddb.get_catalogo_productos.clear()
    yield


@pytest.mark.integration
def test_tabla_ausente_devuelve_dataframe_vacio(monkeypatch, tmp_path):
    ruta = tmp_path / "sin_tabla.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE placeholder (x INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_catalogo_productos()

    assert df.empty


@pytest.mark.integration
def test_excluye_filas_con_id_especificacion_nulo(monkeypatch, tmp_path):
    ruta = tmp_path / "catalogo.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE catalogo_productos (
            id_especificacion TEXT, id_producto TEXT, codigo_barras TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO catalogo_productos VALUES ('E1', 'P1', '111')",
    )
    con.execute(
        "INSERT INTO catalogo_productos VALUES (NULL, 'P2', '222')",
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_catalogo_productos()

    assert list(df["id_especificacion"]) == ["E1"]
    assert df.iloc[0]["id_producto"] == "P1"
    assert df.iloc[0]["codigo_barras"] == "111"


@pytest.mark.integration
def test_lee_las_tres_columnas_esperadas(monkeypatch, tmp_path):
    ruta = tmp_path / "catalogo2.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE catalogo_productos (
            id_especificacion TEXT, id_producto TEXT, codigo_barras TEXT,
            columna_no_expuesta TEXT
        );
        """
    )
    con.execute("INSERT INTO catalogo_productos VALUES ('E9', 'P9', '999', 'ruido')")
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_catalogo_productos()

    assert set(df.columns) == {"id_especificacion", "id_producto", "codigo_barras"}
