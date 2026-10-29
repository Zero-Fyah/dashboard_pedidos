"""Aislamiento entre bodegas a nivel de proceso (DEC-163).

Las rutas y la configuración se fijan al importar cada módulo, con la
variable BODEGA del proceso; por eso se prueban en un subproceso. Antes de
importar nada, el subproceso ancla `comun._RAIZ_PROYECTO` en un directorio
temporal: los logs que abre el scraper al importarse y los `.env` que se
leen son los del temporal, nunca los del proyecto. El entorno del
subproceso se arma desde cero: no hereda las credenciales reales que el
proceso de pytest pudo haber cargado.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import comun

pytestmark = pytest.mark.integration

_RAIZ_REAL = Path(comun.__file__).resolve().parent.parent

_SCRIPT = r"""
import json, sys
from pathlib import Path
import comun
comun._RAIZ_PROYECTO = Path(sys.argv[1])
modo = sys.argv[2]
if modo == "rutas":
    import scraper.config as cfg
    import scraper.inventario, scraper.bochica, scraper.cambios_inventario
    import scraper.movimientos_bochica as mb
    import inventario.layout, inventario.conteos, inventario.hallazgos, inventario.normalizador
    import dashboard.db, dashboard.tareas_db, dashboard.conteos_io
    import integraciones.sheets_cliente as sc
    import scripts.respaldar_remoto as rr
    salida = {
        "LOG_FILE": cfg.CONFIG["LOG_FILE"],
        "ERRORS_DIR": cfg.CONFIG["ERRORS_DIR"],
        "DEBUG_DIR": cfg.CONFIG["DEBUG_DIR"],
        "RUTA_ENV": cfg.RUTA_ENV,
        "admin_xlsx": scraper.inventario.DESTINO_DEFAULT,
        "bochica_xlsx": scraper.bochica.DESTINO_DEFAULT,
        "bochica_sesion": scraper.bochica.SESION_DEFAULT,
        "cambios_dir": scraper.cambios_inventario.DESTINO_DIR,
        "historico_bochica": mb.CARPETA_HISTORICO_BOCHICA,
        "movimientos_diarios": mb.DESTINO_DIR_DIARIO,
        "layout": inventario.layout.RUTA_LAYOUT_DEFAULT,
        "conteos": inventario.conteos.RUTA_CONTEOS_DEFAULT,
        "variantes_arena": inventario.hallazgos.RUTA_VARIANTES_HISTORICAS_ARENA,
        "nombres_arena": inventario.normalizador.RUTA_NOMBRES_CANONICOS_ARENA,
        "dashboard_db": dashboard.db._ruta_db(None),
        "tareas_db": dashboard.tareas_db._ruta(None),
        "conteos_io": dashboard.conteos_io.carpeta_conteos(),
        "sheets_db": sc.DB_PATH,
        "backups_remoto": rr.CARPETA_BACKUPS,
    }
    salida = {k: str(Path(v).relative_to(comun._RAIZ_PROYECTO)) for k, v in salida.items()}
    salida["respaldo_conteos"] = str(dashboard.conteos_io._carpeta_respaldo(None))
    salida["subcarpeta_remota"] = rr.SUBCARPETA_REMOTA
else:
    import scraper.config as cfg
    salida = {
        "ruta_env": cfg.RUTA_ENV.name,
        "pais": cfg.CONFIG["pais"],
        "url_login": cfg.CONFIG["url_login"],
        "usuario": cfg.USUARIO,
    }
    try:
        cfg.validar_config()
        salida["valida"] = "ok"
    except RuntimeError as exc:
        salida["valida"] = str(exc)
