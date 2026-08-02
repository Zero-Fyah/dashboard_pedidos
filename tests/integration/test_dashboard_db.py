"""AUD-M6 (defensa secundaria): dashboard/db.py debe abrir sus conexiones
con timeout=5 en vez del default de sqlite3 (0s, falla de inmediato ante
"database is locked"). La causa raíz de la ventana "no such view" ya
quedó resuelta en el ETL (DEC-019) — esto solo cubre contención residual
con el ETL/scraper escribiendo al mismo tiempo.
"""

import sqlite3

import pandas as pd
import pytest

import dashboard.db as ddb

# Referencia capturada antes de cualquier monkeypatch — el espía la usa
# para abrir la conexión real sin recursar sobre sí mismo (ddb.sqlite3 es
# el mismo objeto módulo que este `sqlite3`, así que parchear
# ddb.sqlite3.connect también reemplaza sqlite3.connect globalmente).
_CONNECT_REAL = sqlite3.connect


def _espiar_connect(monkeypatch):
    """Parchea ddb.sqlite3.connect y devuelve el dict de kwargs capturados."""
    llamadas: dict = {}

    def _connect_espia(path, **kwargs):
        llamadas.update(kwargs)
        return _CONNECT_REAL(path, **kwargs)

    monkeypatch.setattr(ddb.sqlite3, "connect", _connect_espia)
    return llamadas


@pytest.mark.integration
def test_conn_pasa_timeout_5(monkeypatch, tmp_path):
    db_file = tmp_path / "existe.db"
    db_file.touch()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    llamadas = _espiar_connect(monkeypatch)

    con = ddb._conn()
    con.close()

    assert llamadas.get("timeout") == 5


@pytest.mark.integration
def test_conn_sigue_lanzando_filenotfound_si_no_existe_la_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "no_existe.db")
    with pytest.raises(FileNotFoundError):
        ddb._conn()


