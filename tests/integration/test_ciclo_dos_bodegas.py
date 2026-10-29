"""Fase 2 de dos bodegas (DEC-164) de punta a punta, sin red ni origen.

1. `scraper/actualizar_pedidos.sh` real, en una raíz temporal donde
   `.venv/bin/python` es un falso que anota cada invocación (argumentos y
   BODEGA) y deja pasar a `scripts/pasos_bodega.py` al intérprete real, anclado
   a la raíz temporal. Fija el orden de las bodegas, la bodega de cada
   proceso, el modo del scraper, los pasos apagados y el aviso de fallo.
2. `inventario.persistencia.main()` con `BODEGA=bodega2` en un subproceso,
   sobre Excel y base de prueba: el perfil sin layout persiste el picking
   estimado por ubicaciones de reserva y los cálculos que no dependen del
   layout, con el almacén de esta bodega.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import comun

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash"),
]

_RAIZ_REAL = Path(comun.__file__).resolve().parent.parent

# Python falso del ciclo. Fallar a propósito: FALLAR="<bodega>:<modulo>".
_PYTHON_FALSO = r"""#!/bin/bash
if [ "$1" = "scripts/pasos_bodega.py" ]; then
    # PYTHONPATH: el código bajo prueba, no el de la instalación editable.
    PYTHONPATH="{raiz_real}" exec "{python}" -c '
import runpy, sys
from pathlib import Path
import comun
comun._RAIZ_PROYECTO = Path(sys.argv[1])
script, sys.argv = sys.argv[3], ["pasos_bodega.py", sys.argv[2]]
runpy.run_path(script, run_name="__main__")
' "{raiz}" "$2" "{raiz_real}/scripts/pasos_bodega.py"
fi
echo "BODEGA=${{BODEGA:-}} $*" >> "{raiz}/llamadas.txt"
case "${{FALLAR:-}}" in
    "${{BODEGA:-}}:"*) [[ " $* " == *" ${{FALLAR#*:}} "* ]] && exit 1 ;;
