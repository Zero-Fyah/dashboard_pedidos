"""Tests de los detectores de inconsistencias (DEC-047).

Unitarios: los detectores son funciones puras sobre un DataFrame y una
conexión de lectura. Se les arma un catálogo mínimo en memoria y una
SQLite temporal, sin tocar los Excel reales.

Lo que más importa verificar es la propiedad que hace funcionar todo el
módulo: **un detector sin casos no se devuelve**, que es lo que hace que
una tarea desaparezca sola cuando se corrige el origen.
"""

import sqlite3

import pandas as pd
import pytest

from inventario.hallazgos import (
    codigos_barras_multiples_ids,
    detectar_todos,
    especificacion_discrepante,
    estados_sin_clasificar,
    lineas_sin_id_producto,
    personal_duplicado,
    referencias_con_espacios,
)

pytestmark = pytest.mark.unit


def _admin(filas):
    return pd.DataFrame(
        filas,
        columns=[
            "id_especificacion",
            "referencia",
            "codigo_barras",
            "especificacion",
            "nombre_comercial",
        ],
    )


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE lineas_pedido (id_pedido TEXT, referencia TEXT,
           codigo_barras TEXT, presentacion TEXT, nombre_producto TEXT)"""
    )
    c.execute("CREATE TABLE subpedidos (id_pedido TEXT, estado TEXT)")
    c.execute(
        """CREATE TABLE catalogo_productos (referencia TEXT, codigo_barras TEXT,
           id_producto TEXT)"""
    )
    yield c
    c.close()


# ─────────────────────────────────────────────
# Códigos de barras con múltiples ID
# ─────────────────────────────────────────────


def test_codigo_con_dos_ids_se_detecta():
    admin = _admin(
        [
            ("E1", "PJ91", "7700001", "Rojo", "Peluche"),
            ("E2", "PJ91", "7700001", "Azul", "Peluche"),
        ]
    )
    h = codigos_barras_multiples_ids(admin)
    assert h.cantidad == 1
    assert set(h.filas["ID de especificación"]) == {"E1", "E2"}


def test_cantidad_cuenta_codigos_no_filas_de_detalle():
    """El detalle lista un ID por fila; la cifra debe ser de códigos."""
    admin = _admin(
        [
            ("E1", "PJ91", "7700001", "a", "P"),
            ("E2", "PJ91", "7700001", "b", "P"),
            ("E3", "PJ91", "7700001", "c", "P"),
        ]
    )
    h = codigos_barras_multiples_ids(admin)
    assert h.cantidad == 1, "1 código afectado"
    assert len(h.filas) == 3, "3 filas de detalle"


def test_codigo_con_un_solo_id_no_se_detecta():
    admin = _admin([("E1", "PJ91", "7700001", "Rojo", "Peluche")])
    assert codigos_barras_multiples_ids(admin).cantidad == 0


# ─────────────────────────────────────────────
# Referencias con espacios
# ─────────────────────────────────────────────


@pytest.mark.parametrize("referencia", ["PJ91 ", " PJ91", "  PJ91  "])
def test_espacios_sobrantes_se_detectan(referencia):
    admin = _admin([("E1", referencia, "7700001", "x", "Peluche")])
    h = referencias_con_espacios(admin)
    assert h.cantidad == 1
    assert h.filas.iloc[0]["Debería ser"] == "PJ91"


def test_referencia_limpia_no_se_detecta():
    admin = _admin([("E1", "PJ91", "7700001", "x", "Peluche")])
    assert referencias_con_espacios(admin).cantidad == 0


# ─────────────────────────────────────────────
# Especificación discrepante
# ─────────────────────────────────────────────


def test_especificacion_solo_de_formato_no_es_discrepancia(con):
    """Etiqueta, comas, tildes y mayúsculas no cuentan: es el mismo texto."""
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','PS11','7700001','Sabores: A - Atún','x')")
    admin = _admin([("E1", "PS11", "7700001", "Presentación: a - atún;", "Snack")])
    assert especificacion_discrepante(admin, con).cantidad == 0


def test_especificacion_con_contenido_distinto_se_detecta(con):
    """El catálogo agrega la fecha de vencimiento y pedidos no."""
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','PS11','7700001','Sabores: A - Atún','x')")
    admin = _admin([("E1", "PS11", "7700001", "A - Atún FV 07-2027", "Snack")])
    h = especificacion_discrepante(admin, con)
    assert h.cantidad == 1
    assert "FV 07-2027" in h.filas.iloc[0]["Como figura en el catálogo"]


def test_producto_ausente_del_catalogo_no_cuenta_como_discrepancia(con):
    """Ausencia no es discrepancia: eso lo reporta otro detector."""
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','XX99','9999999','algo','x')")
    admin = _admin([("E1", "PS11", "7700001", "otra cosa", "Snack")])
    assert especificacion_discrepante(admin, con).cantidad == 0


# ─────────────────────────────────────────────
# Estados sin clasificar
# ─────────────────────────────────────────────


def test_estado_desconocido_se_detecta(con):
    con.execute("INSERT INTO subpedidos VALUES ('P1','Entregado sin liquidar')")
    con.execute("INSERT INTO subpedidos VALUES ('P2','Completado')")
    h = estados_sin_clasificar(con)
    assert h.cantidad == 1
    assert h.filas.iloc[0]["Estado"] == "Entregado sin liquidar"


def test_estados_conocidos_no_se_detectan(con):
    for e in ("Completado", "Cancelado", "En inspección"):
        con.execute("INSERT INTO subpedidos VALUES ('P1', ?)", (e,))
    assert estados_sin_clasificar(con).cantidad == 0


# ─────────────────────────────────────────────
# Líneas sin ID de producto
# ─────────────────────────────────────────────


def test_linea_sin_par_en_el_catalogo_se_detecta(con):
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','PJ91','7700001','x','Peluche')")
    h = lineas_sin_id_producto(con)
    assert h.cantidad == 1
    assert h.filas.iloc[0]["Líneas afectadas"] == 1


def test_linea_con_id_resuelto_no_se_detecta(con):
    con.execute("INSERT INTO lineas_pedido VALUES ('P1','PJ91','7700001','x','Peluche')")
    con.execute("INSERT INTO catalogo_productos VALUES ('PJ91','7700001','ID1')")
    assert lineas_sin_id_producto(con).cantidad == 0


# ─────────────────────────────────────────────
# La mecánica de "desaparecer al resolverse"
# ─────────────────────────────────────────────


def test_detectar_todos_omite_los_detectores_sin_casos(con):
    """Es lo que hace que una tarea corregida desaparezca del módulo."""
    admin = _admin([("E1", "PJ91", "7700001", "Rojo", "Peluche")])  # todo limpio
    assert detectar_todos(admin, con) == []


def test_detectar_todos_devuelve_solo_lo_que_tiene_casos(con):
    con.execute("INSERT INTO subpedidos VALUES ('P1','Estado Raro')")
    admin = _admin(
        [
            ("E1", "PJ91 ", "7700001", "Rojo", "Peluche"),  # espacio sobrante
            ("E2", "PA10", "7700002", "Azul", "Otro"),
        ]
    )
    claves = {h.clave for h in detectar_todos(admin, con)}
    assert claves == {"referencias_con_espacios", "estados_sin_clasificar"}


def test_un_detector_roto_no_tumba_a_los_demas(con, caplog):
    """Una consulta desactualizada no debe dejar el módulo entero sin datos."""
    con.execute("INSERT INTO subpedidos VALUES ('P1','Estado Raro')")
    admin = _admin([("E1", "PJ91", "7700001", "Rojo", "Peluche")]).drop(
        columns=["nombre_comercial"]  # rompe los detectores que la usan
    )
    with caplog.at_level("ERROR"):
        hallazgos = detectar_todos(admin, con)
    assert any(h.clave == "estados_sin_clasificar" for h in hallazgos)
    assert "un detector falló" in caplog.text


# ─────────────────────────────────────────────
# personal_duplicado (DEC-056)
# ─────────────────────────────────────────────


def _con_personal(alistadores: list[tuple[str, str]]) -> sqlite3.Connection:
    """Conexión en memoria con subpedidos/registro_operaciones mínimos."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE subpedidos (id_pedido TEXT, alistador TEXT, inspector TEXT)")
    con.execute("CREATE TABLE registro_operaciones (usuario TEXT, tipo_usuario TEXT)")
    con.executemany(
        "INSERT INTO subpedidos (id_pedido, alistador, inspector) VALUES (?, ?, '-')",
        alistadores,
    )
    return con


