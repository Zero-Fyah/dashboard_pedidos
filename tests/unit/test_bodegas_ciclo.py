"""Fase 2 de dos bodegas (DEC-164): el ciclo, el modo cierre y el cruce de `bodega2`.

Funciones puras y clientes falsos, sin SQLite ni red. El script del ciclo y
el perfil reducido de inventario de punta a punta están en
`tests/integration/test_ciclo_dos_bodegas.py`.
"""

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

import scraper.bochica as bochica
import scraper.config as cfg
import scripts.notificar_fallo_email as nfe
from comun import (
    ALMACEN_DE_BODEGA,
    ALTURAS_BODEGA2,
    ARENA_CIUDADES,
    PUERTO_BODEGA2,
    RESERVAS_BODEGA2,
)
from integraciones import sheets_arena
from integraciones import sheets_cliente as sc
from inventario.comparacion import comparar
from inventario.layout import TIPO_ALTURA, TIPO_PICKING, clasificar_por_reservas
from scraper.orquestador import build_arg_parser, turno_de_cierre
from scripts.pasos_bodega import PASOS, ConfiguracionInvalidaError, configuracion

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────
# Configuración del ciclo por bodega (scripts/pasos_bodega.py)
# ─────────────────────────────────────────────


def test_sin_configuracion_la_historica_corre_en_cierre_y_la_nueva_no_corre():
    """La nueva no puede arrancar sola antes de su carga inicial: sin
    watermark, el carril de nuevos solo mira 7 días."""
    assert configuracion("miempresa", {}) == {"activo": "1", "modo": "cierre", "apagados": ""}
    assert configuracion("bodega2", {}) == {"activo": "0", "modo": "incremental", "apagados": ""}


def test_la_configuracion_del_archivo_prevalece_sobre_los_defectos():
    conf = configuracion(
        "bodega2",
        {
            "CICLO_ACTIVO": "1",
            "SCRAPER_MODO_CICLO": "cierre",
            "CICLO_PASOS_APAGADOS": "sheets, bochica sheets",
        },
    )
    assert conf == {"activo": "1", "modo": "cierre", "apagados": "sheets bochica"}


def test_un_valor_vacio_toma_el_defecto():
    assert configuracion("miempresa", {"CICLO_ACTIVO": "", "SCRAPER_MODO_CICLO": " "}) == {
        "activo": "1",
        "modo": "cierre",
        "apagados": "",
    }


@pytest.mark.parametrize(
    "valores",
    [
        {"CICLO_ACTIVO": "si"},
        {"SCRAPER_MODO_CICLO": "completo"},
        {"CICLO_PASOS_APAGADOS": "sheet"},  # mal escrito: no puede quedar encendido en silencio
        {"CICLO_PASOS_APAGADOS": "sheets; rm -rf /"},
    ],
)
def test_un_valor_invalido_detiene_la_bodega(valores):
    with pytest.raises(ConfiguracionInvalidaError):
        configuracion("miempresa", valores)


def test_los_pasos_conocidos_son_los_del_script_del_ciclo():
    from pathlib import Path

    script = (Path(cfg.__file__).parent / "actualizar_pedidos.sh").read_text(encoding="utf-8")
    for paso in PASOS:
        assert f"paso_bodega {paso} " in script, paso


# ─────────────────────────────────────────────
# Modo cierre (scraper/orquestador.py)
# ─────────────────────────────────────────────

_IDS = [str(2100000000 + i) for i in range(30)] + ["TEST-X", "TEST-Y"]


def test_los_turnos_reparten_todos_los_activos_sin_repetir():
    turnos = [turno_de_cierre(_IDS, 3, hora) for hora in range(3)]
    juntos = [i for t in turnos for i in t]
    assert sorted(juntos) == sorted(_IDS)
    assert len(juntos) == len(set(juntos))


def test_cada_pedido_vuelve_cada_n_horas():
    for hora in range(24):
        assert turno_de_cierre(_IDS, 3, hora) == turno_de_cierre(_IDS, 3, hora + 3)


def test_un_turno_o_menos_devuelve_todos():
    assert turno_de_cierre(_IDS, 1, 7) == _IDS
    assert turno_de_cierre(_IDS, 0, 7) == _IDS


def test_el_turno_conserva_el_orden():
    turno = turno_de_cierre(_IDS, 4, 2)
    assert turno == [i for i in _IDS if i in set(turno)]


def test_el_cli_acepta_el_modo_cierre():
    assert build_arg_parser().parse_args(["--modo", "cierre"]).modo == "cierre"


# ─────────────────────────────────────────────
# Picking estimado de bodega2 (DEC-164, respuesta 1)
# ─────────────────────────────────────────────


def _bochica(filas):
    return pd.DataFrame(filas, columns=["id_especificacion", "ubicacion", "cantidad"])