esac
exit 0
"""


def _raiz_de_prueba(tmp_path: Path, env_hist: str, env_b2: str | None) -> Path:
    raiz = tmp_path / "raiz"
    (raiz / "scraper").mkdir(parents=True)
    (raiz / "scripts").mkdir()
    (raiz / "logs").mkdir()
    (raiz / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(_RAIZ_REAL / "scraper" / "actualizar_pedidos.sh", raiz / "scraper")
    falso = raiz / ".venv" / "bin" / "python"
    falso.write_text(_PYTHON_FALSO.format(python=sys.executable, raiz=raiz, raiz_real=_RAIZ_REAL))
    falso.chmod(0o755)
    aviso = raiz / "scripts" / "notificar_fallo_scheduler.sh"
    aviso.write_text(f'#!/bin/bash\necho "AVISO notify-send $*" >> "{raiz}/llamadas.txt"\n')
    aviso.chmod(0o755)
    (raiz / ".env").write_text(env_hist)
    if env_b2 is not None:
        (raiz / ".env.bodega2").write_text(env_b2)
    return raiz


def _correr_ciclo(raiz: Path, **extra: str) -> tuple[list[str], str]:
    entorno = {"PATH": os.environ["PATH"], "HOME": str(raiz), **extra}
    subprocess.run(
        ["bash", str(raiz / "scraper" / "actualizar_pedidos.sh")],
        env=entorno,
        check=True,
        timeout=120,
    )
    llamadas = (raiz / "llamadas.txt").read_text().splitlines()
    log = next((raiz / "logs").glob("scraper_scheduler_*.log")).read_text()
    return llamadas, log


def _de(llamadas: list[str], bodega: str) -> list[str]:
    return [linea for linea in llamadas if linea.startswith(f"BODEGA={bodega} ")]


def test_corre_primero_bodega2_y_despues_la_historica_cada_una_con_su_bodega(tmp_path):
    raiz = _raiz_de_prueba(tmp_path, "", "CICLO_ACTIVO=1\n")
    llamadas, log = _correr_ciclo(raiz)

    b2, hist = _de(llamadas, "bodega2"), _de(llamadas, "miempresa")
    assert len(b2) == len(hist) == 10  # 11 pasos menos el mantenimiento (solo día 1)
    assert llamadas.index(b2[-1]) < llamadas.index(hist[0])  # en secuencia, nunca mezcladas
    assert len(b2) + len(hist) == len(llamadas)  # ningún proceso sin BODEGA explícita
    assert "scraper/scraper_principal.py --modo incremental" in b2[0]
    assert "scraper/scraper_principal.py --modo cierre" in hist[0]
    assert "[bodega] bodega2 — fin" in log and "[bodega] miempresa — fin" in log
    assert "[ciclo] fin — pasos con fallo: ninguno" in log
    assert not any("AVISO" in linea or "notificar" in linea for linea in llamadas)


def test_sin_su_archivo_bodega2_no_corre(tmp_path):
    raiz = _raiz_de_prueba(tmp_path, "", None)
    llamadas, log = _correr_ciclo(raiz)

    assert _de(llamadas, "bodega2") == []
    assert len(_de(llamadas, "miempresa")) == 10
    assert "[bodega] bodega2 — inactiva (CICLO_ACTIVO), no corre" in log


def test_con_su_archivo_bodega2_tampoco_corre_hasta_activarla(tmp_path):
    raiz = _raiz_de_prueba(tmp_path, "", "SCRAPER_PAIS=CA\n")
    llamadas, _ = _correr_ciclo(raiz)
    assert _de(llamadas, "bodega2") == []


def test_los_pasos_apagados_no_corren_y_quedan_en_el_log(tmp_path):
    raiz = _raiz_de_prueba(
        tmp_path,
        "SCRAPER_MODO_CICLO=incremental\nCICLO_PASOS_APAGADOS=bochica,sheets\n",
        "CICLO_ACTIVO=1\n",
    )
    llamadas, log = _correr_ciclo(raiz)

    hist = _de(llamadas, "miempresa")
    assert not any("scraper.bochica" in linea or "sheets_cliente" in linea for linea in hist)
    assert "scraper/scraper_principal.py --modo incremental" in hist[0]
    assert "[paso] miempresa/sheets — apagado" in log
    assert any("sheets_cliente" in linea for linea in _de(llamadas, "bodega2"))


def test_un_paso_que_falla_nombra_su_bodega_y_dispara_el_aviso(tmp_path):
    raiz = _raiz_de_prueba(tmp_path, "", "CICLO_ACTIVO=1\n")
    llamadas, log = _correr_ciclo(raiz, FALLAR="bodega2:integraciones.sheets_cliente")

    assert "[bodega] bodega2 — fin" in log
    assert "pasos con fallo: bodega2/sheets\n" in log.split("[bodega] miempresa — inicio")[0]
    assert "[bodega] miempresa — fin" in log  # la otra bodega corrió igual
    assert "[ciclo] fin — pasos con fallo: bodega2/sheets" in log
    assert any("notificar_fallo_email.py" in linea for linea in llamadas)


def test_una_configuracion_invalida_salta_esa_bodega_y_avisa(tmp_path):
    raiz = _raiz_de_prueba(tmp_path, "CICLO_PASOS_APAGADOS=shets\n", "CICLO_ACTIVO=1\n")
    llamadas, log = _correr_ciclo(raiz)

    assert _de(llamadas, "miempresa") == []
    assert len(_de(llamadas, "bodega2")) == 10
    assert "[bodega] miempresa — configuración inválida, no corre" in log
    assert "[ciclo] fin — pasos con fallo: miempresa/configuracion" in log
    assert any("notificar_fallo_email.py" in linea for linea in llamadas)


# ─────────────────────────────────────────────
# Perfil reducido de inventario.persistencia para bodega2
# ─────────────────────────────────────────────

_SCRIPT_PERSISTENCIA = r"""
import json, sqlite3, sys
from pathlib import Path
import pandas as pd
import comun
comun._RAIZ_PROYECTO = Path(sys.argv[1])
from comun import ESTADOS_ACTIVOS_INVENTARIO, get_db_path
import inventario.persistencia as per
from scraper.bochica import DESTINO_DEFAULT as BOCHICA_XLSX
from scraper.inventario import DESTINO_DEFAULT as ADMIN_XLSX

def fila_admin(id_, ref, almacen, categoria, inventario):
    return {"Identificación del producto": "P" + id_, "ID de especificación": id_,
            "Nombre comercial": ref, "Inventario": inventario, "Referencia del producto": ref,
            "ALMACEN": almacen, "Categoria del producto": categoria, "Especificación": ref,
            "Código de barras": "770" + id_, "Peso": 1, "Precio": 1000,
            "Existencias restantes": None, "Producto activo o inactivo": "Fue",
            "Descuento": 0, "IVA": 0}

ADMIN_XLSX.parent.mkdir(parents=True)
pd.DataFrame([
    fila_admin("1", "PC1", "Bogota", "Juguetes y entretenimiento", 60),
    fila_admin("2", "PC1", "Bogota", "Juguetes y entretenimiento", 40),
    fila_admin("3", "PC2", "Bogotá", "Juguetes y entretenimiento", 999),  # grafía de la otra bodega
    fila_admin("4", "PRA13", "Medellin", "Arena", 25),
    fila_admin("5", "PC3", "Bogota", "Juguetes y entretenimiento", 0),  # llegó, sin habilitar
]).to_excel(ADMIN_XLSX, index=False)
pd.DataFrame([
    {"ID Producto": "1", "Referencia": "PC1", "Especificación": "", "Ubicación": "P_1_4",
     "BL": "", "Cantidad": 50, "Lote": "", "Fecha Vencimiento": ""},
    {"ID Producto": "2", "Referencia": "PC1", "Especificación": "", "Ubicación": "H_2_4",
     "BL": "", "Cantidad": 7, "Lote": "", "Fecha Vencimiento": ""},
    {"ID Producto": "1", "Referencia": "PC1", "Especificación": "", "Ubicación": "G_1_2",
     "BL": "", "Cantidad": 20, "Lote": "", "Fecha Vencimiento": ""},
    {"ID Producto": "5", "Referencia": "PC3", "Especificación": "", "Ubicación": "P1_1_1",
     "BL": "", "Cantidad": 500, "Lote": "", "Fecha Vencimiento": ""},
]).to_excel(BOCHICA_XLSX, index=False)

