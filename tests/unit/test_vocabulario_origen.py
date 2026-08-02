"""Detector de cambios del sistema origen (DEC-081).

La vista del pedido en la SPA cambia sola: entre enero y julio de 2026
aparecieron 18 valores nuevos en cuatro vocabularios y ninguno lo detectó el
pipeline. Se encontraron revisando el DOM a mano.
"""

import sqlite3

import pandas as pd
import pytest

from inventario.hallazgos import vocabulario_nuevo_en_origen

pytestmark = pytest.mark.unit


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE timeline_pedido (id_pedido TEXT, titulo TEXT, fecha_hora TEXT)")
    c.execute(
        "CREATE TABLE registro_operaciones (id_pedido TEXT, accion TEXT, momento TEXT, "
        "referencia TEXT)"
    )
    c.execute("CREATE TABLE estadisticas_monto (id_pedido TEXT, concepto_base TEXT)")
    c.execute("CREATE TABLE pedidos (id_pedido TEXT, fecha TEXT)")
    yield c
    c.close()


def _timeline(con, titulo, fecha="2026-07-30 10:00:00"):
    con.execute("INSERT INTO timeline_pedido VALUES ('P1',?,?)", (titulo, fecha))


def _accion(con, accion, momento="2026-07-30 10:00:00"):
    con.execute("INSERT INTO registro_operaciones VALUES ('P1',?,?,NULL)", (accion, momento))


def _veredicto(con, referencia, momento="2026-07-30 10:00:00"):
    con.execute(
        "INSERT INTO registro_operaciones VALUES ('P1','Auditoría de pago',?,?)",
        (momento, referencia),
    )


def _concepto(con, concepto, fecha="2026-07-30"):
    con.execute("INSERT INTO pedidos VALUES ('P1',?)", (fecha,))
    con.execute("INSERT INTO estadisticas_monto VALUES ('P1',?)", (concepto,))


def test_el_vocabulario_conocido_no_alerta(con):
    """El estado normal: la SPA emite lo de siempre y nadie se entera."""
    _timeline(con, "Alistamiento")
    _accion(con, "Confirmar pedido")
    _concepto(con, "Total IVA")

    assert vocabulario_nuevo_en_origen(con).cantidad == 0


def test_un_paso_nuevo_del_timeline_se_detecta(con):
    """Es el caso real de «Esperando a ser recogido», que apareció el
    2026-03-26 y pasó cuatro meses inadvertido."""
    _timeline(con, "Esperando a ser entregado por dron", "2026-07-29 08:00:00")
    h = vocabulario_nuevo_en_origen(con)

    assert h.cantidad == 1
    assert h.filas.iloc[0]["Valor nuevo"] == "Esperando a ser entregado por dron"
    assert h.filas.iloc[0]["Dónde"] == "Paso del timeline"


def test_una_accion_nueva_se_detecta(con):
    _accion(con, "Devolución solicitada")

    assert vocabulario_nuevo_en_origen(con).cantidad == 1


def test_un_concepto_de_monto_nuevo_se_detecta(con):
    """Los descuentos «auto-recogida» y «por tasa» aparecieron el 2026-02-28
    sin aviso; hoy suman 23.554 filas."""
    _concepto(con, "Descuento fidelidad")

    assert vocabulario_nuevo_en_origen(con).cantidad == 1


def test_reporta_cuando_apareció_por_primera_vez(con):
    """Sin la fecha el aviso no es accionable: no distingue un cambio de ayer
    de una deuda que lleva cinco meses pasando inadvertida."""
    _accion(con, "Algo nuevo", "2026-03-15 09:00:00")
    _accion(con, "Algo nuevo", "2026-07-30 09:00:00")
    h = vocabulario_nuevo_en_origen(con)

    assert h.filas.iloc[0]["Visto por primera vez"] == "2026-03-15"
    assert h.filas.iloc[0]["Veces"] == 2


def test_los_cuatro_vocabularios_se_reportan_juntos(con):
    _timeline(con, "Paso raro")
    _accion(con, "Acción rara")
    _concepto(con, "Concepto raro")
    _veredicto(con, "Devuelto a revisión")
    h = vocabulario_nuevo_en_origen(con)

    assert h.cantidad == 4
    assert set(h.filas["Dónde"]) == {
        "Paso del timeline",
        "Acción del registro",
        "Concepto de monto",
        "Veredicto de auditoría",
    }


# ── DEC-083: el veredicto de la auditoría ────────────────────────────────────


def test_un_veredicto_nuevo_se_detecta(con):
    """El caso que motivó ampliar el detector: `Aprobado`/`Rechazado`
    aparecieron el 2026-04-10 en `referencia` y DEC-081 no los vio porque
    solo vigilaba `accion`."""
    _veredicto(con, "Aprobado con reserva", "2026-04-10 09:00:00")
    h = vocabulario_nuevo_en_origen(con)

    assert h.cantidad == 1
    assert h.filas.iloc[0]["Valor nuevo"] == "Aprobado con reserva"
    assert h.filas.iloc[0]["Dónde"] == "Veredicto de auditoría"
    assert h.filas.iloc[0]["Visto por primera vez"] == "2026-04-10"


def test_el_veredicto_declarado_no_alerta(con):
    _veredicto(con, "Aprobado")
    _veredicto(con, "Rechazado")

    assert vocabulario_nuevo_en_origen(con).cantidad == 0


def test_el_id_numerico_del_comprobante_no_se_confunde_con_un_veredicto(con):
    """`referencia` guarda dos cosas distintas: el veredicto en las auditorías
    y el ID numérico del comprobante. Sin el filtro, cada ID sería un
    'valor nuevo' y el detector reportaría miles de falsos positivos."""
    _veredicto(con, "144357")
    _veredicto(con, "144336")

    assert vocabulario_nuevo_en_origen(con).cantidad == 0


def test_el_motivo_de_cancelacion_no_entra_al_vocabulario(con):
    """La misma columna guarda el motivo de `Cancelar pedido` en texto libre
    —con emojis y variantes de mayúsculas—. Ahí no hay vocabulario cerrado que
    declarar, así que el detector solo mira la referencia de las auditorías."""
    con.execute(
        "INSERT INTO registro_operaciones VALUES "
        "('P1','Cancelar pedido','2026-07-30 10:00:00','Excede dias limites de pago 🍀')"
    )

    assert vocabulario_nuevo_en_origen(con).cantidad == 0


def test_lo_mas_reciente_va_primero(con):
    _accion(con, "Vieja", "2026-02-01 09:00:00")
    _accion(con, "Nueva", "2026-07-30 09:00:00")

    assert list(vocabulario_nuevo_en_origen(con).filas["Valor nuevo"]) == ["Nueva", "Vieja"]


def test_declarar_el_valor_apaga_el_hallazgo(con, monkeypatch):
    """Reconocer un valor es agregarlo a `comun/`: misma mecánica de
    auto-cierre que el resto de los detectores (DEC-047)."""
    _accion(con, "Cambiar vehículo")  # ya declarada en ACCIONES_CONOCIDAS

    assert vocabulario_nuevo_en_origen(con).cantidad == 0


def test_sin_las_tablas_no_explota():
    """Los detectores corren antes de que exista nada en una base nueva."""
    h = vocabulario_nuevo_en_origen(sqlite3.connect(":memory:"))

    assert h.cantidad == 0
    assert isinstance(h.filas, pd.DataFrame)