def test_personal_duplicado_detecta_letra_faltante():
    """Una letra de diferencia en el apellido es el caso real que motivó DEC-056."""
    con = _con_personal(
        [("1", "LIZETH CAROLINA HERNADEZ"), ("2", "LIZETH CAROLINA HERNANDEZ MUÑOZ")]
    )
    hallazgo = personal_duplicado(con)

    assert hallazgo.cantidad == 1  # una persona, no dos variantes
    assert len(hallazgo.filas) == 2
    assert set(hallazgo.filas["Grupo"]) == {1}


def test_personal_duplicado_agrupa_nombre_truncado():
    """`PASTOR YESID` contra el nombre completo da similitud 0,60: lo salva el prefijo."""
    con = _con_personal([("1", "PASTOR YESID RODRIGUEZ NIETO"), ("2", "PASTOR YESID")])
    hallazgo = personal_duplicado(con)

    assert hallazgo.cantidad == 1


def test_personal_duplicado_ignora_cuentas_genericas():
    """`temporal5`/`temporal6` dan similitud 0,89 y son cuentas distintas.

    Es el falso positivo que motivó el mínimo de dos tokens: un nombre de
    persona tiene nombre y apellido; una cuenta genérica, no.
    """
    con = _con_personal([("1", "temporal5"), ("2", "temporal6")])

    assert personal_duplicado(con).cantidad == 0


