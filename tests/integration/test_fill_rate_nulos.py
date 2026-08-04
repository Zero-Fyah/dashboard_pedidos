"""FIX C-4 migrado: el fill rate frente a `cantidad_entregada` NULL (DEC-103).

La auditoría de 2026-07-01 encontró que `x - NULL = NULL` y que `SUM()` ignora
la fila entera, así que el KPI "Pendiente" **subcontaba exactamente las líneas
pendientes**. El arreglo fue un `COALESCE` en `v_inventario_comprometido`, y
`test_etl.py` lo protegía.

DEC-103 retiró esa VIEW por semántica inválida (DEC-098). **La preocupación no
se fue con ella**: el mismo patrón —restar `cantidad_entregada` a
`cantidad_comprada`— vive hoy en las consultas del fill rate. Perder el test al
retirar la VIEW habría dejado la clase de bug sin cobertura.

**Medido antes de escribir esto:** hoy `cantidad_entregada` no es NULL en
ninguna de las 881.349 líneas, así que nada de esto está fallando. La columna
admite nulos y el origen ya emitió NULL antes; estos tests fijan el
comportamiento para cuando vuelva a pasar.
"""

import sqlite3

import pytest

import dashboard.db as ddb


def _base(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, scraping_completo INTEGER,
            hay_diferencia INTEGER DEFAULT 0
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            nombre_producto TEXT, referencia TEXT, codigo_barras TEXT, almacen TEXT,
            cantidad_comprada REAL, cantidad_entregada REAL
        );
        """
    )
    con.executescript(
        """
        INSERT INTO pedidos VALUES ('P1', '2026-06-01', 1, 0);
        INSERT INTO subpedidos (id_pedido, numero_subpedido, estado)
             VALUES ('P1', 'S1', 'completado');
        """
    )
    con.commit()
    return ruta, con


def _linea(con, ref, comprada, entregada):
    con.execute(
        "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, nombre_producto,"
        " referencia, codigo_barras, almacen, cantidad_comprada, cantidad_entregada)"
        " VALUES ('P1','S1','Prod',?,'770','BOD',?,?)",
        (ref, comprada, entregada),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    ruta, con = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    ddb.get_despacho_faltantes.clear()
    ddb.get_despacho_lineas_diario.clear()
    yield con
    con.close()


@pytest.mark.integration
def test_una_linea_con_entregada_null_no_desaparece_del_faltante(db):
    """El caso que FIX C-4 documentó, en su nueva casa.

    `get_despacho_faltantes` filtra por `cantidad_entregada < cantidad_comprada`.
    Con NULL esa comparación da NULL —ni true ni false— y SQLite descarta la
    fila: una línea de la que no se entregó nada quedaría fuera del listado de
    faltantes, que es el peor lugar donde perderla.
    """
    _linea(db, "REF-NULL", comprada=10.0, entregada=None)
    _linea(db, "REF-OK", comprada=10.0, entregada=4.0)
    db.commit()

    df = ddb.get_despacho_faltantes("2026-01-01", "2026-12-31")

    refs = set(df["referencia"])
    assert "REF-OK" in refs, "la línea con faltante parcial tiene que estar"
    assert "REF-NULL" in refs, (
        "una línea con cantidad_entregada NULL desapareció del faltante: "
        "`NULL < 10` no es true, y la fila se descarta en silencio"
    )


@pytest.mark.integration
def test_el_faltante_de_una_linea_null_es_la_cantidad_completa(db):
    """Si no se entregó nada, falta todo: `10 - NULL` no puede dar NULL."""
    _linea(db, "REF-NULL", comprada=10.0, entregada=None)
    db.commit()

    df = ddb.get_despacho_faltantes("2026-01-01", "2026-12-31")

    assert float(df[df["referencia"] == "REF-NULL"]["faltante"].iloc[0]) == 10.0


@pytest.mark.integration
def test_las_unidades_faltantes_del_diario_no_ignoran_los_nulos(db):
    """Mismo patrón en el agregado por día: `SUM(x - NULL)` perdería la fila."""
    _linea(db, "REF-NULL", comprada=10.0, entregada=None)
    _linea(db, "REF-OK", comprada=10.0, entregada=6.0)
    db.commit()

    df = ddb.get_despacho_lineas_diario("2026-01-01", "2026-12-31")

    assert len(df) == 1
    assert float(df.iloc[0]["unidades_faltantes"]) == 14.0, "10 (todo) + 4 (parcial)"


@pytest.mark.integration
def test_una_linea_completa_no_cuenta_como_faltante(db):
    """Control: sin el filtro correcto, todo sería faltante y el KPI daría 0%."""
    _linea(db, "REF-COMPLETA", comprada=10.0, entregada=10.0)
    db.commit()

    assert ddb.get_despacho_faltantes("2026-01-01", "2026-12-31").empty


@pytest.mark.integration
def test_el_conteo_de_lineas_con_faltante_tampoco_ignora_los_nulos(db):
    """Variante del mismo bug en el `CASE WHEN`: NULL cae al ELSE y no se cuenta.

    Es más silencioso que el anterior —no desaparece una fila, solo baja un
    contador— y por eso el fill rate por línea saldría **mejor** de lo real.
    """
    _linea(db, "REF-NULL", comprada=10.0, entregada=None)
    _linea(db, "REF-OK", comprada=10.0, entregada=10.0)
    db.commit()

    df = ddb.get_despacho_lineas_diario("2026-01-01", "2026-12-31")

    assert int(df.iloc[0]["lineas"]) == 2
    assert int(df.iloc[0]["lineas_con_faltante"]) == 1, (
        "la línea con entregada NULL no se contó como faltante: el fill rate "
        "por línea saldría mejor de lo que es"
    )