@pytest.mark.integration
def test_num_cols_exist_pasa_timeout_5(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "cols.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE lineas_pedido (monto_pagar_num REAL)")
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    llamadas = _espiar_connect(monkeypatch)

    assert ddb._num_cols_exist() is True
    assert llamadas.get("timeout") == 5
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_view_consolidado_exists_pasa_timeout_5(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "view.db"
    monkeypatch.setattr(ddb, "DB_PATH", db_file)
    db_file.touch()
    llamadas = _espiar_connect(monkeypatch)

    assert ddb._view_consolidado_exists() is False
    assert llamadas.get("timeout") == 5
    ddb.st.cache_data.clear()


# ── AUD-M12: TTL único + indicador de frescura ──────────────────────────────


@pytest.mark.integration
def test_ttl_unico_y_razonable():
    """AUD-M12: un solo TTL para todo el módulo (antes 7200s en queries y
    300s en checks de esquema, inconsistente) y lo bastante corto para no
    volver a acumular horas de datos viejos con el scheduler cada 1h."""
    assert ddb._CACHE_TTL_S <= 1200


@pytest.mark.integration
def test_ultima_actualizacion_devuelve_max(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "fresco.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE pedidos (id_pedido TEXT, actualizado_en TEXT)")
    con_setup.executemany(
        "INSERT INTO pedidos VALUES (?, ?)",
        [("P1", "2026-07-22T10:00:00+00:00"), ("P2", "2026-07-22T12:00:00+00:00")],
    )
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    assert ddb.get_ultima_actualizacion() == "2026-07-22T12:00:00+00:00"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_ultima_actualizacion_none_si_tabla_vacia(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    db_file = tmp_path / "vacio.db"
    con_setup = _CONNECT_REAL(db_file)
    con_setup.execute("CREATE TABLE pedidos (id_pedido TEXT, actualizado_en TEXT)")
    con_setup.commit()
    con_setup.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    assert ddb.get_ultima_actualizacion() is None
    ddb.st.cache_data.clear()


# ── DEC-065: vigencia y cumplimiento de despacho ─────────────────────────────


def _db_con_salud(tmp_path, con_vigencia: bool):
    """Base con v_inventario_salud, con o sin la columna de DEC-065."""
    db_file = tmp_path / f"salud_{con_vigencia}.db"
    con = _CONNECT_REAL(db_file)
    cols = (
        "referencia TEXT, vigencia TEXT, estado TEXT"
        if con_vigencia
        else ("referencia TEXT, estado TEXT")
    )
    con.execute(f"CREATE TABLE inventario_salud ({cols})")
    if con_vigencia:
        con.executemany(
            "INSERT INTO inventario_salud VALUES (?,?,?)",
            [("PA01", "Activo", "Normal"), ("PB02", None, "Normal")],
        )
    else:
        con.execute("INSERT INTO inventario_salud VALUES ('PA01','Normal')")
    con.execute("CREATE VIEW v_inventario_salud AS SELECT * FROM inventario_salud")
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_salud_sin_columna_vigencia_degrada_a_todo_vigente(monkeypatch, tmp_path):
    """DEC-065: entre el deploy y la primera corrida del scheduler la columna
    no existe. La página tiene que abrir con el catálogo completo, no vacía."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_con_salud(tmp_path, con_vigencia=False))

    df = ddb.get_inventario_salud()

    assert list(df["vigencia"]) == [ddb.VIGENCIA_ACTIVO]
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_salud_rellena_vigencia_nula(monkeypatch, tmp_path):
    """Una fila migrada pero aún sin repoblar tiene vigencia NULL: cae del
    lado vigente para no desaparecer de la página en silencio."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_con_salud(tmp_path, con_vigencia=True))

    df = ddb.get_inventario_salud().set_index("referencia")

    assert df.loc["PA01", "vigencia"] == ddb.VIGENCIA_ACTIVO
    assert df.loc["PB02", "vigencia"] == ddb.VIGENCIA_ACTIVO
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_despacho_sin_las_views_devuelve_vacio(monkeypatch, tmp_path):
    """Los getters corren antes de que el ETL cree sus VIEWs en una base
    nueva: tienen que devolver vacío, no reventar."""
    ddb.st.cache_data.clear()
    db_file = tmp_path / "sin_views.db"
    db_file.touch()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    assert ddb.get_despacho_diario().empty
    # DEC-069: los faltantes salen de lineas_pedido. Si la tabla no existe
    # el getter devuelve vacío en vez de propagar: pandas envuelve el error
    # en DatabaseError, que NO es sqlite3.OperationalError y se escaparía
    # del except de la página.
    assert ddb.get_despacho_faltantes("2026-01-01", "2026-12-31").empty
    assert ddb.get_despacho_lineas_diario("2026-01-01", "2026-12-31").empty
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_despacho_diario_cuenta_pedidos_y_faltantes(monkeypatch, tmp_path):
    """El fill rate se calcula sobre TODOS los pedidos completos, no solo
    sobre los que tienen diferencia: el denominador es el total del día."""
    ddb.st.cache_data.clear()
    db_file = tmp_path / "despacho.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, hay_diferencia INT, "
        "scraping_completo INT)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?)",
        [
            ("P1", "2026-07-01", 1, 1),
            ("P2", "2026-07-01", 0, 1),
            ("P3", "2026-07-01", 0, 1),
            ("P4", "2026-07-02", 0, 1),
            ("P5", "2026-07-02", 1, 0),  # incompleto: fuera del universo
            ("P6", "", 0, 1),  # sin fecha: fuera
        ],
    )
    con.execute("CREATE TABLE gd (id_pedido TEXT, fecha TEXT, monto_diferencia_num REAL)")
    con.executemany("INSERT INTO gd VALUES (?,?,?)", [("P1", "2026-07-01", 5000.0)])
    con.execute(
        "CREATE VIEW v_diferencias_resumen AS SELECT id_pedido, fecha, monto_diferencia_num FROM gd"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    df = ddb.get_despacho_diario().set_index("fecha")

    assert df.loc["2026-07-01", "pedidos"] == 3
    assert df.loc["2026-07-01", "pedidos_con_faltante"] == 1
    assert df.loc["2026-07-01", "monto_no_despachado"] == 5000.0
    # El día sin faltantes existe con 0, no desaparece: si faltara, el fill
    # rate del periodo se calcularía sobre un denominador incompleto.
    assert df.loc["2026-07-02", "pedidos"] == 1
    assert df.loc["2026-07-02", "pedidos_con_faltante"] == 0
    assert df.loc["2026-07-02", "monto_no_despachado"] == 0
    assert "" not in df.index
    ddb.st.cache_data.clear()


# ── DEC-069: fill rate sobre lineas_pedido ───────────────────────────────────


def _db_despacho(tmp_path):
    """Base con lineas_pedido/subpedidos/pedidos para el fill rate."""
    db_file = tmp_path / "fill.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, hay_diferencia INT, scraping_completo INT)"
    )
    con.execute("CREATE TABLE subpedidos (id_pedido TEXT, numero_subpedido TEXT, estado TEXT)")
    con.execute(
        "CREATE TABLE lineas_pedido (id_pedido TEXT, numero_subpedido TEXT, referencia TEXT, "
        "codigo_barras TEXT, nombre_producto TEXT, almacen TEXT, cantidad_comprada REAL, "
        "cantidad_entregada REAL)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?)",
        [("P1", "2026-07-01", 1, 1), ("P2", "2026-07-02", 0, 1), ("P3", "2026-07-03", 1, 1)],
    )
    con.executemany(
        "INSERT INTO subpedidos VALUES (?,?,?)",
        [
            ("P1", "S1", "Completado"),  # despachado, corto
            ("P2", "S1", "Completado"),  # despachado, completo
            ("P3", "S1", "Cancelado"),  # cancelado: NO es faltante
        ],
    )
    con.executemany(
        "INSERT INTO lineas_pedido VALUES (?,?,?,?,?,?,?,?)",
        [
            ("P1", "S1", "PA01", "7701", "Corto", "Bogotá", 10.0, 4.0),
            ("P2", "S1", "PB02", "7702", "Completo", "Bogotá", 5.0, 5.0),
            ("P3", "S1", "PC03", "7703", "Cancelado", "Bogotá", 8.0, 0.0),
        ],
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_faltantes_traen_referencia_para_cruzar(monkeypatch, tmp_path):
    """DEC-069: el defecto de la fuente anterior era no tener con qué unir.
    Sin `referencia` no se puede decir qué SKU falló, de qué clase ni con
    qué stock — que es lo único accionable en bodega."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_despacho(tmp_path))

    df = ddb.get_despacho_faltantes("2026-07-01", "2026-07-31")

    assert list(df["referencia"]) == ["PA01"]
    assert df["codigo_barras"].notna().all()
    assert df.iloc[0]["faltante"] == 6.0
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_un_subpedido_cancelado_no_es_faltante(monkeypatch, tmp_path):
    """No salió corto: no salió. Incluirlo sumaba 101 líneas que son
    cancelaciones, no incumplimiento de despacho."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_despacho(tmp_path))

    df = ddb.get_despacho_faltantes("2026-07-01", "2026-07-31")

    assert "PC03" not in set(df["referencia"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_rango_se_aplica_en_sql(monkeypatch, tmp_path):
    """Acotar en SQL y no en pandas: traer los 7 meses cuesta 2,0s contra
    0,6s del mes que la página muestra."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_despacho(tmp_path))

    assert ddb.get_despacho_faltantes("2026-07-02", "2026-07-31").empty
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_lineas_diario_separa_despachadas_de_faltantes(monkeypatch, tmp_path):
    """El fill rate por línea necesita el denominador completo: todas las
    líneas despachadas, no solo las que fallaron."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_despacho(tmp_path))

    df = ddb.get_despacho_lineas_diario("2026-07-01", "2026-07-31").set_index("fecha")

    # El cancelado no entra al universo despachado por ningún lado.
    assert df["lineas"].sum() == 2
    assert df["lineas_con_faltante"].sum() == 1
    assert df.loc["2026-07-01", "unidades_faltantes"] == 6.0
    assert df.loc["2026-07-02", "unidades_faltantes"] == 0.0
    ddb.st.cache_data.clear()


# ── DEC-082: ciclo de auditoría de pago ──────────────────────────────────────


def _db_pagos(tmp_path):
    db_file = tmp_path / "pagos.db"
    con = _CONNECT_REAL(db_file)
    con.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, forma_pago TEXT)")
    con.execute(
        "CREATE TABLE registro_operaciones (id_pedido TEXT, momento TEXT, usuario TEXT, "
        "accion TEXT, referencia TEXT)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?)",
        [
            ("P1", "2026-07-01", "Pago inmediato"),  # subido y auditado
            ("P2", "2026-07-02", "Pago a crédito"),  # subido, SIN auditar
            ("P3", "2026-07-03", "Pago inmediato"),  # sin comprobante
            ("P4", "2026-08-01", "Pago inmediato"),  # fuera de rango
            # P5 es el caso que rompe los contadores si no se pre-agrega:
            # 2 comprobantes x 2 auditorías = 4 filas en el join (DEC-083).
            ("P5", "2026-07-05", "Pago inmediato"),
        ],
    )
    con.executemany(
        "INSERT INTO registro_operaciones VALUES (?,?,?,?,?)",
        [
            ("P1", "2026-07-01 08:00:00", "ana", "Subir comprobante de pago", "9001"),
            ("P1", "2026-07-01 11:00:00", "beto", "Auditoría de pago", "Aprobado"),
            ("P2", "2026-07-02 09:00:00", "ana", "Subir comprobante de pago", None),
            ("P3", "2026-07-03 09:00:00", "ana", "Confirmar pedido", None),
            ("P4", "2026-08-01 09:00:00", "ana", "Subir comprobante de pago", None),
            ("P5", "2026-07-05 08:00:00", "ana", "Subir comprobante de pago", "9002"),
            ("P5", "2026-07-05 09:00:00", "beto", "Auditoría de pago", "Rechazado"),
            ("P5", "2026-07-05 10:00:00", "ana", "Subir comprobante de pago", "9003"),
            ("P5", "2026-07-05 12:00:00", "beto", "Auditoría de pago", "Aprobado"),
        ],
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_el_comprobante_sin_auditar_no_desaparece(monkeypatch, tmp_path):
    """DEC-082: es el caso que más importa — una cola de trabajo invisible.
    Un INNER JOIN lo borraría justo a él."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    df = ddb.get_auditoria_pago("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert set(df.index) == {"P1", "P2", "P5"}
    # pandas convierte el NULL de SQLite en NaN, no en None.
    assert pd.isna(df.loc["P2", "auditado_en"])
    assert pd.isna(df.loc["P2", "auditado_por"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_calcula_el_ciclo_del_que_si_se_audito(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    df = ddb.get_auditoria_pago("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert df.loc["P1", "subido_en"] == "2026-07-01 08:00:00"
    assert df.loc["P1", "auditado_en"] == "2026-07-01 11:00:00"
    assert df.loc["P1", "auditado_por"] == "beto"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_un_pedido_sin_comprobante_no_entra(monkeypatch, tmp_path):
    """El universo son los pedidos con comprobante subido, no todos."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    assert "P3" not in set(ddb.get_auditoria_pago("2026-07-01", "2026-07-31")["id_pedido"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_rango_se_aplica_sobre_la_fecha_del_pedido(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    assert "P4" not in set(ddb.get_auditoria_pago("2026-07-01", "2026-07-31")["id_pedido"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_la_tabla_devuelve_vacio(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    vacia = tmp_path / "vacia.db"
    vacia.touch()
    monkeypatch.setattr(ddb, "DB_PATH", vacia)

    assert ddb.get_auditoria_pago("2026-01-01", "2026-12-31").empty
    ddb.st.cache_data.clear()


# ── DEC-083: el veredicto de la auditoría ────────────────────────────────────


@pytest.mark.integration
def test_los_contadores_no_cuentan_doble_con_varios_comprobantes(monkeypatch, tmp_path):
    """DEC-083: es el riesgo real de agregar SUM() a esta consulta.

    P5 tiene 2 comprobantes y 2 auditorías. Sin pre-agregar las subidas, el
    join produce 4 filas y los contadores se duplican: 2 rechazos donde hay 1.
    El MIN() original era inmune al problema; los contadores no lo son.
    """
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    df = ddb.get_auditoria_pago("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert df.loc["P5", "comprobantes"] == 2
    assert df.loc["P5", "auditorias"] == 2
    assert df.loc["P5", "rechazos"] == 1
    assert df.loc["P5", "aprobaciones"] == 1
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_ciclo_se_mide_desde_el_primer_comprobante(monkeypatch, tmp_path):
    """Con varios intentos, lo que importa es cuánto esperó el cliente desde
    que subió el primero, no desde el último."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_pagos(tmp_path))

    df = ddb.get_auditoria_pago("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert df.loc["P5", "subido_en"] == "2026-07-05 08:00:00"
    assert df.loc["P5", "auditado_en"] == "2026-07-05 09:00:00"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_veredicto_registrado_no_cuenta_como_aprobado(monkeypatch, tmp_path):
    """El veredicto solo existe desde el 2026-04-10. Una auditoría anterior
    tiene `referencia` nula y debe quedar FUERA del denominador, no contarse
    como aprobación — eso hundiría la tasa de rechazo del primer trimestre."""
    ddb.st.cache_data.clear()
    db_file = tmp_path / "sinver.db"
    con = _CONNECT_REAL(db_file)
    con.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, forma_pago TEXT)")
    con.execute(
        "CREATE TABLE registro_operaciones (id_pedido TEXT, momento TEXT, usuario TEXT, "
        "accion TEXT, referencia TEXT)"
    )
    con.execute("INSERT INTO pedidos VALUES ('V1','2026-02-10','Pago inmediato')")
    con.executemany(
        "INSERT INTO registro_operaciones VALUES (?,?,?,?,?)",
        [
            ("V1", "2026-02-10 08:00:00", "ana", "Subir comprobante de pago", None),
            ("V1", "2026-02-10 09:00:00", "beto", "Auditoría de pago", None),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    df = ddb.get_auditoria_pago("2026-02-01", "2026-02-28").set_index("id_pedido")

    assert df.loc["V1", "auditorias"] == 1  # sí se auditó
    assert df.loc["V1", "con_veredicto"] == 0  # pero no sabemos en qué terminó
    assert df.loc["V1", "aprobaciones"] == 0
    ddb.st.cache_data.clear()


# ── DEC-084: saldo a favor del cliente ───────────────────────────────────────


def _db_saldos(tmp_path):
    db_file = tmp_path / "saldos.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, nit TEXT, "
        "nombre_empresa TEXT, forma_pago TEXT)"
    )
    con.execute(
        "CREATE TABLE gestion_diferencias (id_pedido TEXT, total_pagar_pedido_num REAL, "
        "monto_final_pagar_num REAL, monto_pagado_num REAL, monto_diferencia_num REAL)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?,?)",
        [
            ("S1", "2026-07-01", "900", "Acme", "Pago inmediato"),  # a favor
            ("S2", "2026-07-02", "901", "Beta", "Pago a crédito"),  # en contra
            ("S3", "2026-07-03", "902", "Gama", "Pago inmediato"),  # anómalo
            ("S4", "2026-07-04", "903", "Delta", "Pago inmediato"),  # sin saldo
            ("S5", "2026-08-01", "904", "Eps", "Pago inmediato"),  # fuera de rango
        ],
    )
    con.executemany(
        "INSERT INTO gestion_diferencias VALUES (?,?,?,?,?)",
        [
            ("S1", 1000.0, 800.0, 1000.0, 200.0),  # pagó el total, faltó 200 → +200
            ("S2", 1000.0, 900.0, 500.0, 100.0),  # solo pagó 500 → −400
            ("S3", 1000.0, 900.0, 50000.0, 100.0),  # pagó 50x → anómalo
            ("S4", 1000.0, 900.0, 900.0, 100.0),  # pagó justo lo facturado
            ("S5", 1000.0, 800.0, 1000.0, 200.0),
        ],
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_el_saldo_lleva_el_signo_correcto(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_saldos(tmp_path))

    df = ddb.get_saldo_a_favor("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert df.loc["S1", "saldo"] == 200.0  # a favor del cliente
    assert df.loc["S2", "saldo"] == -400.0  # el cliente todavía debe
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_pedido_que_pago_justo_no_aparece(monkeypatch, tmp_path):
    """Solo interesan los pedidos con saldo. S4 pagó exactamente lo facturado."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_saldos(tmp_path))

    assert "S4" not in set(ddb.get_saldo_a_favor("2026-07-01", "2026-07-31")["id_pedido"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_anomalo_se_marca_pero_no_se_descarta(monkeypatch, tmp_path):
    """DEC-084: 16 pedidos cargan el 79% del saldo bruto y son datos malos del
    origen. La capa de datos los MARCA; decidir qué mostrar es de la página.
    Descartarlos acá los volvería invisibles y nadie los investigaría."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_saldos(tmp_path))

    df = ddb.get_saldo_a_favor("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert "S3" in df.index
    assert df.loc["S3", "anomalo"] == 1
    assert df.loc["S1", "anomalo"] == 0
    assert df.loc["S2", "anomalo"] == 0
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_rango_del_saldo_se_aplica_sobre_la_fecha_del_pedido(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_saldos(tmp_path))

    assert "S5" not in set(ddb.get_saldo_a_favor("2026-07-01", "2026-07-31")["id_pedido"])
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_gestion_diferencias_devuelve_vacio(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    vacia = tmp_path / "vacia2.db"
    vacia.touch()
    monkeypatch.setattr(ddb, "DB_PATH", vacia)

    assert ddb.get_saldo_a_favor("2026-01-01", "2026-12-31").empty
    ddb.st.cache_data.clear()


# ── DEC-085: motivos de cancelación y pedidos impagos ────────────────────────


def _db_cancel(tmp_path):
    db_file = tmp_path / "cancel.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, forma_pago TEXT, "
        "nombre_empresa TEXT, nit TEXT, scraping_completo INTEGER)"
    )
    con.execute(
        "CREATE TABLE registro_operaciones (id_pedido TEXT, momento TEXT, usuario TEXT, "
        "accion TEXT, referencia TEXT)"
    )
    con.execute(
        "CREATE TABLE estadisticas_monto (id_pedido TEXT, concepto_base TEXT, "
        "monto_final_num REAL, monto_pagar_num REAL)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?,?,1)",
        [
            ("C1", "2026-07-01", "Pago inmediato", "Acme", "900"),
            ("C2", "2026-07-02", "Pago inmediato", "Beta", "901"),
            ("C3", "2026-08-01", "Pago inmediato", "Gama", "902"),
        ],
    )
    con.executemany(
        "INSERT INTO registro_operaciones VALUES (?,?,?,?,?)",
        [
            # C1 se cancela dos veces: gana el PRIMER evento.
            ("C1", "2026-07-06 10:00:00", "ana", "Cancelar pedido", "sin pago"),
            ("C1", "2026-07-09 10:00:00", "ana", "Cancelar pedido", "confirmar devolucion"),
            ("C2", "2026-07-05 10:00:00", "beto", "Cancelar pedido", "pedido duplicado"),
            ("C3", "2026-08-04 10:00:00", "ana", "Cancelar pedido", "sin pago"),
        ],
    )
    con.executemany(
        "INSERT INTO estadisticas_monto VALUES (?,?,?,?)",
        [
            ("C1", "Total a pagar / Total final a pagar", 1000.0, 1000.0),
            ("C2", "Total a pagar / Total final a pagar", 2000.0, 2000.0),
            ("C3", "Total a pagar / Total final a pagar", 3000.0, 3000.0),
        ],
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_se_toma_el_primer_evento_de_cancelacion(monkeypatch, tmp_path):
    """Un pedido puede cancelarse varias veces (5.784 eventos sobre 4.947
    pedidos). El primero es el que explica por qué murió; los siguientes son
    reintentos administrativos y contarlos duplicaría el pedido."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_cancel(tmp_path))

    df = ddb.get_motivos_cancelacion("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert len(df) == 2  # C1 y C2, una fila cada uno
    assert df.loc["C1", "motivo"] == "sin pago"
    assert df.loc["C1", "cancelado_en"] == "2026-07-06 10:00:00"


@pytest.mark.integration
def test_los_dias_hasta_cancelar_se_calculan_en_sql(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_cancel(tmp_path))

    df = ddb.get_motivos_cancelacion("2026-07-01", "2026-07-31").set_index("id_pedido")

    assert df.loc["C1", "dias"] == pytest.approx(5.42, abs=0.01)
    assert df.loc["C1", "valor"] == 1000.0


@pytest.mark.integration
def test_el_rango_de_motivos_filtra_por_fecha_del_pedido(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_cancel(tmp_path))

    ids = set(ddb.get_motivos_cancelacion("2026-07-01", "2026-07-31")["id_pedido"])

    assert "C3" not in ids


@pytest.mark.integration
def test_sin_registro_de_operaciones_no_explota(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    vacia = tmp_path / "vacia3.db"
    vacia.touch()
    monkeypatch.setattr(ddb, "DB_PATH", vacia)

    assert ddb.get_motivos_cancelacion("2026-01-01", "2026-12-31").empty


# -- DEC-086: crédito abierto y su vencimiento -------------------------------


def _db_credito(tmp_path):
    db_file = tmp_path / "credito.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, forma_pago TEXT, nit TEXT, "
        "nombre_empresa TEXT, dias_credito TEXT, inicio_credito TEXT, "
        "vencimiento_credito TEXT, scraping_completo INTEGER)"
    )
    con.execute("CREATE TABLE subpedidos (id_pedido TEXT, estado TEXT)")
    con.execute("CREATE TABLE registro_operaciones (id_pedido TEXT, accion TEXT, referencia TEXT)")
    con.execute(
        "CREATE TABLE estadisticas_monto (id_pedido TEXT, concepto_base TEXT, monto_final_num REAL)"
    )
    con.execute("CREATE TABLE lineas_pedido (id_pedido TEXT, monto_pagar_num REAL)")
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?,?,?,?,?,1)",
        [
            # K1: credito abierto y vencido hace mucho.
            (
                "K1",
                "2026-01-05",
                "Pago a crédito",
                "900",
                "Acme",
                "30",
                "2026-01-05 10:00:00",
                "2026-02-04",
            ),
            # K2: subio comprobante -> pago, NO es credito abierto.
            (
                "K2",
                "2026-01-06",
                "Pago a crédito",
                "901",
                "Beta",
                "30",
                "2026-01-06 10:00:00",
                "2026-02-05",
            ),
            # K3: cancelado -> fuera.
            (
                "K3",
                "2026-01-07",
                "Pago a crédito",
                "902",
                "Gama",
                "30",
                "2026-01-07 10:00:00",
                "2026-02-06",
            ),
            # K4: sin vencimiento -> el credito ya se solto, fuera.
            ("K4", "2026-01-08", "Pago a crédito", "903", "Delta", None, None, None),
            # K5: pago inmediato -> no es credito.
            (
                "K5",
                "2026-01-09",
                "Pago inmediato",
                "904",
                "Eps",
                "30",
                "2026-01-09 10:00:00",
                "2026-02-08",
            ),
        ],
    )
    con.executemany(
        "INSERT INTO subpedidos VALUES (?,?)",
        [
            ("K1", "Entregado sin liquidar"),
            ("K2", "Completado"),
            ("K3", "Cancelado"),
            ("K4", "Completado"),
            ("K5", "Completado"),
        ],
    )
    con.execute("INSERT INTO registro_operaciones VALUES ('K2','Subir comprobante de pago','9001')")
    con.executemany(
        "INSERT INTO estadisticas_monto VALUES (?,?,?)",
        [
            ("K1", "Total a pagar / Total final a pagar", 1000.0),
            ("K2", "Total a pagar / Total final a pagar", 2000.0),
            ("K3", "Total a pagar / Total final a pagar", 3000.0),
            ("K4", "Total a pagar / Total final a pagar", 4000.0),
            ("K5", "Total a pagar / Total final a pagar", 5000.0),
        ],
    )
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_el_comprobante_saca_al_pedido_del_credito_abierto(monkeypatch, tmp_path):
    """DEC-086: es LA decision de la consulta. Los pedidos `Completado` que
    conservan los campos de credito tienen comprobante el 100% de las veces
    -- ya pagaron y el origen no limpio el campo. Sin este filtro entrarian
    188 pedidos ($1.192 M) que no se le deben a nadie."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_credito(tmp_path))

    ids = set(ddb.get_credito_abierto()["id_pedido"])

    assert "K1" in ids
    assert "K2" not in ids
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_no_entra_lo_cancelado_ni_lo_que_solto_el_vencimiento(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_credito(tmp_path))

    ids = set(ddb.get_credito_abierto()["id_pedido"])

    assert "K3" not in ids  # cancelado
    assert "K4" not in ids  # sin vencimiento: el credito ya se solto
    assert "K5" not in ids  # pago inmediato, no es credito
    assert ids == {"K1"}
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_atraso_es_positivo_cuando_ya_vencio(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_credito(tmp_path))

    df = ddb.get_credito_abierto().set_index("id_pedido")

    # Vencio el 2026-02-04, o sea que a esta altura el atraso es grande y
    # positivo. El signo es lo que separa "al dia" de "vencido" en la pagina.
    assert df.loc["K1", "atraso"] > 0
    assert df.loc["K1", "valor"] == 1000.0
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_las_tablas_de_credito_devuelve_vacio(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    vacia = tmp_path / "vacia4.db"
    vacia.touch()
    monkeypatch.setattr(ddb, "DB_PATH", vacia)

    assert ddb.get_credito_abierto().empty
    ddb.st.cache_data.clear()


# -- DEC-089: el saldo sale del origen, no de la derivacion ------------------


def _db_estado_pago(tmp_path):
    db_file = tmp_path / "estadopago.db"
    con = _CONNECT_REAL(db_file)
    con.execute(
        "CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, forma_pago TEXT, "
        "nombre_empresa TEXT, nit TEXT, scraping_completo INTEGER, pago_estado TEXT, "
        "pago_total_num REAL, pago_pagado_num REAL, pago_saldo_num REAL, pago_progreso TEXT)"
    )
    con.execute("CREATE TABLE subpedidos (id_pedido TEXT, estado TEXT)")
    con.execute(
        "CREATE TABLE estadisticas_monto (id_pedido TEXT, concepto_base TEXT, "
        "monto_final_num REAL, monto_pagar_num REAL)"
    )
    con.execute(
        "CREATE TABLE lineas_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id_pedido TEXT, referencia TEXT, cantidad_comprada TEXT, monto_pagar_num REAL)"
    )
    con.execute(
        "CREATE TABLE registros_pago (id_pedido TEXT, metodo_pago TEXT, estado_revision TEXT)"
    )
    con.executemany(
        "INSERT INTO pedidos VALUES (?,?,?,?,?,1,?,?,?,?,?)",
        [
            # E1: el origen dice Pagado; la derivacion diria que debe 500.
            (
                "E1",
                "2026-07-20",
                "Pago inmediato",
                "Acme",
                "900",
                "Pagado",
                1000.0,
                1000.0,
                0.0,
                "100%",
            ),
            # E2: el origen dice que debe 300.
            (
                "E2",
                "2026-07-21",
                "Pago inmediato",
                "Beta",
                "901",
                "Pendiente de pago",
                1000.0,
                700.0,
                300.0,
                "70%",
            ),
            # E3: anterior al 2026-07-16, sin tarjeta -> derivacion.
            ("E3", "2026-05-10", "Pago inmediato", "Gama", "902", None, None, None, None, None),
        ],
    )
    con.executemany(
        "INSERT INTO subpedidos VALUES (?,?)",
        [
            ("E1", "Pendiente de entrega"),
            ("E2", "Pendiente de entrega"),
            ("E3", "Pendiente de entrega"),
        ],
    )
    con.executemany(
        "INSERT INTO estadisticas_monto VALUES (?,?,?,?)",
        [
            # La derivacion para E1 dice 500 de saldo: el campo viejo miente.
            ("E1", "Total a pagar / Total final a pagar", 1000.0, None),
            ("E1", "Monto pagado", None, 500.0),
            ("E2", "Total a pagar / Total final a pagar", 1000.0, None),
            ("E2", "Monto pagado", None, 700.0),
            ("E3", "Total a pagar / Total final a pagar", 800.0, None),
            ("E3", "Monto pagado", None, 200.0),
        ],
    )
    con.execute("INSERT INTO registros_pago VALUES ('E2','Pago en linea','Aprobado')")
    con.commit()
    con.close()
    return db_file


@pytest.mark.integration
def test_el_saldo_del_origen_le_gana_a_la_derivacion(monkeypatch, tmp_path):
    """DEC-089: E1 esta pagado segun el origen, pero `estadisticas_monto` dice
    que debe 500. Medido en produccion: la derivacion marcaba 230 impagos y el
    origen 224, y los 6 de diferencia estaban TODOS pagados."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_estado_pago(tmp_path))

    df = ddb.get_pedidos_impagos().set_index("id_pedido")

    assert "E1" not in df.index
    assert df.loc["E2", "saldo"] == 300.0
    assert df.loc["E2", "fuente"] == "origen"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_tarjeta_cae_a_la_derivacion_y_lo_declara(monkeypatch, tmp_path):
    """La tarjeta solo existe desde el 2026-07-16. Un pedido anterior tiene que
    seguir apareciendo —con su saldo derivado— y decir que lo es."""
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_estado_pago(tmp_path))

    df = ddb.get_pedidos_impagos().set_index("id_pedido")

    assert df.loc["E3", "saldo"] == 600.0
    assert df.loc["E3", "fuente"] == "derivado"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_estado_de_pago_trae_el_conteo_de_comprobantes(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_estado_pago(tmp_path))

    df = ddb.get_estado_pago("2026-01-01", "2026-12-31").set_index("id_pedido")

    assert set(df.index) == {"E1", "E2"}  # E3 no tiene tarjeta
    assert df.loc["E2", "comprobantes"] == 1
    assert df.loc["E2", "metodos"] == "Pago en linea"
    assert df.loc["E1", "comprobantes"] == 0
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_el_estado_de_pago_respeta_el_rango(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    monkeypatch.setattr(ddb, "DB_PATH", _db_estado_pago(tmp_path))

    df = ddb.get_estado_pago("2026-07-21", "2026-07-31")

    assert list(df["id_pedido"]) == ["E2"]
    ddb.st.cache_data.clear()


# -- DEC-090: comprobantes uno por uno --------------------------------------


@pytest.mark.integration
def test_el_placeholder_del_revisor_se_normaliza(monkeypatch, tmp_path):
    """El origen escribe '-' cuando todavia nadie reviso. Dejarlo pasar
    convertiria a '-' en el revisor mas productivo de la tabla (826 filas)."""
    ddb.st.cache_data.clear()
    db_file = tmp_path / "comp.db"
    con = _CONNECT_REAL(db_file)
    con.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT, nombre_empresa TEXT)")
    con.execute(
        "CREATE TABLE registros_pago (id_pedido TEXT, secuencia TEXT, metodo_pago TEXT, "
        "cuenta_receptora TEXT, monto_comprobante_num REAL, monto_pago_num REAL, "
        "hora_pago TEXT, fecha_envio TEXT, estado_revision TEXT, fecha_revision TEXT, "
        "revisor TEXT, observaciones TEXT)"
    )
    con.execute("INSERT INTO pedidos VALUES ('C1','2026-07-20','Acme')")
    con.executemany(
        "INSERT INTO registros_pago VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "C1",
                "1-1",
                "Comprobante subido",
                "BANCO 1",
                100.0,
                100.0,
                "2026-07-20 10:00",
                "2026-07-20 10:05",
                "Aprobado",
                "2026-07-20 11:00",
                "ana",
                "",
            ),
            (
                "C1",
                "1-2",
                "Pago en linea",
                "-",
                50.0,
                50.0,
                "2026-07-20 12:00",
                "",
                "",
                "",
                "-",
                "",
            ),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ddb, "DB_PATH", db_file)

    df = ddb.get_comprobantes("2026-07-01", "2026-07-31").set_index("secuencia")

    assert df.loc["1-1", "revisor"] == "ana"
    assert df.loc["1-2", "revisor"] == ""  # el '-' no es una persona
    assert df.loc["1-2", "estado"] == "Sin revisar"
    ddb.st.cache_data.clear()


@pytest.mark.integration
def test_sin_la_tabla_de_comprobantes_no_explota(monkeypatch, tmp_path):
    ddb.st.cache_data.clear()
    vacia = tmp_path / "vacia5.db"
    vacia.touch()
    monkeypatch.setattr(ddb, "DB_PATH", vacia)

    assert ddb.get_comprobantes("2026-01-01", "2026-12-31").empty
    ddb.st.cache_data.clear()
