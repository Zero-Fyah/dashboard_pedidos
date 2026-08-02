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
    productos_en_bodega_sin_precio,
    referencias_con_espacios,
    sku_en_bodega_fuera_de_catalogo,
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


# ─────────────────────────────────────────────
# productos_en_bodega_sin_precio (DEC-061)
# ─────────────────────────────────────────────


def _con_ubicaciones(filas):
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE inventario_ubicaciones "
        "(ubicacion TEXT, id_especificacion TEXT, cantidad REAL)"
    )
    con.executemany("INSERT INTO inventario_ubicaciones VALUES (?,?,?)", filas)
    return con


def _admin_precios(filas):
    return pd.DataFrame(
        filas, columns=["id_especificacion", "referencia", "nombre_comercial", "precio"]
    )


def test_solo_alerta_lo_que_esta_en_una_posicion():
    """Un ID sin precio y sin existencias es una ficha incompleta de algo
    que no está: no compite por la atención de nadie."""
    admin = _admin_precios(
        [
            ("1", "PA01", "En bodega sin precio", 0),
            ("2", "PA02", "Sin precio y sin stock", 0),
            ("3", "PA03", "Con precio", 5000),
        ]
    )
    con = _con_ubicaciones([("A_1_5", "1", 100.0), ("A_1_5", "3", 50.0)])
    h = productos_en_bodega_sin_precio(admin, con)

    assert h.cantidad == 1
    assert list(h.filas["ID"]) == ["1"]


def test_agrega_posiciones_y_unidades_por_producto():
    admin = _admin_precios([("1", "PA01", "Sin precio", None)])
    con = _con_ubicaciones([("A_1_5", "1", 100.0), ("B_2_6", "1", 44.0)])
    h = productos_en_bodega_sin_precio(admin, con)

    assert h.filas.iloc[0]["Posiciones"] == 2
    assert h.filas.iloc[0]["Unidades"] == 144.0


def test_el_precio_cero_cuenta_como_sin_precio():
    """El catálogo usa 0 y vacío indistintamente para 'no tiene precio'."""
    admin = _admin_precios([("1", "PA01", "Cero", 0), ("2", "PA02", "Nulo", None)])
    con = _con_ubicaciones([("A_1_5", "1", 10.0), ("A_1_5", "2", 10.0)])

    assert productos_en_bodega_sin_precio(admin, con).cantidad == 2


def test_sin_la_tabla_de_ubicaciones_no_explota():
    """Los detectores corren ANTES de que la corrida persista sus tablas:
    en una base nueva `inventario_ubicaciones` todavía no existe."""
    h = productos_en_bodega_sin_precio(
        _admin_precios([("1", "PA01", "x", 0)]), sqlite3.connect(":memory:")
    )

    assert h.cantidad == 0
    assert h.filas.empty


def test_todo_con_precio_no_reporta_nada():
    admin = _admin_precios([("1", "PA01", "Con precio", 9000)])
    con = _con_ubicaciones([("A_1_5", "1", 10.0)])

    assert productos_en_bodega_sin_precio(admin, con).cantidad == 0


def test_cuenta_productos_no_filas_de_catalogo():
    """DEC-064: el admin trae una fila por (producto, almacén) —12 almacenes—
    y el detector contaba filas del merge. Un producto de presencia nacional
    aportaba 12 al total y una sola alta movía la cifra en 12."""
    admin = _admin_precios(
        [("1", "PA01", "Nacional sin precio", 0) for _ in range(12)]
        + [("2", "PA02", "Solo Bogotá sin precio", 0)]
    )
    con = _con_ubicaciones([("A_1_5", "1", 100.0), ("B_2_6", "2", 50.0)])
    h = productos_en_bodega_sin_precio(admin, con)

    assert h.cantidad == 2  # antes: 13
    assert list(h.filas["ID"]) == ["1", "2"]


def test_no_duplica_las_filas_del_detalle_por_almacen():
    """`Posiciones` y `Unidades` vienen del lado de bodega, que es una sola:
    las 12 filas por almacén eran duplicados idénticos en la tabla."""
    admin = _admin_precios([("1", "PA01", "Sin precio", 0)] * 12)
    con = _con_ubicaciones([("A_1_5", "1", 100.0), ("B_2_6", "1", 44.0)])
    h = productos_en_bodega_sin_precio(admin, con)

    assert len(h.filas) == 1
    assert h.filas.iloc[0]["Posiciones"] == 2
    assert h.filas.iloc[0]["Unidades"] == 144.0


def test_con_precio_en_un_solo_almacen_no_esta_sin_precio():
    """Un producto está sin precio solo si NINGUNA de sus filas tiene precio.
    Hoy no hay IDs contradictorios en el catálogo real; el max los aguanta."""
    admin = _admin_precios(
        [
            ("1", "PA01", "Precio en Medellín, cero en el resto", 0),
            ("1", "PA01", "Precio en Medellín, cero en el resto", 7500),
            ("2", "PA02", "Cero en todos", 0),
            ("2", "PA02", "Cero en todos", None),
        ]
    )
    con = _con_ubicaciones([("A_1_5", "1", 10.0), ("A_1_6", "2", 10.0)])
    h = productos_en_bodega_sin_precio(admin, con)

    assert h.cantidad == 1
    assert list(h.filas["ID"]) == ["2"]


# ─────────────────────────────────────────────
# sku_en_bodega_fuera_de_catalogo (DEC-072)
# ─────────────────────────────────────────────


def _con_ubicaciones_ref(filas):
    """filas: (ubicacion, id, referencia, cantidad)."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE inventario_ubicaciones "
        "(ubicacion TEXT, id_especificacion TEXT, referencia TEXT, cantidad REAL)"
    )
    con.executemany("INSERT INTO inventario_ubicaciones VALUES (?,?,?,?)", filas)
    return con


def _admin_ids(ids):
    return pd.DataFrame({"id_especificacion": [str(i) for i in ids]})


def test_reporta_los_id_que_el_catalogo_no_reconoce():
    """DEC-072: el alcance son los ID del admin; lo que queda afuera tiene que
    quedar contado o la exclusión se vuelve invisible."""
    con = _con_ubicaciones_ref([("A_1_5", "1", "PA01", 10.0), ("B_2_6", "999", "M140-A021", 500.0)])
    h = sku_en_bodega_fuera_de_catalogo(_admin_ids(["1"]), con)

    assert h.cantidad == 1
    assert list(h.filas["ID"]) == ["999"]
    assert h.filas.iloc[0]["Unidades"] == 500.0


def test_agrega_posiciones_y_unidades_por_id():
    con = _con_ubicaciones_ref([("A_1_5", "999", "X", 100.0), ("B_2_6", "999", "X", 50.0)])
    h = sku_en_bodega_fuera_de_catalogo(_admin_ids(["1"]), con)

    assert h.filas.iloc[0]["Posiciones"] == 2
    assert h.filas.iloc[0]["Unidades"] == 150.0


def test_si_el_catalogo_los_reconoce_a_todos_no_hay_hallazgo():
    """El detector se apaga solo cuando el admin publica los ID faltantes —
    la mecánica de auto-cierre de DEC-047."""
    con = _con_ubicaciones_ref([("A_1_5", "1", "PA01", 10.0)])

    assert sku_en_bodega_fuera_de_catalogo(_admin_ids(["1", "2"]), con).cantidad == 0


def test_fuera_de_catalogo_sin_la_tabla_de_ubicaciones_no_explota():
    h = sku_en_bodega_fuera_de_catalogo(_admin_ids(["1"]), sqlite3.connect(":memory:"))

    assert h.cantidad == 0
    assert h.filas.empty
