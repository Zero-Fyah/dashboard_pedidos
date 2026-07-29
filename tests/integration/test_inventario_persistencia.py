"""Tests de inventario/persistencia.py — el contrato con el dashboard (DEC-043).

Van en integration/ porque ejercitan SQLite real: es justamente lo que se
quiere verificar (esquema, transacción, reemplazo del snapshot y tipos
que sqlite3 acepta), no algo que un mock pueda sustituir.

Incluye la cobertura de `calcular_vendido_no_alistado()`, la única pieza
de la fórmula que toca la DB y que la auditoría previa a la vista
encontró sin tests.
"""

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from inventario.comparacion import calcular_vendido_no_alistado
from inventario.persistencia import (
    _COLUMNAS_CONTEOS,
    _SNAPSHOTS_RECREABLES,
    UMBRAL_DESACTUALIZADO_H,
    init_schema,
    medir_frescura,
    persistir,
)

pytestmark = pytest.mark.integration


# Columnas del resultado de `comparar()` (DEC-041, ampliado en DEC-051).
_COLS_COMPARACION = [
    "referencia",
    "familia",
    "es_averia",
    "disponible_venta",
    "vendido_no_alistado",
    "inventario_teorico",
    "bochica_altura",
    "bochica_picking",
    "bochica_paso",
    "bochica_total",
    "diferencia",
    "sobrante_altura",
    "picking_estimado",
]


def _comparacion(filas=None):
    """Filas coherentes con la fórmula: total = altura + picking,
    diferencia = total − teórico, sobrante = altura − teórico."""
    return pd.DataFrame(
        filas
        or [
            # teórico 160 · altura 30 · picking 60 → total 90, dif −70, sobrante −130
            ("PA01", "PA", False, 150.0, 10.0, 160.0, 30.0, 60.0, 11.0, 90.0, -70.0, -130.0, 130.0),
            # teórico 225 · altura 90 · picking 300 → total 390, dif 165, sobrante −135
            (
                "PJ91",
                "PJ",
                False,
                200.0,
                25.0,
                225.0,
                90.0,
                300.0,
                0.0,
                390.0,
                165.0,
                -135.0,
                135.0,
            ),
        ],
        columns=_COLS_COMPARACION,
    )


def _anomalias(filas=None):
    return pd.DataFrame(
        filas or [("paso_montacarga", "B_25_2", "1001", 7700.0)],
        columns=["motivo", "ubicacion", "id_especificacion", "cantidad"],
    )


_FRESCURA_OK = {
    "admin_actualizado_en": "2026-07-25T22:43:12+00:00",
    "bochica_actualizado_en": "2026-07-25T22:44:41+00:00",
    "layout_actualizado_en": "2026-07-25T21:29:00+00:00",
    "fuente_mas_vieja_h": 0.8,
    "datos_desactualizados": 0,
}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "pedidos.db")


# ─────────────────────────────────────────────
# Esquema y VIEWs — el contrato que lee el dashboard
# ─────────────────────────────────────────────


def test_init_schema_crea_tablas_y_views(db):
    con = sqlite3.connect(db)
    init_schema(con)
    nombres = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"inventario_comparacion", "inventario_anomalias", "inventario_corridas"} <= nombres
    assert {
        "v_inventario_comparacion",
        "v_inventario_anomalias",
        "v_inventario_corridas",
    } <= nombres
    con.close()


def test_init_schema_es_idempotente(db):
    """Corre en cada corrida del scheduler: no puede fallar la segunda vez."""
    con = sqlite3.connect(db)
    init_schema(con)
    init_schema(con)
    con.close()


def test_las_views_exponen_lo_que_consume_el_dashboard(db):
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    df = pd.read_sql("SELECT * FROM v_inventario_comparacion", con)
    # La VIEW es el contrato con el dashboard: si cambia una columna, este
    # test lo dice antes que la página.
    assert set(df.columns) == set(_COLS_COMPARACION)
    assert len(df) == 2
    con.close()


# ─────────────────────────────────────────────
# Snapshot: la corrida nueva reemplaza a la anterior
# ─────────────────────────────────────────────