def test_las_reservas_y_el_puerto_cuentan_como_altura():
    clasificado = clasificar_por_reservas(
        _bochica([("1", "P_1_4", 5), ("1", "H_2_4", 7), ("1", "P1_1_1", 9)]), ALTURAS_BODEGA2
    )
    # H_2_4 es nivel 4, pero no es reserva de bodega2; P1_1_1 es el puerto:
    # mercancía que llegó y no se almacenó todavía (Arquitecto, 2026-10-28).
    assert clasificado["tipo"].tolist() == [TIPO_ALTURA, TIPO_PICKING, TIPO_ALTURA]


def test_picking_estimado_es_previos_mas_disponible_menos_alturas():
    """picking = pedidos previos al picking + disponible para la venta − alturas."""
    admin = pd.DataFrame(
        {
            "id_especificacion": ["1", "2"],
            "referencia": ["PC1", "PC1"],
            "inventario": [60, 40],
            "categoria": ["Juguetes", "Juguetes"],
        }
    )
    bochica_clasificado = clasificar_por_reservas(
        _bochica([("1", "P_1_4", 50), ("2", "H_2_4", 7), ("1", "G_1_2", 20)]),
        RESERVAS_BODEGA2,
    )
    vendido = pd.DataFrame({"referencia": ["PC1"], "vendido_no_alistado": [30]})

    fila = comparar(admin, bochica_clasificado, vendido).iloc[0]

    assert fila["inventario_teorico"] == 130
    assert fila["bochica_altura"] == 50
    assert fila["picking_estimado"] == 80
    assert fila["bochica_picking"] == 27


def test_las_alturas_son_las_doce_reservas_mas_el_puerto():
    assert len(RESERVAS_BODEGA2) == len(set(RESERVAS_BODEGA2)) == 12
    assert PUERTO_BODEGA2 == ("P1_1_1",)
    assert set(ALTURAS_BODEGA2) == set(RESERVAS_BODEGA2) | set(PUERTO_BODEGA2)


@pytest.mark.parametrize(
    "sin_habilitar,entidad,texto",
    [
        (False, "Altura", "por encima del teórico"),
        (True, "Alturas y puerto", "no se sumaron ni se habilitaron para la venta"),
    ],
)
def test_el_sobrante_de_bodega2_es_inventario_sin_habilitar(sin_habilitar, entidad, texto):
    from inventario.alertas import generar_alertas

    comparacion = pd.DataFrame({"referencia": ["PC1", "PC3"], "sobrante_altura": [-80.0, 500.0]})
    alertas = generar_alertas(
        pd.DataFrame(),
        comparacion,
        pd.DataFrame(),
        None,
        pd.DataFrame(),
        sobrante_sin_habilitar=sin_habilitar,
    )
    fila = alertas[alertas["clave"] == "sobrante:total"].iloc[0]
    assert fila["entidad"] == entidad
    assert texto in fila["detalle"] and "PC3" in fila["detalle"]
    assert fila["valor"] == 500


# ─────────────────────────────────────────────
# Almacén de cada bodega en Arena y en los Sheets
# ─────────────────────────────────────────────


def test_los_sheets_aceptan_el_almacen_de_cada_bodega():
    for almacen in ALMACEN_DE_BODEGA.values():
        assert almacen in sheets_arena.CIUDADES_SHEETS
    assert sheets_arena.CIUDADES_SHEETS[: len(ARENA_CIUDADES)] == ARENA_CIUDADES


# ─────────────────────────────────────────────
# Sheets: bodega2 nunca escribe sin nombre de pestaña
# ─────────────────────────────────────────────


def test_bodega2_sin_pestana_no_escribe(monkeypatch):
    monkeypatch.setenv("BODEGA", "bodega2")
    monkeypatch.delenv("SHEETS_PESTANA", raising=False)

    def _no_debe_conectar(*_a, **_k):
        raise AssertionError("no debe abrir la base ni Google sin pestaña")

    monkeypatch.setattr(sc.sqlite3, "connect", _no_debe_conectar)
    monkeypatch.setattr(sc, "_cliente", _no_debe_conectar)
    with pytest.raises(RuntimeError, match="SHEETS_PESTANA"):
        sc.main()


# ─────────────────────────────────────────────
# BOCHICA: el botón de operación sale del país de la bodega
# ─────────────────────────────────────────────


class _Localizador:
    def __init__(self, registro, selector):
        self.registro, self.selector = registro, selector

    async def fill(self, _valor):
        self.registro.append(("fill", self.selector))

    async def click(self, timeout=None):
        self.registro.append(("click", self.selector))

    async def wait_for(self, state=None, timeout=None):
        self.registro.append(("wait", self.selector))


class _Frame:
    def __init__(self):
        self.registro: list[tuple[str, str]] = []

    def locator(self, selector):
        return _Localizador(self.registro, selector)


@pytest.mark.parametrize("pais", ["CO", "CA"])
def test_bochica_elige_la_operacion_del_pais_de_la_bodega(monkeypatch, pais):
    frame = _Frame()

    async def _frame(*_a, **_k):
        return frame

    monkeypatch.setattr(bochica, "frame_con_selector", _frame)
    monkeypatch.setitem(cfg.CONFIG, "pais", pais)
    monkeypatch.setattr(bochica, "log_event", lambda *a, **k: None)

    asyncio.run(bochica.login_app_bochica(object(), "u", "c"))

    clics = [s for accion, s in frame.registro if accion == "click"]
    assert clics == ["#loginButton", f"#btnPais_{pais}"]


