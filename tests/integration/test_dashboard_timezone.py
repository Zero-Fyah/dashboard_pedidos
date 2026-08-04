"""AUD-B6: la antigüedad en días no puede mezclar zonas horarias (DEC-103).

`pedidos.fecha` es la fecha que muestra la SPA —hora local de Colombia, UTC−5,
sin TZ explícita— mientras que `DATE('now')` en SQLite es UTC. Un pedido creado
"hoy" en Colombia mostraba 1 día de antigüedad en vez de 0 durante la ventana
diaria de 5 h (00:00–05:00 UTC) en que la fecha UTC ya cambió y la de Colombia
todavía no.

Colombia no tiene horario de verano desde 1993, así que un offset fijo de −5 h
es correcto todo el año y el test no necesita simular una hora: construye "hoy
en Colombia" con el mismo cálculo que usa el SQL y verifica que dé 0 sin
importar cuándo corra.

**Este archivo cambió de sujeto, no de propósito.** Probaba
`get_pedidos_activos()`, que DEC-103 retiró con el resto del código muerto — la
página de ciclo de vida (DEC-096) responde mejor la misma pregunta, desde el
timeline. El offset sobrevive en `get_pedidos_impagos()` y
`get_credito_abierto()`, así que la cobertura se mudó ahí en vez de irse con la
función.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dashboard.db as ddb


def _base(tmp_path, fecha):
    """Un pedido vivo, de pago inmediato y con saldo pendiente."""
    ruta = tmp_path / "tz.db"
    con = sqlite3.connect(ruta)
    con.executescript(
        """
        CREATE TABLE pedidos (
            id_pedido TEXT PRIMARY KEY, fecha TEXT, nombre_empresa TEXT, nit TEXT,
            forma_pago TEXT, scraping_completo INTEGER,
            pago_estado TEXT, pago_total_num REAL, pago_pagado_num REAL,
            pago_saldo_num REAL
        );
        CREATE TABLE subpedidos (
            id INTEGER PRIMARY KEY, id_pedido TEXT, numero_subpedido TEXT, estado TEXT
        );
        CREATE TABLE estadisticas_monto (
            id INTEGER PRIMARY KEY, id_pedido TEXT, concepto_base TEXT,
            monto_pagar_num REAL, monto_final_num REAL
        );
        CREATE TABLE lineas_pedido (
            id INTEGER PRIMARY KEY, id_pedido TEXT, referencia TEXT, cantidad_comprada REAL
        );
        """
    )
    con.execute(
        "INSERT INTO pedidos (id_pedido, fecha, nombre_empresa, nit, forma_pago,"
        " scraping_completo) VALUES ('TZ-001', ?, 'ACME', '900', 'Pago inmediato', 1)",
        (fecha,),
    )
    con.execute(
        "INSERT INTO subpedidos (id_pedido, numero_subpedido, estado)"
        " VALUES ('TZ-001', '1', 'pendiente de entrega')"
    )
    for concepto, pagar, final in (
        ("Total a pagar / Total final a pagar", 5000.0, 5000.0),
        ("Monto pagado", 0.0, 0.0),
    ):
        con.execute(
            "INSERT INTO estadisticas_monto (id_pedido, concepto_base, monto_pagar_num,"
            " monto_final_num) VALUES ('TZ-001',?,?,?)",
            (concepto, pagar, final),
        )
    con.commit()
    con.close()
    return ruta


def _dias_de(monkeypatch, tmp_path, fecha):
    monkeypatch.setattr(ddb, "DB_PATH", _base(tmp_path, fecha))
    monkeypatch.setattr(ddb, "_num_cols_exist", lambda: True)
    ddb.get_pedidos_impagos.clear()
    df = ddb.get_pedidos_impagos()
    assert len(df) == 1, "el pedido de prueba tiene que aparecer como impago"
    return int(df.iloc[0]["dias"])


@pytest.mark.integration
def test_un_pedido_de_hoy_en_colombia_tiene_cero_dias(monkeypatch, tmp_path):
    """El caso exacto de AUD-B6: con `DATE('now')` en UTC daría 1 de madrugada."""
    hoy_co = (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()

    assert _dias_de(monkeypatch, tmp_path, hoy_co) == 0


@pytest.mark.integration
def test_un_pedido_de_ayer_en_colombia_tiene_un_dia(monkeypatch, tmp_path):
    """Control: si el offset se aplicara de más, este daría 0 y el test no serviría."""
    ayer_co = (datetime.now(timezone.utc) - timedelta(hours=5, days=1)).date().isoformat()

    assert _dias_de(monkeypatch, tmp_path, ayer_co) == 1


@pytest.mark.integration
def test_el_offset_esta_declarado_una_sola_vez():
    """`_HOY_CO` existe para que el offset no se reescriba a mano en cada consulta.

    No es un test de comportamiento sino de forma, y vale la pena: AUD-B6 nació
    justamente de tener la fecha calculada de dos maneras en el mismo archivo.
    """
    assert ddb._HOY_CO == "DATE('now', '-5 hours')"
