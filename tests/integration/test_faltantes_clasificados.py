"""get_faltantes_clasificados() — clasificación de faltantes (DEC-115/DEC-117).

Cubre los 3 grupos (Interno/Real/Monetario), el caso "mezcla" (un subpedido
con línea de relleno Y línea real con diferencia — Real gana), y el caso
"Monetario" con más de un subpedido, donde el origen no permite atribuir a
uno específico.
"""

import sqlite3

import pandas as pd
import pytest

import dashboard.db as ddb


@pytest.fixture(autouse=True)
def _sin_cache():
    """`get_faltantes_clasificados` tiene `@st.cache_data`: sin esto, un test
    reutilizaría el resultado cacheado del anterior aunque DB_PATH cambie."""
    ddb.get_faltantes_clasificados.clear()
    yield


def _base(tmp_path):
    ruta = tmp_path / "faltantes.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY, fecha TEXT, hay_diferencia INTEGER);
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            nombre_producto TEXT, cantidad_comprada REAL, cantidad_entregada REAL
        );
        CREATE TABLE gestion_diferencias (
            id INTEGER PRIMARY KEY, id_pedido TEXT, monto_diferencia TEXT
        );
        """
    )
    con.commit()
    con.close()
    return ruta


def _pedido(con, id_pedido, fecha, monto_diferencia, subpedidos):
    """`subpedidos`: lista de (numero_subpedido, estado, [(nombre_producto, comprada, entregada)])."""
    con.execute(
        "INSERT INTO pedidos (id_pedido, fecha, hay_diferencia) VALUES (?, ?, 1)",
        (id_pedido, fecha),
    )
    con.execute(
        "INSERT INTO gestion_diferencias (id_pedido, monto_diferencia) VALUES (?, ?)",
        (id_pedido, monto_diferencia),
    )
    for numero_subpedido, estado, lineas in subpedidos:
        con.execute(
            "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES (?, ?, ?)",
            (id_pedido, numero_subpedido, estado),
        )
        for nombre_producto, comprada, entregada in lineas:
            con.execute(
                "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, nombre_producto,"
                " cantidad_comprada, cantidad_entregada) VALUES (?, ?, ?, ?, ?)",
                (id_pedido, numero_subpedido, nombre_producto, comprada, entregada),
            )


@pytest.mark.integration
def test_grupo_real_por_faltante_de_unidades(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-REAL",
        "2026-08-01",
        "COP 30.000",
        [("S1", "Completado", [("Comedero PC85", 2, 1)])],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    fila = df[df["id_pedido"] == "TEST-REAL"].iloc[0]
    assert fila["grupo"] == "Real"
    assert fila["numero_subpedido"] == "S1"
    assert fila["unidades_faltantes"] == 1
    assert fila["atribuible"]


@pytest.mark.integration
def test_grupo_interno_sin_faltante_real(monkeypatch, tmp_path):
    """Interno Acc con comprada=1/entregada=0, pero las líneas reales coinciden — no es faltante."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-INTERNO",
        "2026-08-01",
        "COP 500.000",
        [
            (
                "S1",
                "Completado",
                [("Interno Acc", 1, 0), ("Comedero PC85", 2, 2)],
            )
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    fila = df[df["id_pedido"] == "TEST-INTERNO"].iloc[0]
    assert fila["grupo"] == "Interno"
    assert fila["unidades_faltantes"] == 0


@pytest.mark.integration
def test_mezcla_interno_y_real_gana_real(monkeypatch, tmp_path):
    """Un subpedido con línea Interno Y una línea real con diferencia clasifica como Real."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-MEZCLA",
        "2026-08-01",
        "COP 550.000",
        [
            (
                "S1",
                "Completado",
                [("Interno Acc", 1, 0), ("Comedero PC85", 2, 1)],
            )
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    fila = df[df["id_pedido"] == "TEST-MEZCLA"].iloc[0]
    assert fila["grupo"] == "Real"
    assert fila["unidades_faltantes"] == 1


@pytest.mark.integration
def test_monetario_un_subpedido_es_atribuible(monkeypatch, tmp_path):
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-MONO-1",
        "2026-08-01",
        "COP 10.000",
        [("S1", "Completado", [("Comedero PC85", 2, 2)])],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    fila = df[df["id_pedido"] == "TEST-MONO-1"].iloc[0]
    assert fila["grupo"] == "Monetario"
    assert fila["numero_subpedido"] == "S1"
    assert fila["atribuible"]


@pytest.mark.integration
def test_monetario_varios_subpedidos_no_es_atribuible(monkeypatch, tmp_path):
    """El origen no identifica el subpedido — queda a nivel pedido (DEC-117)."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-MONO-2",
        "2026-08-01",
        "COP 10.000",
        [
            ("S1", "Completado", [("Comedero PC85", 2, 2)]),
            ("S2", "Completado", [("Pelota Chewing", 1, 1)]),
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    filas = df[df["id_pedido"] == "TEST-MONO-2"]
    assert len(filas) == 1
    fila = filas.iloc[0]
    assert fila["grupo"] == "Monetario"
    assert pd.isna(fila["numero_subpedido"])
    assert not fila["atribuible"]


@pytest.mark.integration
def test_cancelado_queda_marcado(monkeypatch, tmp_path):
    """El flag `cancelado` sale del subpedido en estado 'Cancelado' — el filtro por defecto
    de la página vive ahí, no en esta función."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    _pedido(
        con,
        "TEST-CANCEL",
        "2026-08-01",
        "COP 30.000",
        [("S1", "Cancelado", [("Comedero PC85", 2, 1)])],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    fila = df[df["id_pedido"] == "TEST-CANCEL"].iloc[0]
    assert fila["cancelado"]


@pytest.mark.integration
def test_pedido_sin_diferencia_no_aparece(monkeypatch, tmp_path):
    """`hay_diferencia=0` — sin fila en gestion_diferencias, fuera del universo."""
    ruta = _base(tmp_path)
    con = sqlite3.connect(ruta)
    con.execute(
        "INSERT INTO pedidos (id_pedido, fecha, hay_diferencia) VALUES ('TEST-SANO', '2026-08-01', 0)"
    )
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES ('TEST-SANO', 'S1', 'Completado')"
    )
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, nombre_producto,"
        " cantidad_comprada, cantidad_entregada) VALUES ('TEST-SANO', 'S1', 'Comedero PC85', 2, 2)"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    df = ddb.get_faltantes_clasificados()

    assert df.empty or "TEST-SANO" not in set(df["id_pedido"])