def test_persistir_reemplaza_el_snapshot_anterior(db):
    """DEC-043: comparacion/anomalias guardan solo la última corrida."""
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    nueva = _comparacion(
        [("PC10", "PC", False, 5.0, 0.0, 5.0, 1.0, 2.0, 0.0, 3.0, -2.0, -4.0, 4.0)]
    )
    persistir(nueva, _anomalias(), _FRESCURA_OK, db_path=db)

    con = sqlite3.connect(db)
    refs = [r[0] for r in con.execute("SELECT referencia FROM inventario_comparacion")]
    assert refs == ["PC10"], "el snapshot viejo debió borrarse"
    con.close()


def test_corridas_acumula_historial(db):
    """inventario_corridas es la única que crece: es la columna vertebral temporal."""
    id1 = persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    id2 = persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    assert id2 > id1
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM inventario_corridas").fetchone()[0] == 2
    con.close()


def test_agregados_de_la_corrida_son_correctos(db):
    corrida_id = persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    fila = con.execute(
        """SELECT referencias, inventario_teorico, bochica_altura, picking_estimado,
                  referencias_negativas, unidades_negativas, anomalias_filas, anomalias_unidades
           FROM inventario_corridas WHERE id = ?""",
        (corrida_id,),
    ).fetchone()
    con.close()
    # teorico 160+225=385 · altura 30+90=120 · picking_estimado 130+135=265
    assert fila == (2, 385.0, 120.0, 265.0, 0, 0.0, 1, 7700.0)


