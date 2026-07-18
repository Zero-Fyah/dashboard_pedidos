"""Cobertura dirigida de las ramas post-refactor de persistencia_worker
(Fase 7, AUD-M10).

Huecos que cierra esta sección, según medición con pytest-cov:
  - Ruta CON DATOS del helper _persistir_secciones_satelite (los tests
    previos solo enviaban secciones vacías).
  - Ramas except/ROLLBACK de con_cantidades, solo_estado y del registro
    de errores.
  - Rama no_encontrado de _actualizar_estado_subpedido.
"""

import copy

import aiosqlite
import pytest

from tests.integration.test_persistencia import (
    _PEDIDO_BASE,
    persistir_uno,
)

_SATELITES = {
    "estadisticas": [
        {
            "id_pedido": "X",
            "orden": 1,
            "concepto": "Total",
            "concepto_tag": "",
            "monto_pagar": "100.000,00",
            "monto_final": "90.000,00",
            "diferencia": "10.000,00",
        }
    ],
    "hay_diferencia": 1,
    "gestion_dif": {
        "total_pagar_pedido": "100.000,00",
        "monto_final_pagar": "90.000,00",
        "monto_pagado": "90.000,00",
        "monto_diferencia": "10.000,00",
    },
    "detalle_dif": [
        {
            "id_pedido": "X",
            "nombre_producto": "Producto Test",
            "especificacion": "Unidad",
            "tipo": "Normal",
            "precio_unitario": "10.000,00",
            "descuento": "0,00",
            "descuento_tipo": "Promoción10%",  # DEC-024
            "precio_descuento": "10.000,00",
            "cantidad_pedido": "10",
            "cantidad_entregada": "9",
            "diferencia_cantidad": "1",
            "monto_pagar_pedido": "100.000,00",
            "monto_final_pagar": "90.000,00",
            "iva": "0,00",
            "monto_diferencia": "10.000,00",
        }
    ],
    "registro_ops": [
        {
            "id_pedido": "X",
            "momento": "2026-05-22 10:00:00",
            "usuario": "operador",
            "tipo_usuario": "staff",
            "accion": "Pedido creado",
            "referencia": "",
        }
    ],
}

_TABLAS_SATELITE = (
    "estadisticas_monto",
    "gestion_diferencias",
    "detalle_diferencias",
    "registro_operaciones",
)


def _con_satelites(base: dict, id_pedido: str) -> dict:
    """Clona un resultado y puebla las 4 secciones satélite con datos."""
    p = copy.deepcopy(base)
    p["id_pedido"] = id_pedido
    if "info_general" in p:
        p["info_general"]["id_pedido"] = id_pedido
    for paso in p.get("timeline", []):
        paso["id_pedido"] = id_pedido
    sat = copy.deepcopy(_SATELITES)
    for fila in sat["estadisticas"] + sat["detalle_dif"] + sat["registro_ops"]:
        fila["id_pedido"] = id_pedido
    p.update(sat)
    return p


def _resultado_liviano(tipo: str, subpedidos: list) -> dict:
    """Esqueleto de resultado con secciones satélite vacías."""
    return {
        "tipo": tipo,
        "id_pedido": "TEST-001",
        "subpedidos": subpedidos,
        "timeline": [],
        "estadisticas": [],
        "hay_diferencia": None,
        "gestion_dif": None,
        "detalle_dif": [],
        "registro_ops": [],
    }


@pytest.mark.integration
async def test_satelites_con_datos_se_persisten_y_reemplazan(db_path):
    """Ruta con datos del helper AUD-M10: inserta las 4 tablas y una segunda
    pasada reemplaza (DELETE + INSERT) sin duplicar — compatible con el
    índice UNIQUE de gestion_diferencias (AUD-B9)."""
    p = _con_satelites(_PEDIDO_BASE, "TEST-SAT")
    await persistir_uno(p, db_path)

    p2 = _con_satelites(_PEDIDO_BASE, "TEST-SAT")
    p2["gestion_dif"]["monto_diferencia"] = "20.000,00"
    await persistir_uno(p2, db_path)

    async with aiosqlite.connect(db_path) as db:
        conteos = {}
        for tabla in _TABLAS_SATELITE:
            conteos[tabla] = (
                await (
                    await db.execute(f"SELECT COUNT(*) FROM {tabla} WHERE id_pedido = 'TEST-SAT'")
                ).fetchone()
            )[0]
        gd = await (
            await db.execute(
                "SELECT monto_diferencia FROM gestion_diferencias WHERE id_pedido = 'TEST-SAT'"
            )
        ).fetchone()
        hd = await (
            await db.execute("SELECT hay_diferencia FROM pedidos WHERE id_pedido = 'TEST-SAT'")
        ).fetchone()

    assert conteos == dict.fromkeys(_TABLAS_SATELITE, 1)
    assert gd[0] == "20.000,00"  # la segunda pasada reemplazó
    assert hd[0] == 1  # FIX C-3: hay_diferencia verificado se escribe


@pytest.mark.integration
async def test_solo_estado_persiste_satelites_y_hay_diferencia(db_path):
    """Rama solo_estado con secciones pobladas (warn=False, ruta con datos)."""
    await persistir_uno(copy.deepcopy(_PEDIDO_BASE), db_path)
    cierre = _con_satelites(
        _resultado_liviano(
            "solo_estado",
            [{"numero_subpedido": "SUB-001", "estado": "completado"}],
        ),
        "TEST-001",
    )
    await persistir_uno(cierre, db_path)

    async with aiosqlite.connect(db_path) as db:
        n_ops = (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM registro_operaciones WHERE id_pedido = 'TEST-001'"
                )
            ).fetchone()
        )[0]
        hd = (
            await (
                await db.execute("SELECT hay_diferencia FROM pedidos WHERE id_pedido = 'TEST-001'")
            ).fetchone()
        )[0]
    assert n_ops == 1
    assert hd == 1