import asyncio
from scraper.db import init_db
asyncio.run(init_db(get_db_path()))  # esquema real del scraper; la base nace marcada como bodega2
con = sqlite3.connect(get_db_path())
con.execute("INSERT INTO pedidos (id_pedido, fecha, hora, scraping_completo) VALUES ('TEST-1', date('now', '-1 day'), '10:00:00', 1)")
con.execute("INSERT INTO subpedidos (id_pedido, numero_subpedido, estado, inicio_inspeccion) VALUES ('TEST-1', 'S1', ?, '-')", (ESTADOS_ACTIVOS_INVENTARIO[0],))
con.execute("INSERT INTO lineas_pedido (id_pedido, numero_subpedido, referencia, almacen, codigo_barras, cantidad_comprada) VALUES ('TEST-1', 'S1', 'PC1', 'Bogota', '7701', 30)")
con.commit(); con.close()
# Paso de activación (DEC-164): el ETL corre una vez tras la carga inicial,
# porque ABC-XYZ y salud leen columnas `_num` que crea el ETL.
import etl.etl_principal as etl
assert asyncio.run(etl.main()) == 0

rc = per.main()
con = sqlite3.connect(get_db_path())
comp = pd.read_sql("SELECT * FROM inventario_comparacion", con)
salida = {
    "rc": rc,
    "comparacion": comp[["referencia", "inventario_teorico", "bochica_altura", "bochica_picking", "picking_estimado"]].to_dict("records"),
    "arena": pd.read_sql("SELECT almacen, referencia FROM arena_inventario", con).to_dict("records"),
    "catalogo": int(con.execute("SELECT COUNT(*) FROM catalogo_productos").fetchone()[0]),
    "corridas": int(con.execute("SELECT COUNT(*) FROM inventario_corridas").fetchone()[0]),
    "salud": pd.read_sql("SELECT referencia, disponible FROM inventario_salud", con).to_dict("records"),
    "abc": int(con.execute("SELECT COUNT(*) FROM inventario_abc").fetchone()[0]),
    "ubicaciones": int(con.execute("SELECT COUNT(*) FROM inventario_ubicaciones").fetchone()[0]),
    "marca": int(con.execute("PRAGMA application_id").fetchone()[0]),
    "alertas": pd.read_sql("SELECT clave, entidad, detalle, valor FROM alertas", con).to_dict("records"),
}
print(json.dumps(salida))
"""


def test_bodega2_persiste_su_picking_estimado_por_reservas(tmp_path):
    entorno = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(_RAIZ_REAL),
        "BODEGA": "bodega2",
    }
    r = subprocess.run(
        [sys.executable, "-c", _SCRIPT_PERSISTENCIA, str(tmp_path)],
        env=entorno,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    salida = json.loads(r.stdout.strip().splitlines()[-1])

    assert salida["rc"] == 0
    # PC1: disponible 100 (solo "Bogota") + 30 previos al picking − 50 en la
    # reserva P_1_4. H_2_4 (7) y G_1_2 (20) cuentan como picking. PC2 queda
    # fuera: "Bogotá" es la grafía de la otra operación. PC3: 500 en el
    # puerto, que cuenta como altura, y 0 disponibles en el sistema.
    assert salida["comparacion"] == [
        {
            "referencia": "PC1",
            "inventario_teorico": 130.0,
            "bochica_altura": 50.0,
            "bochica_picking": 27.0,
            "picking_estimado": 80.0,
        },
        {
            "referencia": "PC3",
            "inventario_teorico": 0.0,
            "bochica_altura": 500.0,
            "bochica_picking": 0.0,
            "picking_estimado": -500.0,
        },
    ]
    # El sobrante es la alerta del Arquitecto: inventario que llegó y no se
    # sumó ni se habilitó para la venta.
    sobrante = [a for a in salida["alertas"] if a["clave"] == "sobrante:total"]
    assert len(sobrante) == 1 and sobrante[0]["valor"] == 500
    assert sobrante[0]["entidad"] == "Alturas y puerto"
    assert "no se sumaron ni se habilitaron para la venta" in sobrante[0]["detalle"]
    assert salida["arena"] == [{"almacen": "Medellin", "referencia": "PRA13"}]
    assert salida["catalogo"] > 0
    assert salida["corridas"] == 1
    # DEC-164: los cálculos que no dependen del layout corren, con el almacén
    # de esta bodega; los que dependen del layout, no.
    assert salida["salud"] == [
        {"referencia": "PC1", "disponible": 100.0},
        {"referencia": "PC3", "disponible": 0.0},
    ]
    assert salida["abc"] > 0
    assert salida["ubicaciones"] == 0
    assert salida["marca"] == comun.MARCA_BODEGA["bodega2"]
