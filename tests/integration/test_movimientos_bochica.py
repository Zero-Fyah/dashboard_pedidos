"""Tests de scraper/movimientos_bochica.py — esquema y carga.

Van en integration/ porque ejercitan SQLite real y lectura de archivo: es
justamente el contrato que se quiere verificar (idempotencia, normalización
de placeholders, clave natural, parseo de fecha en español), no algo que
un mock pueda sustituir.
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from scraper.movimientos_bochica import (
    RETENCION_HISTORICO_DIAS,
    archivar_snapshot_bochica,
    cargar_movimientos,
    init_schema,
    ya_cargado,
)

pytestmark = pytest.mark.integration

_COLUMNAS_FUENTE = [
    "Fecha",
    "Bodega Origen",
    "Ubicación Origen",
    "Bodega Destino",
    "Ubicación Destino",
    "Producto",
    "Cantidad",
    "Lote",
    "Vencimiento",
    "Responsable",
    "Tipo",
]


def _fila(
    fecha="25/8/2026, 4:56:53 p. m.",
    bodega_origen="MOSQUERA",
    ubicacion_origen="PU_1_1",
    bodega_destino="MOSQUERA",
    ubicacion_destino="F_43_5",
    sku_id="1828930401800818691",
    cantidad="18",
    lote="-",
    vencimiento="-",
    responsable="GERMAN ALEXANDER JOSA BOTINA",
    tipo="traslado",
):
    return {
        "Fecha": fecha,
        "Bodega Origen": bodega_origen,
        "Ubicación Origen": ubicacion_origen,
        "Bodega Destino": bodega_destino,
        "Ubicación Destino": ubicacion_destino,
        "Producto": sku_id,
        "Cantidad": cantidad,
        "Lote": lote,
        "Vencimiento": vencimiento,
        "Responsable": responsable,
        "Tipo": tipo,
    }


def _escribir_tsv(tmp_path, filas, nombre="movimientos.txt"):
    ruta = tmp_path / nombre
    pd.DataFrame(filas, columns=_COLUMNAS_FUENTE).to_csv(ruta, sep="\t", index=False)
    return ruta


def _escribir_xlsx(tmp_path, filas, nombre="movimientos.xlsx"):
    """Formato real de producción: descargar_movimientos_bochica() escribe
    .xlsx, no .txt — los tests de carga de este archivo usaban solo TSV
    hasta la auditoría del 2026-08-25 (hallazgo de qa-engineer: la rama
    .xlsx de cargar_movimientos() nunca se había ejercitado)."""
    ruta = tmp_path / nombre
    pd.DataFrame(filas, columns=_COLUMNAS_FUENTE).to_excel(ruta, sheet_name="Sheet1", index=False)
    return ruta


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test_pedidos.db")
    init_schema(path)
    return path


def test_init_schema_es_idempotente(db):
    init_schema(db)  # segunda llamada no debe fallar
    con = sqlite3.connect(db)
    tablas = {fila[0] for fila in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "movimientos_bochica" in tablas


def test_cargar_movimientos_inserta_filas_nuevas(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila()])
    insertadas = cargar_movimientos(ruta, db, origen="backfill")
    assert insertadas == 1
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert total == 1


def test_cargar_movimientos_es_idempotente(db, tmp_path):
    """Cargar el mismo archivo dos veces no duplica filas (INSERT OR IGNORE)."""
    ruta = _escribir_tsv(tmp_path, [_fila()])
    cargar_movimientos(ruta, db, origen="backfill")
    segunda = cargar_movimientos(ruta, db, origen="backfill")
    assert segunda == 0
    con = sqlite3.connect(db)
    total = con.execute("SELECT COUNT(*) FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert total == 1


def test_cargar_movimientos_parsea_fecha_am_pm_a_iso(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila(fecha="1/4/2026, 6:41:26 a. m.")])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    fecha = con.execute("SELECT fecha_operacion FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert fecha == "2026-04-01 06:41:26"


def test_cargar_movimientos_cantidad_placeholder_queda_null(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila(tipo="error", cantidad="-")])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    fila = con.execute("SELECT cantidad FROM movimientos_bochica WHERE cantidad IS NULL").fetchone()
    con.close()
    assert fila is not None


def test_cargar_movimientos_error_con_cantidad_real_no_se_pisa(db, tmp_path):
    """340 filas 'error' de la carga real traen cantidad — no se asume NULL."""
    ruta = _escribir_tsv(tmp_path, [_fila(tipo="error", cantidad="1560")])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    tipo, cantidad = con.execute("SELECT tipo, cantidad FROM movimientos_bochica").fetchone()
    con.close()
    assert (tipo, cantidad) == ("error", 1560.0)


def test_cargar_movimientos_lote_vencimiento_placeholder_queda_null(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila(lote="—", vencimiento="—")])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    lote, vencimiento = con.execute("SELECT lote, vencimiento FROM movimientos_bochica").fetchone()
    con.close()
    assert (lote, vencimiento) == (None, None)


def test_cargar_movimientos_lote_vencimiento_reales_se_conservan(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila(lote="CN26", vencimiento="2027-12-04")])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    lote, vencimiento = con.execute("SELECT lote, vencimiento FROM movimientos_bochica").fetchone()
    con.close()
    assert (lote, vencimiento) == ("CN26", "2027-12-04")


def test_cargar_movimientos_reintegro_origen_sintetico_se_conserva(db, tmp_path):
    ruta = _escribir_tsv(
        tmp_path,
        [_fila(ubicacion_origen="REINTEGRO-130206", ubicacion_destino="M_46_3", tipo="reintegro")],
    )
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    origen_ubic = con.execute("SELECT ubicacion_origen FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert origen_ubic == "REINTEGRO-130206"


def test_cargar_movimientos_tipo_desconocido_no_falla_pero_avisa(db, tmp_path, caplog):
    ruta = _escribir_tsv(tmp_path, [_fila(tipo="ajuste_nuevo")])
    with caplog.at_level("WARNING"):
        insertadas = cargar_movimientos(ruta, db, origen="backfill")
    assert insertadas == 1
    assert "ajuste_nuevo" in caplog.text


def test_ya_cargado_detecta_fecha_existente(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila(fecha="25/8/2026, 4:56:53 p. m.")])
    cargar_movimientos(ruta, db, origen="backfill")
    assert ya_cargado(db, date(2026, 8, 25)) is True
    assert ya_cargado(db, date(2026, 8, 24)) is False


def test_dos_tipos_mismo_movimiento_no_colisionan(db, tmp_path):
    """La clave natural incluye `tipo`: un traslado y un error sobre el mismo
    SKU/hora/ubicaciones/responsable no se pisan entre sí."""
    filas = [
        _fila(sku_id="2000000000000000001", tipo="traslado", cantidad="50"),
        _fila(sku_id="2000000000000000001", tipo="error", cantidad="-"),
    ]
    ruta = _escribir_tsv(tmp_path, filas)
    insertadas = cargar_movimientos(ruta, db, origen="backfill")
    assert insertadas == 2


def test_origen_se_registra_por_fila(db, tmp_path):
    ruta = _escribir_tsv(tmp_path, [_fila()])
    cargar_movimientos(ruta, db, origen="backfill")
    con = sqlite3.connect(db)
    origen = con.execute("SELECT origen FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert origen == "backfill"


def test_cantidad_no_participa_en_la_clave_natural(db, tmp_path):
    """Documenta como contrato una decisión ya tomada (comentario del
    módulo, no antes fijado por ningún test): dos filas con la misma clave
    natural pero distinta `cantidad` colisionan — la segunda se descarta en
    silencio (INSERT OR IGNORE). Si algún día se agrega `cantidad` al
    índice único, este test debe fallar y forzar a revisar la decisión."""
    filas = [
        _fila(sku_id="3000000000000000001", cantidad="18"),
        _fila(sku_id="3000000000000000001", cantidad="99"),
    ]
    ruta = _escribir_tsv(tmp_path, filas)
    insertadas = cargar_movimientos(ruta, db, origen="backfill")
    assert insertadas == 1

    con = sqlite3.connect(db)
    cantidad = con.execute("SELECT cantidad FROM movimientos_bochica").fetchone()[0]
    con.close()
    assert cantidad == 18.0  # se queda con la primera; la segunda se ignora


class TestCargarMovimientosXlsx:
    """Rama .xlsx de cargar_movimientos() — el formato real que produce
    descargar_movimientos_bochica() en producción, no ejercitado hasta la
    auditoría del 2026-08-25 (los demás tests de este archivo usan TSV)."""

    def test_inserta_filas_desde_xlsx(self, db, tmp_path):
        ruta = _escribir_xlsx(tmp_path, [_fila()])
        insertadas = cargar_movimientos(ruta, db, origen="diario")
        assert insertadas == 1
        con = sqlite3.connect(db)
        total = con.execute("SELECT COUNT(*) FROM movimientos_bochica").fetchone()[0]
        con.close()
        assert total == 1

    def test_placeholders_en_xlsx_quedan_null(self, db, tmp_path):
        ruta = _escribir_xlsx(
            tmp_path, [_fila(tipo="error", cantidad="-", lote="—", vencimiento="—")]
        )
        cargar_movimientos(ruta, db, origen="diario")
        con = sqlite3.connect(db)
        cantidad, lote, vencimiento = con.execute(
            "SELECT cantidad, lote, vencimiento FROM movimientos_bochica"
        ).fetchone()
        con.close()
        assert (cantidad, lote, vencimiento) == (None, None, None)

    def test_es_idempotente_igual_que_tsv(self, db, tmp_path):
        ruta = _escribir_xlsx(tmp_path, [_fila()])
        cargar_movimientos(ruta, db, origen="diario")
        segunda = cargar_movimientos(ruta, db, origen="diario")
        assert segunda == 0


class TestArchivarSnapshotBochica:
    """archivar_snapshot_bochica() — copia fechada + purga de retención."""

    def test_copia_el_snapshot_con_el_nombre_de_la_fecha(self, tmp_path):
        vivo = tmp_path / "bochica_inventario.xlsx"
        vivo.write_bytes(b"contenido de prueba")
        historico = tmp_path / "historico"

        destino = archivar_snapshot_bochica(
            date(2026, 8, 25), origen=vivo, carpeta_historico=historico
        )

        assert destino == historico / "bochica_inventario_2026-08-25.xlsx"
        assert destino.read_bytes() == b"contenido de prueba"

    def test_snapshot_vivo_ausente_no_falla(self, tmp_path):
        vivo = tmp_path / "no_existe.xlsx"
        historico = tmp_path / "historico"

        destino = archivar_snapshot_bochica(
            date(2026, 8, 25), origen=vivo, carpeta_historico=historico
        )

        assert destino is None
        assert not historico.exists() or list(historico.glob("*.xlsx")) == []

    def test_purga_snapshots_mas_viejos_que_la_retencion(self, tmp_path):
        vivo = tmp_path / "bochica_inventario.xlsx"
        vivo.write_bytes(b"x")
        historico = tmp_path / "historico"
        historico.mkdir()

        hoy = date(2026, 8, 25)
        viejo = historico / "bochica_inventario_2026-06-01.xlsx"  # > 30 días antes
        viejo.write_bytes(b"viejo")
        reciente = historico / "bochica_inventario_2026-08-10.xlsx"  # < 30 días antes
        reciente.write_bytes(b"reciente")

        archivar_snapshot_bochica(hoy, origen=vivo, carpeta_historico=historico)

        assert not viejo.exists()
        assert reciente.exists()
        assert (historico / "bochica_inventario_2026-08-25.xlsx").exists()

    def test_no_purga_dentro_de_la_ventana_de_retencion(self, tmp_path):
        vivo = tmp_path / "bochica_inventario.xlsx"
        vivo.write_bytes(b"x")
        historico = tmp_path / "historico"
        historico.mkdir()

        hoy = date(2026, 8, 25)
        limite = hoy - timedelta(days=RETENCION_HISTORICO_DIAS)
        justo_dentro = historico / f"bochica_inventario_{limite.isoformat()}.xlsx"
        justo_dentro.write_bytes(b"limite")

        archivar_snapshot_bochica(hoy, origen=vivo, carpeta_historico=historico)

        assert justo_dentro.exists()

    def test_ignora_archivos_con_nombre_no_reconocido(self, tmp_path):
        vivo = tmp_path / "bochica_inventario.xlsx"
        vivo.write_bytes(b"x")
        historico = tmp_path / "historico"
        historico.mkdir()
        ajeno = historico / "bochica_inventario_notas.xlsx"
        ajeno.write_bytes(b"nota manual, no una fecha")

        archivar_snapshot_bochica(date(2026, 8, 25), origen=vivo, carpeta_historico=historico)

        assert ajeno.exists()