def test_referencias_negativas_se_cuentan(db):
    """picking_estimado negativo es el hallazgo que la vista debe destacar."""
    con_negativo = _comparacion(
        [("PR11A", "PR", False, 100.0, 0.0, 100.0, 372.0, 5.0, 0.0, 377.0, 277.0, 272.0, -272.0)]
    )
    corrida_id = persistir(con_negativo, _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    fila = con.execute(
        "SELECT referencias_negativas, unidades_negativas FROM inventario_corridas WHERE id = ?",
        (corrida_id,),
    ).fetchone()
    con.close()
    assert fila == (1, -272.0)


def test_persistir_sin_anomalias_no_rompe(db):
    vacias = _anomalias().iloc[0:0]
    corrida_id = persistir(_comparacion(), vacias, _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM inventario_anomalias").fetchone()[0] == 0
    assert (
        con.execute(
            "SELECT anomalias_unidades FROM inventario_corridas WHERE id=?", (corrida_id,)
        ).fetchone()[0]
        == 0.0
    )
    con.close()


def test_tipos_de_pandas_se_convierten_para_sqlite(db):
    """numpy.bool_/int64 revientan con InterfaceError si no se normalizan."""
    df = _comparacion()
    df["es_averia"] = df["es_averia"].astype(bool)
    df["disponible_venta"] = df["disponible_venta"].astype("int64")
    persistir(df, _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    assert (
        con.execute(
            "SELECT COUNT(*) FROM inventario_comparacion WHERE es_averia IN (0,1)"
        ).fetchone()[0]
        == 2
    )
    con.close()


def test_nan_se_guarda_como_null(db):
    df = _comparacion()
    df.loc[0, "familia"] = None
    persistir(df, _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT COUNT(*) FROM inventario_comparacion WHERE familia IS NULL").fetchone()[
            0
        ]
        == 1
    )
    con.close()


# ─────────────────────────────────────────────
# Frescura (DEC-043)
# ─────────────────────────────────────────────


def _tocar(path, horas_atras, ahora):
    import os

    path.write_text("x", encoding="utf-8")
    ts = (ahora - dt.timedelta(hours=horas_atras)).timestamp()
    os.utime(path, (ts, ts))
    return path


def test_frescura_marca_ok_con_fuentes_recientes(tmp_path):
    ahora = dt.datetime.now(tz=dt.timezone.utc)
    admin = _tocar(tmp_path / "a.xlsx", 0.5, ahora)
    bochica = _tocar(tmp_path / "b.xlsx", 0.6, ahora)
    layout = _tocar(tmp_path / "l.xlsx", 1.0, ahora)
    f = medir_frescura(admin, bochica, layout, ahora=ahora)
    assert f["datos_desactualizados"] == 0
    assert f["fuente_mas_vieja_h"] == pytest.approx(0.6, abs=0.05)


def test_frescura_detecta_descarga_fallida(tmp_path):
    """Sin `&&` en el .bat, una descarga caída deja el Excel viejo en su sitio."""
    ahora = dt.datetime.now(tz=dt.timezone.utc)
    admin = _tocar(tmp_path / "a.xlsx", 0.5, ahora)
    bochica = _tocar(tmp_path / "b.xlsx", UMBRAL_DESACTUALIZADO_H + 2, ahora)
    layout = _tocar(tmp_path / "l.xlsx", 1.0, ahora)
    f = medir_frescura(admin, bochica, layout, ahora=ahora)
    assert f["datos_desactualizados"] == 1


def test_layout_viejo_no_marca_desactualizado(tmp_path):
    """El layout es manual: es normal que tenga semanas, no es una falla."""
    ahora = dt.datetime.now(tz=dt.timezone.utc)
    admin = _tocar(tmp_path / "a.xlsx", 0.5, ahora)
    bochica = _tocar(tmp_path / "b.xlsx", 0.6, ahora)
    layout = _tocar(tmp_path / "l.xlsx", 24 * 30, ahora)
    f = medir_frescura(admin, bochica, layout, ahora=ahora)
    assert f["datos_desactualizados"] == 0
    assert f["layout_actualizado_en"] is not None


def test_frescura_con_fuente_faltante(tmp_path):
    ahora = dt.datetime.now(tz=dt.timezone.utc)
    admin = _tocar(tmp_path / "a.xlsx", 0.5, ahora)
    f = medir_frescura(admin, tmp_path / "no_existe.xlsx", tmp_path / "tampoco.xlsx", ahora=ahora)
    assert f["bochica_actualizado_en"] is None
    assert f["datos_desactualizados"] == 1


def test_frescura_llega_a_la_view(db):
    persistir(
        _comparacion(), _anomalias(), {**_FRESCURA_OK, "datos_desactualizados": 1}, db_path=db
    )
    con = sqlite3.connect(db)
    fila = pd.read_sql("SELECT * FROM v_inventario_corridas", con).iloc[0]
    con.close()
    assert fila["datos_desactualizados"] == 1
    assert fila["bochica_actualizado_en"] == "2026-07-25T22:44:41+00:00"


# ─────────────────────────────────────────────
# calcular_vendido_no_alistado — hueco que encontró la auditoría
# ─────────────────────────────────────────────


def _db_con_pedidos(path, filas):
    """filas: (referencia, almacen, estado, inicio_inspeccion, cantidad_comprada)."""
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT,
           referencia TEXT, almacen TEXT, cantidad_comprada REAL)"""
    )
    con.execute(
        """CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT,
           estado TEXT, inicio_inspeccion TEXT)"""
    )
    for i, (ref, almacen, estado, inspeccion, cant) in enumerate(filas):
        con.execute(
            "INSERT INTO lineas_pedido VALUES (?,?,?,?,?)", (f"P{i}", f"S{i}", ref, almacen, cant)
        )
        con.execute(
            "INSERT INTO subpedidos VALUES (?,?,?,?)", (f"P{i}", f"S{i}", estado, inspeccion)
        )
    con.commit()
    con.close()
    return path


def test_vendido_no_alistado_solo_cuenta_alistamiento_sin_terminar(db):
    """HAL-013: inicio_inspeccion='-' marca que el alistamiento físico no terminó."""
    _db_con_pedidos(
        db,
        [
            ("PA01", "Bogotá", "Pendiente de entrega", "-", 10),
            ("PA01", "Bogotá", "Pendiente de entrega", "2026-07-20 10:00", 99),
        ],
    )
    df = calcular_vendido_no_alistado(db)
    assert df.set_index("referencia").loc["PA01", "vendido_no_alistado"] == 10


def test_vendido_no_alistado_filtra_por_almacen(db):
    """DEC-041: solo la bodega comparada (Bogotá = CEDI Mosquera)."""
    _db_con_pedidos(
        db,
        [
            ("PA01", "Bogotá", "Pendiente de entrega", "-", 10),
            ("PA01", "Medellin", "Pendiente de entrega", "-", 500),
        ],
    )
    df = calcular_vendido_no_alistado(db)
    assert df.set_index("referencia").loc["PA01", "vendido_no_alistado"] == 10


def test_vendido_no_alistado_ignora_estados_no_activos(db):
    _db_con_pedidos(
        db,
        [
            ("PA01", "Bogotá", "Pendiente de entrega", "-", 10),
            ("PA01", "Bogotá", "Completado", "-", 777),
        ],
    )
    df = calcular_vendido_no_alistado(db)
    assert df.set_index("referencia").loc["PA01", "vendido_no_alistado"] == 10


def test_vendido_no_alistado_agrupa_por_referencia(db):
    _db_con_pedidos(
        db,
        [
            ("PA01", "Bogotá", "Pendiente de entrega", "-", 10),
            ("PA01", "Bogotá", "En inspección", "-", 5),
            ("PJ91", "Bogotá", "Pendiente de entrega", "-", 3),
        ],
    )
    df = calcular_vendido_no_alistado(db).set_index("referencia")
    assert df.loc["PA01", "vendido_no_alistado"] == 15
    assert df.loc["PJ91", "vendido_no_alistado"] == 3


def test_vendido_no_alistado_descarta_referencias_vacias(db):
    _db_con_pedidos(
        db,
        [
            ("", "Bogotá", "Pendiente de entrega", "-", 10),
            (None, "Bogotá", "Pendiente de entrega", "-", 10),
            ("PA01", "Bogotá", "Pendiente de entrega", "-", 7),
        ],
    )
    df = calcular_vendido_no_alistado(db)
    assert list(df["referencia"]) == ["PA01"]


# ─────────────────────────────────────────────
# catalogo_productos — el puente producto↔pedido (DEC-045)
# ─────────────────────────────────────────────


def _admin_catalogo(filas):
    """filas: (id_especificacion, id_producto_admin, referencia, codigo_barras, nombre)."""
    return pd.DataFrame(
        filas,
        columns=[
            "id_especificacion",
            "id_producto_admin",
            "referencia",
            "codigo_barras",
            "nombre_comercial",
        ],
    )


def test_catalogo_conserva_pares_inequivocos():
    from inventario.persistencia import construir_catalogo_productos

    admin = _admin_catalogo(
        [
            ("E1", "P1", "PJ91", "7700001", "Peluche"),
            ("E2", "P1", "PJ91", "7700002", "Peluche"),
        ]
    )
    puente = construir_catalogo_productos(admin)
    assert len(puente) == 2
    assert set(puente["id_producto"]) == {"P1"}


def test_catalogo_colapsa_variantes_del_mismo_par():
    """Dos especificaciones bajo el mismo (ref, codigo) y mismo producto: una fila."""
    from inventario.persistencia import construir_catalogo_productos

    admin = _admin_catalogo(
        [
            ("E1", "P1", "PR13", "7700001", "Arena"),
            ("E2", "P1", "PR13", "7700001", "Arena"),
        ]
    )
    puente = construir_catalogo_productos(admin)
    assert len(puente) == 1, "un par no puede aparecer dos veces: duplicaría la línea"


def test_catalogo_descarta_pares_ambiguos():
    """El caso que corrompería Cantidad comprada: un par con dos id_producto."""
    from inventario.persistencia import construir_catalogo_productos

    admin = _admin_catalogo(
        [
            ("E1", "P1", "ARENA TONELADA", "7700001", "Arena"),
            ("E2", "P2", "ARENA TONELADA", "7700001", "Arena"),  # mismo par, otro producto
            ("E3", "P3", "PJ91", "7700009", "Peluche"),
        ]
    )
    puente = construir_catalogo_productos(admin)
    assert list(puente["referencia"]) == ["PJ91"], "el par ambiguo debe quedar fuera"


def test_catalogo_es_llave_unica_por_par(db):
    """La PK (referencia, codigo_barras) es la garantía de que el join no multiplica."""
    from inventario.persistencia import construir_catalogo_productos

    admin = _admin_catalogo(
        [
            ("E1", "P1", "PJ91", "7700001", "Peluche"),
            ("E2", "P1", "PJ91", "7700001", "Peluche"),
        ]
    )
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        catalogo=construir_catalogo_productos(admin),
    )
    con = sqlite3.connect(db)
    filas = con.execute("SELECT COUNT(*) FROM catalogo_productos").fetchone()[0]
    pares = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT referencia, codigo_barras FROM catalogo_productos)"
    ).fetchone()[0]
    con.close()
    assert filas == pares == 1


def test_persistir_sin_catalogo_no_borra_el_puente(db):
    """Si el cruce de inventario falla, la vista de Pedidos no debe quedarse sin IDs."""
    from inventario.persistencia import construir_catalogo_productos

    admin = _admin_catalogo([("E1", "P1", "PJ91", "7700001", "Peluche")])
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        catalogo=construir_catalogo_productos(admin),
    )
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)  # sin catalogo
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM catalogo_productos").fetchone()[0] == 1
    con.close()


# ─────────────────────────────────────────────
# Migración de esquema y tendencia (DEC-051/052)
# ─────────────────────────────────────────────


def test_columna_nueva_se_agrega_a_una_tabla_existente(db):
    """CREATE TABLE IF NOT EXISTS no toca una tabla ya creada."""
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE inventario_corridas (id INTEGER PRIMARY KEY, ejecutado_en TEXT)")
    con.commit()
    con.close()

    init_schema(sqlite3.connect(db))
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(inventario_corridas)")}
    con.close()
    assert {"sobrante_referencias", "sobrante_unidades", "bochica_altura"} <= cols


def test_las_views_se_recrean_despues_de_migrar(db):
    """El UPDATE del backfill abría una transacción implícita y hacía
    reventar el BEGIN IMMEDIATE de las VIEWs — dejándolas con la definición
    vieja y la corrida entera sin persistir."""
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE inventario_corridas (id INTEGER PRIMARY KEY, ejecutado_en TEXT,
           referencias_negativas INTEGER, unidades_negativas REAL)"""
    )
    con.execute(
        "INSERT INTO inventario_corridas VALUES (1, '2026-07-01T00:00:00+00:00', 7, -700.0)"
    )
    con.commit()
    con.close()

    init_schema(sqlite3.connect(db))  # no debe lanzar
    con = sqlite3.connect(db)
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='v_inventario_corridas'"
    ).fetchone()[0]
    con.close()
    assert "sobrante_referencias" in sql


