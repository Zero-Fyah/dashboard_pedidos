"""Pasada de mantenimiento — DEC-092.

Recupera la información de entrega mientras el origen todavía la muestra. La
ventana es de ~6 meses y avanza un día por día (DEC-091): lo que no se capture
ahí no se recupera nunca, y ya hay 3.517 pedidos perdidos.

En producción el selector devuelve 0 hoy —marzo a junio están completos y
julio es demasiado reciente— así que **el comportamiento se fija acá**: un
detector que devuelve cero no prueba que funcione.
"""

from datetime import date, timedelta

import aiosqlite
import pytest

from scraper.orquestador import (
    marcar_para_recuperacion,
    obtener_ids_para_recuperar,
    ventana_mantenimiento,
)


async def _crear(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE pedidos (id_pedido TEXT PRIMARY KEY, fecha TEXT, "
            "despachador TEXT, hora_entrega TEXT, obs_entrega TEXT, "
            "scraping_completo INTEGER DEFAULT 1)"
        )
        await db.execute("CREATE TABLE subpedidos (id_pedido TEXT, estado TEXT)")
        await db.execute("CREATE TABLE timeline_pedido (id_pedido TEXT, titulo TEXT)")
        await db.commit()


async def _pedido(
    db_path: str,
    pid: str,
    *,
    dias: int,
    entregado: bool = True,
    cerrado: bool = True,
    hora_entrega: str = "",
    despachador: str = "",
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO pedidos (id_pedido, fecha, despachador, hora_entrega, obs_entrega) "
            "VALUES (?, DATE('now', '-' || ? || ' days'), ?, ?, '')",
            (pid, dias, despachador, hora_entrega),
        )
        await db.execute(
            "INSERT INTO subpedidos VALUES (?, ?)",
            (pid, "completado" if cerrado else "Pendiente de entrega"),
        )
        if entregado:
            await db.execute(
                "INSERT INTO timeline_pedido VALUES (?, 'Recibido y recibido')", (pid,)
            )
        await db.commit()


@pytest.mark.integration
async def test_elige_al_entregado_sin_informacion_de_entrega(tmp_path):
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "SI", dias=60)

    assert await obtener_ids_para_recuperar(db) == ["SI"]


@pytest.mark.integration
async def test_no_elige_al_que_no_se_ha_entregado(tmp_path):
    """Su tarjeta está vacía CON RAZÓN: re-scrapearlo traería los mismos
    vacíos y gastaría browser para nada."""
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "NO_ENTREGADO", dias=60, entregado=False)

    assert await obtener_ids_para_recuperar(db) == []


@pytest.mark.integration
async def test_no_elige_al_que_ya_tiene_los_datos(tmp_path):
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "COMPLETO", dias=60, hora_entrega="2026-06-01 08:00 ~ 10:00")

    assert await obtener_ids_para_recuperar(db) == []


@pytest.mark.integration
async def test_no_elige_al_que_sigue_abierto(tmp_path):
    """El carril de activos del incremental ya los visita; duplicarlo sería
    competir por el lock de SQLite para hacer el mismo trabajo dos veces."""
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "ABIERTO", dias=60, cerrado=False)

    assert await obtener_ids_para_recuperar(db) == []


@pytest.mark.integration
async def test_respeta_los_dos_bordes_de_la_ventana(tmp_path):
    """Los bordes se pasan explícitos para no depender del día en que corra
    la suite: con ventana por mes calendario, "hace 5 días" cae dentro o
    fuera según sea 2 o 20 del mes.

    El borde superior importa tanto como el inferior: un pedido demasiado
    viejo ya no tiene tarjeta que leer —808 pedidos de febrero re-extraídos
    al 100% recuperaron cero— y uno demasiado nuevo no se ha entregado.
    """
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "ANTES", dias=200)
    await _pedido(db, "DENTRO", dias=60)
    await _pedido(db, "DESPUES", dias=2)

    hoy = date.today()
    desde = (hoy - timedelta(days=120)).isoformat()
    hasta = (hoy - timedelta(days=20)).isoformat()

    assert await obtener_ids_para_recuperar(db, desde=desde, hasta=hasta) == ["DENTRO"]


@pytest.mark.integration
async def test_marcar_fuerza_el_modo_completo(tmp_path):
    """`determinar_modo()` manda a `completo` solo si scraping_completo=0.
    Es el mismo mecanismo del backfill de DEC-027, no una vía nueva."""
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "A", dias=60)
    await _pedido(db, "B", dias=60, hora_entrega="2026-06-01 08:00 ~ 10:00")

    await marcar_para_recuperacion(db, ["A"])

    async with aiosqlite.connect(db) as con:
        filas = dict(
            await (await con.execute("SELECT id_pedido, scraping_completo FROM pedidos")).fetchall()
        )
    assert filas["A"] == 0
    assert filas["B"] == 1  # no se toca lo que no se eligió


@pytest.mark.integration
async def test_marcar_sin_ids_no_hace_nada(tmp_path):
    db = str(tmp_path / "m.db")
    await _crear(db)
    await marcar_para_recuperacion(db, [])


@pytest.mark.integration
async def test_no_reelige_al_que_el_origen_dejo_de_poblar(tmp_path):
    """La corrida real refutó el primer criterio.

    Con `despachador` como testigo la pasada eligió 504 pedidos, corrió al
    100% y recuperó **2**: el origen dejó de poblar ese campo en julio (44%
    contra el 100% de marzo-junio) y re-leerlos no trae nada. Con
    `hora_entrega` —al 100% en los tres métodos de entrega— un pedido cuya
    tarjeta SÍ se leyó no vuelve a la cola aunque le falten otros campos.
    """
    db = str(tmp_path / "m.db")
    await _crear(db)
    await _pedido(db, "LEIDO_SIN_DESPACHADOR", dias=60, hora_entrega="2026-06-01 08:00 ~ 10:00")

    assert await obtener_ids_para_recuperar(db) == []


# -- La ventana por mes calendario (decisión del Arquitecto) -----------------


def test_cubre_el_mes_recien_cerrado_completo():
    """Corriendo el día 1, el mes anterior queda cubierto de punta a punta."""
    desde, hasta = ventana_mantenimiento(date(2026, 9, 1))

    assert hasta == "2026-08-31"
    assert desde <= "2026-08-01"


def test_no_toca_el_mes_en_curso():
    """Los pedidos del mes corriente son demasiado nuevos para haberse
    entregado: mirarlos gastaría navegador para no encontrar nada."""
    _, hasta = ventana_mantenimiento(date(2026, 9, 15))

    assert hasta == "2026-08-31"


def test_retrocede_para_atrapar_a_los_entregados_tarde():
    """El 20-32% de los pedidos se entrega en un mes posterior al de su fecha
    (media 9,2 días, máximo 79). Sin retroceso, esa cuarta parte de cada mes
    no volvería a mirarse nunca."""
    desde, hasta = ventana_mantenimiento(date(2026, 9, 1))

    assert desde == "2026-04-01"
    assert hasta == "2026-08-31"


def test_cruza_el_cambio_de_año():
    assert ventana_mantenimiento(date(2026, 1, 1)) == ("2025-08-01", "2025-12-31")


def test_respeta_el_ultimo_dia_de_febrero():
    """El fin de mes se deriva restando un día al primero del mes actual, así
    que no hay que saber cuántos días tiene febrero."""
    assert ventana_mantenimiento(date(2026, 3, 1))[1] == "2026-02-28"
