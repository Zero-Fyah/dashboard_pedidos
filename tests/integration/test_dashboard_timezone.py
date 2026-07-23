"""AUD-B6: "Días abierto"/"Días sin mov." mezclaban zonas horarias —
`pedidos.fecha` es la fecha que muestra la SPA (hora local Colombia,
UTC-5, sin TZ explícita) mientras `DATE('now')` en SQLite es UTC. Un
pedido creado "hoy" en Colombia podía mostrar "Días abierto"=1 en vez de
0 durante la ventana diaria de 5h (00:00-05:00 UTC) en que la fecha UTC
ya cambió de día pero la de Colombia todavía no.

Colombia no tiene horario de verano desde 1993 — un offset fijo de -5h
es correcto todo el año, así que el test no necesita simular una hora
específica: construye la fecha "hoy en Colombia" con el mismo cálculo
que usa el SQL (`datetime.now(utc) - 5h`) y verifica que el resultado
sea 0 sin importar el momento real en que corra el test.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dashboard.db as ddb
from scraper.db import init_db


async def _db_con_pedido_activo(tmp_path, fecha_pedido: str) -> str:
    db_file = str(tmp_path / "tz.db")
    await init_db(db_file)
    con = sqlite3.connect(db_file)
    con.execute(
        "INSERT INTO pedidos (id_pedido, fecha, scraping_completo) VALUES (?, ?, 1)",
        ("TZ-001", fecha_pedido),
    )
    con.execute(
        """INSERT INTO subpedidos
           (id_pedido, numero_subpedido, estado, cantidades_definitivas, estado_cambiado_en)
           VALUES (?, ?, ?, 0, ?)""",
        ("TZ-001", "SUB-1", "en alistamiento", datetime.now(timezone.utc).isoformat()),
    )
    con.execute(
        """INSERT INTO lineas_pedido (id_pedido, numero_subpedido, almacen)
           VALUES (?, ?, ?)""",
        ("TZ-001", "SUB-1", "Principal"),
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
async def test_dias_abierto_cero_para_pedido_de_hoy_colombia(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    hoy_co = (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()
    await _db_con_pedido_activo(tmp_path, hoy_co)
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "tz.db")

    df = ddb.get_pedidos_activos((), (), ())

    fila = df[df["ID Pedido"] == "TZ-001"].iloc[0]
    assert fila["Días abierto"] == 0
    ddb.st.cache_data.clear()
