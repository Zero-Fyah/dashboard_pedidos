"""`get_scorecard()` y su consistencia con las páginas de detalle (DEC-102).

**Este archivo existe por DEC-067.** Aquella auditoría encontró dos definiciones
del mismo indicador divergiendo en silencio: el scorecard contaba 155 quiebres
contra los 81 de la página de Salud, y dos criterios de "clase A/B" daban 1.817
contra 1.516 posiciones. Nadie lo notó hasta que alguien comparó a mano.

`get_scorecard()` recalcula en SQL cifras que las páginas de detalle ya
computan, por rendimiento —reusar sus funciones costaba 6,6 s en la portada—.
Esa decisión **reintroduce exactamente ese riesgo**, y estos tests son la
contrapartida: comparan las dos vías sobre la misma base sintética. Si alguien
cambia una definición y no la otra, la suite cae.
"""

import sqlite3

import pytest

import dashboard.db as ddb
from comun.entregas import A_TIEMPO, clasificar_por_dia, parsear_compromiso


def _base(tmp_path):
    ruta = tmp_path / "pedidos.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, metodo_entrega TEXT,
            forma_pago TEXT, nombre_empresa TEXT, vendedor TEXT, hora_entrega TEXT,
            despachador TEXT, hay_diferencia INTEGER DEFAULT 0, scraping_completo INTEGER
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE registro_operaciones (
            id INTEGER PRIMARY KEY, id_pedido TEXT, momento TEXT, usuario TEXT,
            tipo_usuario TEXT, accion TEXT, referencia TEXT
        );
        CREATE TABLE estadisticas_monto (
            id INTEGER PRIMARY KEY, id_pedido TEXT, concepto_base TEXT, monto_final_num REAL
        );
        CREATE TABLE timeline_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, paso INTEGER, titulo TEXT,
            fecha_hora TEXT, completado INTEGER
        );
        CREATE TABLE inventario_salud (
            referencia TEXT, disponible REAL, estado TEXT, familia TEXT,
            demanda_diaria REAL, valor_venta REAL
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT,
            referencia TEXT, precio_unitario_num REAL
        );
        """
    )
    con.commit()
    return ruta, con


def _pedido(con, pid, *, fecha="2026-08-01", hora_entrega="", diferencia=0, valor=None):
    con.execute(
        "INSERT INTO pedidos (id_pedido, fecha, metodo_entrega, forma_pago, nombre_empresa,"
        " vendedor, hora_entrega, hay_diferencia, scraping_completo)"
        " VALUES (?,?,'Ruta','Pago inmediato','ACME','ANA',?,?,1)",
        (pid, fecha, hora_entrega, diferencia),
    )
    con.execute(
        "INSERT INTO timeline_pedido (id_pedido, paso, titulo, fecha_hora, completado)"
        " VALUES (?,1,'Alistamiento',?,1)",
        (pid, fecha + " 08:00:00"),
    )
    if valor is not None:
        con.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto_base, monto_final_num)"
            " VALUES (?,'Total a pagar / Total final a pagar',?)",
            (pid, valor),
        )


def _sub(con, pid, estado):
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado) VALUES (?,'1',?)",
        (pid, estado),
    )


def _entrega(con, pid, momento):
    con.execute(
        "INSERT INTO registro_operaciones (id_pedido, momento, usuario, tipo_usuario,"
        " accion, referencia) VALUES (?,?,'sys','system','Entrega','')",
        (pid, momento),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    ruta, con = _base(tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", ruta)
    for fn in (
        ddb.get_scorecard,
        ddb.get_ciclo_pedidos,
        ddb.get_entregas,
        ddb.get_riesgo_por_referencia,
    ):
        fn.clear()
    yield con
    con.close()


# ── Consistencia con las páginas de detalle ────────────────────────────────────


@pytest.mark.integration
def test_pedidos_abiertos_coincide_con_la_pagina_de_ciclo(db):
    """La misma cifra por dos caminos: el del scorecard y el de `ciclo.py`."""
    _pedido(db, "A1", valor=1000.0)
    _sub(db, "A1", "pendiente de entrega")
    _pedido(db, "A2", valor=2000.0)
    _sub(db, "A2", "completado")
    _sub(db, "A2", "en inspección")
    _pedido(db, "C1", valor=9000.0)
    _sub(db, "C1", "completado")
    db.commit()

    tarjeta = ddb.get_scorecard()
    detalle = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert tarjeta["pedidos_abiertos"] == int(detalle["abierto"].sum()) == 2


@pytest.mark.integration
def test_valor_retenido_coincide_con_la_pagina_de_ciclo(db):
    _pedido(db, "A1", valor=1000.0)
    _sub(db, "A1", "pendiente de entrega")
    _pedido(db, "A2", valor=2500.0)
    _sub(db, "A2", "en inspección")
    _pedido(db, "C1", valor=9000.0)
    _sub(db, "C1", "completado")
    db.commit()

    tarjeta = ddb.get_scorecard()
    detalle = ddb.get_ciclo_pedidos("2026-01-01", "2026-12-31")

    assert tarjeta["valor_retenido"] == detalle[detalle["abierto"] == 1]["valor"].sum() == 3500.0


@pytest.mark.integration
def test_on_time_coincide_con_comun_entregas(db):
    """El SQL del scorecard es la traducción literal de `clasificar_por_dia()`.

    Se cubren los dos formatos fechados y los dos lados del corte: entregado el
    día pactado (a tiempo) y al día siguiente (tarde).
    """
    casos = [
        ("P1", "2026-08-01 08:00 ~ 09:00", "2026-08-01 23:00:00"),  # franja, a tiempo
        ("P2", "2026-08-01 08:00 ~ 09:00", "2026-08-02 07:00:00"),  # franja, tarde
        ("P3", "2026-03-05 14:00", "2026-03-05 09:00:00"),  # punto, a tiempo
        ("P4", "2026-03-05 14:00", "2026-03-08 09:00:00"),  # punto, tarde
    ]
    for pid, compromiso, entregado in casos:
        _pedido(db, pid, hora_entrega=compromiso)
        _sub(db, pid, "completado")
        _entrega(db, pid, entregado)
    db.commit()

    tarjeta = ddb.get_scorecard()

    esperado = sum(
        1
        for _, compromiso, entregado in casos
        if clasificar_por_dia(parsear_compromiso(compromiso), entregado) == A_TIEMPO
    )
    assert tarjeta["entregas_medibles"] == 4
    assert round(tarjeta["on_time"] / 100 * 4) == esperado == 2


@pytest.mark.integration
def test_cualquier_hora_no_entra_en_el_on_time(db):
    """No es un compromiso fechado: contarlo como incumplido inventaría una promesa."""
    _pedido(db, "SIN", hora_entrega="Cualquier hora")
    _sub(db, "SIN", "completado")
    _entrega(db, "SIN", "2026-08-05 10:00:00")
    db.commit()

    assert ddb.get_scorecard()["entregas_medibles"] == 0


@pytest.mark.integration
def test_fill_rate_usa_hay_diferencia(db):
    _pedido(db, "OK1")
    _pedido(db, "OK2")
    _pedido(db, "CORTO", diferencia=1)
    db.commit()

    assert ddb.get_scorecard()["fill_rate"] == pytest.approx(200 / 3)


# ── Ausencia de datos: None, nunca cero ────────────────────────────────────────


@pytest.mark.integration
def test_sin_entregas_medibles_el_on_time_es_none_no_cero(db):
    """Cero por ciento y «no medible» son cosas distintas en una portada."""
    _pedido(db, "P1")
    _sub(db, "P1", "completado")
    db.commit()

    assert ddb.get_scorecard()["on_time"] is None


@pytest.mark.integration
def test_base_vacia_no_rompe(db):
    tarjeta = ddb.get_scorecard()

    assert tarjeta["pedidos_abiertos"] == 0
    assert tarjeta["fill_rate"] is None


# ── Riesgo económico de las alertas ────────────────────────────────────────────


@pytest.mark.integration
def test_el_riesgo_usa_el_precio_de_venta_no_el_valor_del_stock(db):
    """Una referencia en quiebre tiene `valor_venta = 0`: el precio sale de las líneas."""
    db.execute("INSERT INTO inventario_salud VALUES ('PS13', 0.0, 'Quiebre', 'PS', 20.0, 0.0)")
    for precio in (100.0, 200.0):
        db.execute(
            "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia,"
            " precio_unitario_num) VALUES ('P1','1','PS13',?)",
            (precio,),
        )
    db.commit()

    df = ddb.get_riesgo_por_referencia()

    assert len(df) == 1
    assert df.iloc[0]["precio_medio"] == 150.0
    assert df.iloc[0]["riesgo_diario"] == 3000.0


@pytest.mark.integration
def test_los_precios_en_cero_no_arrastran_la_media(db):
    """Hay líneas con precio 0 (promociones, ajustes): incluirlas subestimaría el riesgo."""
    db.execute("INSERT INTO inventario_salud VALUES ('PX', 0.0, 'Quiebre', 'PX', 10.0, 0.0)")
    for precio in (0.0, 100.0):
        db.execute(
            "INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia,"
            " precio_unitario_num) VALUES ('P1','1','PX',?)",
            (precio,),
        )
    db.commit()

    assert ddb.get_riesgo_por_referencia().iloc[0]["precio_medio"] == 100.0