print(json.dumps(salida))
"""

# Rutas fijas anteriores a DEC-163, relativas a la raíz, escritas a mano.
_DE_SIEMPRE = {
    "LOG_FILE": "logs/scraper.log",
    "ERRORS_DIR": "data/errors",
    "DEBUG_DIR": "data/debug",
    "RUTA_ENV": ".env",
    "admin_xlsx": "data/inventario/admin_inventario.xlsx",
    "bochica_xlsx": "data/inventario/bochica_inventario.xlsx",
    "bochica_sesion": "data/sesiones/bochica_storage_state.json",
    "cambios_dir": "data/inventario/cambios_diarios",
    "historico_bochica": "data/inventario/historico_bochica",
    "movimientos_diarios": "data/inventario/movimientos_diarios",
    "layout": "data/inventario/layout_bodega.xlsx",
    "conteos": "data/conteos",
    "variantes_arena": "data/inventario/arena_variantes_historicas.xlsx",
    "nombres_arena": "data/inventario/arena_nombres_canonicos.xlsx",
    "dashboard_db": "data/pedidos.db",
    "tareas_db": "data/tareas.db",
    "conteos_io": "data/conteos",
    "sheets_db": "data/pedidos.db",
    "backups_remoto": "data/backups",
}

_URLS = {
    "SCRAPER_URL_LOGIN": "https://{h}/login",
    "SCRAPER_URL_POST_LOGIN": "https://{h}/platform",
    "SCRAPER_URL_PEDIDOS": "https://{h}/country/{p}/orders/parent-orders",
    "SCRAPER_URL_DETALLE": "https://{h}/country/{p}/orders/parent-orders/detail/",
    "SCRAPER_URL_INVENTARIO": "https://{h}/country/{p}/shop/goods-center",
    "SCRAPER_URL_CAMBIOS_INVENTARIO": "https://{h}/country/{p}/shop/inventory/change",
}


def _archivo_env(ruta: Path, host: str, pais: str, *, con_pais: bool, **extra: str) -> None:
    lineas = {k: v.format(h=host, p=pais) for k, v in _URLS.items()}
    lineas |= {"SCRAPER_USUARIO": f"usuario_{host}", "SCRAPER_PASSWORD": "x"}
    if con_pais:
        lineas["SCRAPER_PAIS"] = pais
    lineas |= extra
    ruta.write_text("".join(f"{k}={v}\n" for k, v in lineas.items()), encoding="utf-8")


def _correr(raiz: Path, modo: str, bodega: str | None, **entorno: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_RAIZ_REAL),
        "PYTHONDONTWRITEBYTECODE": "1",
        **entorno,
    }
    if bodega is not None:
        env["BODEGA"] = bodega
    resultado = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(raiz), modo],
        cwd=_RAIZ_REAL,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    return json.loads(resultado.stdout.strip().splitlines()[-1])


def test_sin_bodega_cada_modulo_conserva_sus_rutas_de_siempre(tmp_path):
    rutas = _correr(tmp_path, "rutas", None)
    assert {k: rutas[k] for k in _DE_SIEMPRE} == _DE_SIEMPRE
    assert rutas["respaldo_conteos"].endswith("/EJPC-RESPALDO/dashboard_pedidos_respaldo/conteos")
    assert rutas["subcarpeta_remota"] == ""


def test_bodega2_solo_comparte_la_sesion_de_bochica_con_la_bodega_por_defecto(tmp_path):
    rutas = _correr(tmp_path, "rutas", "bodega2")
    for clave, de_siempre in _DE_SIEMPRE.items():
        if clave == "RUTA_ENV":
            assert rutas[clave] == ".env.bodega2"
        elif clave == "bochica_sesion":
            # DEC-164: la sesión de Google de BOCHICA es una sola para las dos.
            assert rutas[clave] == de_siempre
        else:
            assert rutas[clave].startswith("data/bodega2/"), clave
            assert rutas[clave] != de_siempre
    assert rutas["respaldo_conteos"].endswith("/dashboard_pedidos_respaldo/bodega2/conteos")
    assert rutas["subcarpeta_remota"] == "bodega2"
    # Lo único que se crea al importar (el log del scraper) cae en su carpeta.
    assert (tmp_path / "data" / "bodega2" / "logs").is_dir()
    assert not (tmp_path / "logs").exists()


def test_bodega2_nunca_cae_al_env_de_la_otra_bodega(tmp_path):
    _archivo_env(tmp_path / ".env", "otra.example", "CO", con_pais=False)
    # Sin .env.bodega2, y con una variable heredada del entorno.
    salida = _correr(tmp_path, "config", "bodega2", SCRAPER_USUARIO="heredado")
    assert salida["ruta_env"] == ".env.bodega2"
    assert salida["url_login"] == ""  # nada del .env
    assert salida["usuario"] == "heredado"
    assert "faltantes" in salida["valida"]
    for variable in ("SCRAPER_USUARIO", "SCRAPER_URL_LOGIN", "SCRAPER_PAIS"):
        assert variable in salida["valida"]  # heredada no cuenta
    assert ".env.bodega2" in salida["valida"]


def test_bodega2_con_su_archivo_prevalece_sobre_el_entorno(tmp_path):
    _archivo_env(tmp_path / ".env", "otra.example", "CO", con_pais=False)
    _archivo_env(tmp_path / ".env.bodega2", "nueva.example", "PE", con_pais=True)
    salida = _correr(
        tmp_path, "config", "bodega2", SCRAPER_URL_LOGIN="https://heredado.example/login"
    )
    assert salida["url_login"] == "https://nueva.example/login"
    assert salida["usuario"] == "usuario_nueva.example"
    assert salida["pais"] == "PE"
    assert salida["valida"] == "ok"


def test_bodega2_con_una_url_de_otro_pais_no_arranca(tmp_path):
    _archivo_env(
        tmp_path / ".env.bodega2",
        "nueva.example",
        "PE",
        con_pais=True,
        SCRAPER_URL_INVENTARIO="https://nueva.example/country/CO/shop/goods-center",
    )
    salida = _correr(tmp_path, "config", "bodega2")
    assert "SCRAPER_URL_INVENTARIO apunta al país CO" in salida["valida"]


def test_bodega_por_defecto_lee_su_env_como_antes(tmp_path):
    _archivo_env(tmp_path / ".env", "otra.example", "CO", con_pais=False)
    _archivo_env(tmp_path / ".env.bodega2", "nueva.example", "PE", con_pais=True)
    salida = _correr(tmp_path, "config", None)
    assert salida["ruta_env"] == ".env"
    assert salida["url_login"] == "https://otra.example/login"
    assert salida["pais"] == "CO"  # sin SCRAPER_PAIS: el de siempre
    assert salida["valida"] == "ok"