@pytest.mark.integration
async def test_solo_estado_subpedido_inexistente_loggea_warning(db_path, capsys):
    """_actualizar_estado_subpedido: rama no_encontrado — WARNING sin abortar
    la transacción del pedido."""
    await persistir_uno(copy.deepcopy(_PEDIDO_BASE), db_path)
    run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}
    fantasma = _resultado_liviano(
        "solo_estado",
        [{"numero_subpedido": "SUB-NOEXISTE", "estado": "completado"}],
    )
    await persistir_uno(fantasma, db_path, run_stats)
    assert run_stats["ok"] == {"TEST-001"}  # el pedido no falla por esto
    assert "subpedido_no_encontrado" in capsys.readouterr().out


@pytest.mark.integration
async def test_con_cantidades_malformado_hace_rollback(db_path):
    """Rama except de con_cantidades: un resultado sin la clave 'estado'
    revienta dentro de la transacción -> ROLLBACK + run_stats error."""
    await persistir_uno(copy.deepcopy(_PEDIDO_BASE), db_path)
    run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}
    roto = _resultado_liviano(
        "con_cantidades",
        [{"numero_subpedido": "SUB-001", "lineas": []}],  # sin 'estado'
    )
    await persistir_uno(roto, db_path, run_stats)
    assert run_stats["error"] == {"TEST-001"}
    assert run_stats["ok"] == set()


@pytest.mark.integration
async def test_solo_estado_malformado_hace_rollback(db_path):
    """Rama except de solo_estado: subpedidos sin 'numero_subpedido'."""
    await persistir_uno(copy.deepcopy(_PEDIDO_BASE), db_path)
    run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}
    roto = _resultado_liviano("solo_estado", [{"estado": "completado"}])
    await persistir_uno(roto, db_path, run_stats)
    assert run_stats["error"] == {"TEST-001"}


@pytest.mark.integration
async def test_con_cantidades_feliz_con_timeline_y_sin_match(db_path, capsys):
    """Rama con_cantidades completa: reemplaza timeline, refresca run_stats
    tras COMMIT, y un codigo_barras sin correspondencia loggea
    update_sin_match sin abortar (el test histórico usaba codigo_barras=''
    que SÍ matchea contra el valor vacío almacenado)."""
    await persistir_uno(copy.deepcopy(_PEDIDO_BASE), db_path)
    run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}
    p = _resultado_liviano(
        "con_cantidades",
        [
            {
                "numero_subpedido": "SUB-001",
                "estado": "completado",
                "lineas": [{"codigo_barras": "NO-EXISTE-999", "cantidad_entregada": 5.0}],
            }
        ],
    )
    p["timeline"] = [
        {
            "id_pedido": "TEST-001",
            "paso": 1,
            "titulo": "Enviado",
            "fecha_hora": "2026-05-23 08:00:00",
            "completado": 1,
        }
    ]
    await persistir_uno(p, db_path, run_stats)

    assert run_stats["ok"] == {"TEST-001"}
    assert "update_sin_match" in capsys.readouterr().out
    async with aiosqlite.connect(db_path) as db:
        titulo = (
            await (
                await db.execute("SELECT titulo FROM timeline_pedido WHERE id_pedido = 'TEST-001'")
            ).fetchone()
        )[0]
    assert titulo == "Enviado"  # el timeline fue reemplazado


@pytest.mark.integration
async def test_completo_timeline_vacio_loggea_warning_y_solo_estado_reemplaza(db_path, capsys):
    """timeline vacío en completo -> WARNING timeline_vacio (BUG-012/013);
    solo_estado con timeline poblado -> DELETE + INSERT."""
    p = copy.deepcopy(_PEDIDO_BASE)
    p["timeline"] = []
    await persistir_uno(p, db_path)
    assert "timeline_vacio" in capsys.readouterr().out

    cierre = _resultado_liviano(
        "solo_estado",
        [{"numero_subpedido": "SUB-001", "estado": "completado"}],
    )
    cierre["timeline"] = [
        {
            "id_pedido": "TEST-001",
            "paso": 2,
            "titulo": "Entregado",
            "fecha_hora": "2026-05-24 08:00:00",
            "completado": 1,
        }
    ]
    await persistir_uno(cierre, db_path)
    async with aiosqlite.connect(db_path) as db:
        filas = await (
            await db.execute("SELECT titulo FROM timeline_pedido WHERE id_pedido = 'TEST-001'")
        ).fetchall()
    assert [f[0] for f in filas] == ["Entregado"]


@pytest.mark.integration
async def test_error_sin_detalle_loggea_db_error(db_path, capsys):
    """Rama except del registro de error: un resultado _error sin 'detalle'
    revienta el INSERT en errores -> ROLLBACK + db_error, sin matar el worker."""
    run_stats: dict[str, set[str]] = {"ok": set(), "error": set()}
    await persistir_uno({"id_pedido": "TEST-ERR", "_error": True}, db_path, run_stats)
    assert run_stats["error"] == {"TEST-ERR"}
    assert "db_error" in capsys.readouterr().out