def test_bochica_sin_pais_no_elige_ninguna_operacion(monkeypatch):
    frame = _Frame()

    async def _frame(*_a, **_k):
        return frame

    monkeypatch.setattr(bochica, "frame_con_selector", _frame)
    monkeypatch.setitem(cfg.CONFIG, "pais", "")

    with pytest.raises(RuntimeError, match="SCRAPER_PAIS"):
        asyncio.run(bochica.login_app_bochica(object(), "u", "c"))
    assert frame.registro == []


# ─────────────────────────────────────────────
# Email de fallo: qué bodega falló, y el ciclo completo
# ─────────────────────────────────────────────


def _log_dos_ciclos(tmp_path):
    ruta = tmp_path / "scraper_scheduler_2026-10-28.log"
    ruta.write_text(
        "[ciclo] inicio 2026-10-28 16:00:00\n"
        "[paso] miempresa/etl — fin rc=1 en 9 s\n"
        "[ciclo] fin — pasos con fallo: miempresa/etl\n"
        "[ciclo] inicio 2026-10-28 17:00:00\n"
        "[bodega] bodega2 — inicio, scraper en modo incremental\n"
        + "".join(f"[paso] bodega2/p{i} — fin rc=0 en 1 s\n" for i in range(20))
        + "[paso] bodega2/sheets — fin rc=1 en 4 s\n"
        "[bodega] bodega2 — fin en 300 s — pasos con fallo: bodega2/sheets\n"
        "[bodega] miempresa — inicio, scraper en modo cierre\n"
        '{"event": "pedido_ok"}\n'
        "[bodega] miempresa — fin en 400 s — pasos con fallo: ninguno\n"
        "[ciclo] fin — pasos con fallo: bodega2/sheets\n",
        encoding="utf-8",
    )
    return ruta


def test_el_aviso_nombra_las_bodegas_del_ultimo_ciclo(tmp_path):
    assert nfe.bodegas_con_fallo(_log_dos_ciclos(tmp_path)) == ["bodega2"]


def test_el_resumen_es_el_ultimo_ciclo_completo(tmp_path):
    resumen = nfe._resumen_del_log(_log_dos_ciclos(tmp_path))
    assert resumen.startswith("[ciclo] inicio 2026-10-28 17:00:00")
    assert "miempresa/etl" not in resumen  # del ciclo anterior
    assert "[bodega] bodega2 — inicio" in resumen  # no se corta con 20+ pasos
    assert '"event"' not in resumen


def test_un_log_sin_prefijos_de_bodega_no_nombra_ninguna(tmp_path):
    ruta = tmp_path / "viejo.log"
    ruta.write_text("[paso] sheets — fin rc=1 en 1 s\n[ciclo] fin — pasos con fallo: sheets\n")
    assert nfe.bodegas_con_fallo(ruta) == []
    assert nfe.bodegas_con_fallo(tmp_path / "no-existe.log") == []


# ─────────────────────────────────────────────
# Hallazgos sin layout: los dos detectores de ubicaciones no corren
# ─────────────────────────────────────────────


@pytest.mark.parametrize("con_ubicaciones,esperado", [(True, 2), (False, 0)])
def test_sin_layout_no_corren_los_detectores_de_ubicaciones(monkeypatch, con_ubicaciones, esperado):
    """Sin `inventario_ubicaciones` poblada darían siempre cero casos, y eso
    se leería como «sin problemas» cuando es «no medido» (DEC-164)."""
    import inventario.hallazgos as h

    llamados: list[str] = []
    for nombre in ("productos_en_bodega_sin_precio", "sku_en_bodega_fuera_de_catalogo"):
        monkeypatch.setattr(
            h,
            nombre,
            lambda *_a, _n=nombre: llamados.append(_n) or SimpleNamespace(cantidad=0, clave=_n),
        )
    monkeypatch.setattr(h.logger, "exception", lambda *a, **k: None)  # los demás fallan sin base

    h.detectar_todos(pd.DataFrame(), None, con_ubicaciones=con_ubicaciones)

    assert len(llamados) == esperado


# ─────────────────────────────────────────────
# Movimientos de BOCHICA: el filtro de bodega depende de la operación
# ─────────────────────────────────────────────


def test_el_filtro_de_bodega_de_movimientos_existe_para_cada_bodega():
    """Medido el 2026-10-28: la operación de bodega2 solo ofrece «Todas» (""),
    la histórica ofrece MOSQUERA. Una bodega sin entrada haría fallar el
    import del módulo, no el paso a mitad del ciclo."""
    import scraper.movimientos_bochica as mb
    from comun import BODEGAS

    assert set(mb._BODEGA_MOVIMIENTOS_POR_BODEGA) == set(BODEGAS)
    assert mb._BODEGA_MOVIMIENTOS_POR_BODEGA["miempresa"] == "MOSQUERA"
    assert mb._BODEGA_MOVIMIENTOS_POR_BODEGA["bodega2"] == ""