def test_backfill_recupera_el_sobrante_de_corridas_viejas(db):
    """Las corridas anteriores a DEC-051 ya traen el dato con el nombre
    viejo: se copia en vez de arrancar la tendencia desde cero."""
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE inventario_corridas (id INTEGER PRIMARY KEY, ejecutado_en TEXT,
           referencias_negativas INTEGER, unidades_negativas REAL)"""
    )
    con.execute(
        "INSERT INTO inventario_corridas VALUES (1, '2026-07-01T00:00:00+00:00', 7, -700.0)"
    )
    con.commit()
    con.close()

    init_schema(sqlite3.connect(db))
    con = sqlite3.connect(db)
    fila = con.execute(
        "SELECT sobrante_referencias, sobrante_unidades FROM inventario_corridas WHERE id=1"
    ).fetchone()
    con.close()
    assert fila == (7, 700.0), "el signo se invierte: 'negativas' era el sobrante al revés"


def test_backfill_no_pisa_lo_ya_calculado(db):
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    antes = con.execute("SELECT sobrante_unidades FROM inventario_corridas").fetchone()[0]
    con.close()

    init_schema(sqlite3.connect(db))  # vuelve a correr la migración
    con = sqlite3.connect(db)
    despues = con.execute("SELECT sobrante_unidades FROM inventario_corridas").fetchone()[0]
    con.close()
    assert antes == despues


def test_la_corrida_registra_el_sobrante(db):
    """PR11A: teórico 100, altura 372 → sobrante 272."""
    con_sobrante = _comparacion(
        [("PR11A", "PR", False, 100.0, 0.0, 100.0, 372.0, 5.0, 0.0, 377.0, 277.0, 272.0, -272.0)]
    )
    corrida_id = persistir(con_sobrante, _anomalias(), _FRESCURA_OK, db_path=db)
    con = sqlite3.connect(db)
    fila = con.execute(
        "SELECT sobrante_referencias, sobrante_unidades FROM inventario_corridas WHERE id=?",
        (corrida_id,),
    ).fetchone()
    con.close()
    assert fila == (1, 272.0)


def test_la_tendencia_toma_una_corrida_por_dia(db):
    """El scheduler corre cada hora: promediar mezclaría fotos del mismo
    día. Se toma la última, que es el estado con que cerró la jornada."""
    con = sqlite3.connect(db)
    init_schema(con)
    filas = [
        ("2026-07-20T14:00:00+00:00", 5, 500.0),
        ("2026-07-20T20:00:00+00:00", 7, 700.0),  # última del 20 (09:00 hora CO)
        ("2026-07-21T20:00:00+00:00", 9, 900.0),
    ]
    for momento, refs, unidades in filas:
        con.execute(
            """INSERT INTO inventario_corridas
               (ejecutado_en, sobrante_referencias, sobrante_unidades)
               VALUES (?, ?, ?)""",
            (momento, refs, unidades),
        )
    con.commit()

    serie = con.execute(
        """SELECT DATE(ejecutado_en, '-5 hours') AS dia, COUNT(*),
                  MAX(ejecutado_en), sobrante_unidades
           FROM inventario_corridas
           WHERE sobrante_unidades IS NOT NULL
           GROUP BY dia ORDER BY dia"""
    ).fetchall()
    con.close()

    assert len(serie) == 2, "dos días distintos"
    assert serie[0][1] == 2, "el 20 agrupa dos corridas"
    # SQLite: con un único MAX, las columnas sueltas vienen de esa misma
    # fila (mismo comportamiento documentado que aprovecha DEC-021).
    assert serie[0][3] == 700.0, "se queda con la última corrida del día"


def test_la_fecha_de_la_tendencia_usa_hora_colombia(db):
    """`ejecutado_en` se guarda en UTC. Sin convertir, una corrida de las
    23:00 hora local caería en el día siguiente."""
    con = sqlite3.connect(db)
    init_schema(con)
    # 2026-07-21 02:00 UTC = 2026-07-20 21:00 en Colombia.
    con.execute(
        """INSERT INTO inventario_corridas
           (ejecutado_en, sobrante_referencias, sobrante_unidades)
           VALUES ('2026-07-21T02:00:00+00:00', 3, 300.0)"""
    )
    con.commit()
    dia = con.execute("SELECT DATE(ejecutado_en, '-5 hours') FROM inventario_corridas").fetchone()[
        0
    ]
    con.close()
    assert dia == "2026-07-20"


# ─────────────────────────────────────────────
# conteos: tabla con historia (DEC-058)
# ─────────────────────────────────────────────


def _conteo(ubicacion="A_1_5", archivo="hoja.xlsx", quien="ANA", cantidad=100.0):
    return {
        "ubicacion": ubicacion,
        "id_especificacion": "1",
        "fecha": "2026-07-27",
        "contado_por": quien,
        "cantidad_contada": cantidad,
        "cantidad_sistema": 100.0,
        "diferencia": cantidad - 100.0,
        "diferencia_pct": 0.0,
        "clase": "A",
        "tipo": "Altura",
        "causa": None,
        "tolerancia": 0.01,
        "exacta": 1,
        "hallazgo": "Coincide",
        "lote": None,
        "vencimiento": None,
        "actividad_origen": "Conteo dirigido",
        "observacion": None,
        "archivo": archivo,
    }


def test_sacar_una_hoja_deshace_su_conteo(db):
    """La operación de corrección que DEC-058 tenía que habilitar.

    `conteos` es un espejo de `data/conteos/`: si la hoja de Luis sale de la
    carpeta, su conteo desaparece del cálculo. Con la tabla acumulativa
    original esto era imposible y un archivo mal cargado ensuciaba el IRA
    para siempre.
    """
    dos = pd.DataFrame([_conteo(archivo="ana.xlsx"), _conteo("B_2_6", "luis.xlsx", "LUIS")])
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db, conteos=dos)

    # Segunda corrida: la carpeta ya solo tiene la hoja de Ana.
    una = pd.DataFrame([_conteo(archivo="ana.xlsx")])
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db, conteos=una)

    con = sqlite3.connect(db)
    quedan = [r[0] for r in con.execute("SELECT contado_por FROM conteos")]
    assert quedan == ["ANA"]


def test_corregir_una_cantidad_reemplaza_el_valor(db):
    """Editar el Excel y re-ingerir no deja las dos versiones conviviendo."""
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame([_conteo(cantidad=50.0)]),
    )
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame([_conteo(cantidad=120.0)]),
    )

    con = sqlite3.connect(db)
    assert con.execute("SELECT cantidad_contada FROM conteos").fetchall() == [(120.0,)]


def test_carpeta_ausente_no_borra_los_conteos(db):
    """`None` significa 'no toques la tabla', y es distinto de 'carpeta vacía'.

    Si el disco de red no monta, la corrida no puede vaciar el historial.
    """
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame([_conteo()]),
    )
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db, conteos=None)

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM conteos").fetchone()[0] == 1


def test_carpeta_vacia_si_vacia_el_espejo(db):
    """Distinto del caso anterior: acá el usuario sacó las hojas a propósito."""
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame([_conteo()]),
    )
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame(columns=list(_COLUMNAS_CONTEOS)),
    )

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM conteos").fetchone()[0] == 0


def test_el_rastro_del_archivo_sobrevive_a_su_anulacion(db):
    """Sacar una hoja no puede ser una pérdida silenciosa.

    `conteos_archivos` acumula: aunque el conteo salga del cálculo, queda
    constancia de que ese archivo existió y cuándo se vio por última vez.
    """
    archivos = pd.DataFrame(
        [
            {
                "archivo": "ana.xlsx",
                "filas": 8,
                "modificado_en": "2026-07-27T10:00:00+00:00",
                "visto_en": "2026-07-27T10:05:00+00:00",
            },
            {
                "archivo": "luis.xlsx",
                "filas": 8,
                "modificado_en": "2026-07-27T10:00:00+00:00",
                "visto_en": "2026-07-27T10:05:00+00:00",
            },
        ]
    )
    persistir(_comparacion(), _anomalias(), _FRESCURA_OK, db_path=db, archivos=archivos)
    # Luis se anula: la siguiente corrida ya no lo trae.
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        conteos=pd.DataFrame(columns=list(_COLUMNAS_CONTEOS)),
        archivos=archivos.head(1),
    )

    con = sqlite3.connect(db)
    registrados = {r[0] for r in con.execute("SELECT archivo FROM conteos_archivos")}
    assert registrados == {"ana.xlsx", "luis.xlsx"}


def test_primera_vez_no_se_pisa_en_las_reingestas(db):
    """Es la fecha en que la hoja entró, no la de la última corrida."""
    base = {"archivo": "ana.xlsx", "filas": 8, "modificado_en": "2026-07-27T10:00:00+00:00"}
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        archivos=pd.DataFrame([{**base, "visto_en": "2026-07-27T10:00:00+00:00"}]),
    )
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        archivos=pd.DataFrame([{**base, "visto_en": "2026-07-28T15:00:00+00:00"}]),
    )

    con = sqlite3.connect(db)
    primera, visto = con.execute("SELECT primera_vez, visto_en FROM conteos_archivos").fetchone()
    assert primera.startswith("2026-07-27")
    assert visto.startswith("2026-07-28")


def test_conteos_archivos_no_esta_en_la_lista_de_tablas_recreables():
    """El rastro de archivos sí es historia: un DROP la perdería."""
    assert "conteos_archivos" not in _SNAPSHOTS_RECREABLES


def test_lo_que_calculan_los_modulos_es_lo_que_se_persiste():
    """Guard estructural: una columna nueva en el módulo pero no en el DDL.

    `CREATE TABLE IF NOT EXISTS` no agrega columnas y `_migrar_columnas()`
    solo migra lo que el DDL declara, así que una columna añadida al
    cálculo y olvidada en el esquema se pierde en silencio hasta que el
    dashboard la pide y revienta. Pasó con `origen_clase`.
    """
    from inventario.conteos import _COLUMNAS as COLS_CONTEOS
    from inventario.persistencia import _COLUMNAS_CONTEOS, _COLUMNAS_UBICACIONES
    from inventario.ubicaciones import _COLUMNAS as COLS_UBICACIONES

    assert set(COLS_UBICACIONES) == set(_COLUMNAS_UBICACIONES)
    assert set(COLS_CONTEOS) == set(_COLUMNAS_CONTEOS)


def test_las_columnas_persistidas_existen_en_la_tabla(db):
    """La tupla de INSERT y el DDL tienen que hablar del mismo esquema."""
    from inventario.persistencia import _COLUMNAS_CONTEOS, _COLUMNAS_UBICACIONES

    con = sqlite3.connect(db)
    init_schema(con)
    for tabla, columnas in (
        ("inventario_ubicaciones", _COLUMNAS_UBICACIONES),
        ("conteos", _COLUMNAS_CONTEOS),
    ):
        reales = {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}
        assert set(columnas) <= reales, f"{tabla}: faltan {set(columnas) - reales}"
    con.close()


# ─────────────────────────────────────────────
# alertas (DEC-059)
# ─────────────────────────────────────────────


def _alerta(clave="quiebre:PA01", severidad="Crítica", valor=500.0):
    return {
        "clave": clave,
        "tipo": "Quiebre de stock",
        "severidad": severidad,
        "entidad": "PA01",
        "detalle": "sin disponible",
        "valor": valor,
        "modulo": "Salud",
    }


def test_la_alerta_conserva_su_fecha_de_aparicion(db):
    """De esto depende la antigüedad: si `primera_vez` se pisara en cada
    corrida, toda alerta parecería recién nacida y el indicador no mediría
    nada."""
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        alertas=pd.DataFrame([_alerta()]),
    )
    con = sqlite3.connect(db)
    primera = con.execute("SELECT primera_vez FROM alertas").fetchone()[0]
    con.close()

    # Segunda corrida: la misma alerta sigue abierta, con otro valor.
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        alertas=pd.DataFrame([_alerta(valor=900.0)]),
    )
    con = sqlite3.connect(db)
    fila = con.execute("SELECT primera_vez, visto_en, valor FROM alertas").fetchone()
    con.close()

    assert fila[0] == primera  # la aparición no se movió
    assert fila[1] >= primera  # la última vez sí avanzó
    assert fila[2] == 900.0  # el valor se actualizó


def test_la_alerta_que_deja_de_emitirse_queda_como_resuelta(db):
    """No se borra: su `visto_en` viejo la marca, y de ahí sale el tiempo
    de resolución."""
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        alertas=pd.DataFrame([_alerta("quiebre:PA01"), _alerta("quiebre:PB02")]),
    )
    # PB02 se resolvió: la siguiente corrida ya no la emite.
    persistir(
        _comparacion(),
        _anomalias(),
        _FRESCURA_OK,
        db_path=db,
        alertas=pd.DataFrame([_alerta("quiebre:PA01")]),
    )

    con = sqlite3.connect(db)
    activas = {r[0] for r in con.execute("SELECT clave FROM v_alertas WHERE activa = 1")}
    todas = {r[0] for r in con.execute("SELECT clave FROM v_alertas")}
    con.close()

    assert activas == {"quiebre:PA01"}
    assert todas == {"quiebre:PA01", "quiebre:PB02"}