def test_personal_duplicado_no_marca_personas_distintas():
    """Dos personas sin parecido no se agrupan — el detector debe callar."""
    con = _con_personal([("1", "MARIA FERNANDA GOMEZ"), ("2", "CARLOS ANDRES PEREZ")])

    assert personal_duplicado(con).cantidad == 0


def test_personal_duplicado_agrupa_tres_variantes_como_un_caso():
    """Tres grafías de una persona son un caso, no tres pares."""
    con = _con_personal(
        [
            ("1", "WILFRIDO ACEVEDO FLOREZ"),
            ("2", "WILFRIDO ACEVEDO FLORES"),
            ("3", "WILFRIDO ACEVEDO FLORES GOMEZ"),
        ]
    )
    hallazgo = personal_duplicado(con)

    assert hallazgo.cantidad == 1
    assert len(hallazgo.filas) == 3


def test_personal_duplicado_separa_alistadores_multivaluados():
    """El alistador viene separado por comas: cada persona cuenta aparte."""
    con = _con_personal([("1", "ANA MARIA SOTO, LUIS FELIPE RUIZ"), ("2", "ANA MARIA SOTOS")])
    hallazgo = personal_duplicado(con)

    assert hallazgo.cantidad == 1
    # LUIS FELIPE RUIZ no se parece a nadie y queda fuera del detalle.
    assert "LUIS FELIPE RUIZ" not in set(hallazgo.filas["Nombre como figura"])


def test_personal_duplicado_ignora_placeholder():
    """El guion es el placeholder de 'sin asignar', no una persona."""
    con = _con_personal([("1", "-"), ("2", "-")])

    assert personal_duplicado(con).cantidad == 0
