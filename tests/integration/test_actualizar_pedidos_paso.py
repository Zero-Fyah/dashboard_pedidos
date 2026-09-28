"""`paso()` de scraper/actualizar_pedidos.sh (auditoría 2026-09-25, DEC-146).

Se extrae la definición REAL de la función del script y se ejecuta con bash:
cada paso deja inicio, código de salida y duración en el log; un fallo marca
FALLO=1 y el nombre del paso, sin cortar la cadena.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scraper" / "actualizar_pedidos.sh"


def _definicion_paso() -> str:
    texto = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^paso\(\) \{\n.*?^\}\n", texto, flags=re.S | re.M)
    assert m, "no se encontró la función paso() en actualizar_pedidos.sh"
    return m.group(0)


@pytest.mark.skipif(shutil.which("bash") is None, reason="requiere bash")
def test_paso_registra_rc_y_duracion_y_no_corta_la_cadena(tmp_path):
    log = tmp_path / "ciclo.log"
    guion = f"""
set -uo pipefail
LOGFILE="{log}"
FALLO=0
PASOS_FALLIDOS=""
{_definicion_paso()}
paso uno true
paso dos bash -c 'echo salida-del-paso; exit 3'
paso tres true
echo "FALLO=$FALLO PASOS=[$PASOS_FALLIDOS]"
"""
    r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, check=True)
    contenido = log.read_text(encoding="utf-8")

    assert r.stdout.strip() == "FALLO=1 PASOS=[ dos]"
    assert re.search(r"\[paso\] uno — fin rc=0 en \d+ s", contenido)
    assert re.search(r"\[paso\] dos — fin rc=3 en \d+ s", contenido)
    assert re.search(r"\[paso\] tres — fin rc=0 en \d+ s", contenido)  # la cadena siguió
    assert "salida-del-paso" in contenido  # la salida del paso va al log


def test_todos_los_pasos_del_ciclo_pasan_por_paso():
    """Ningún paso vuelve al patrón viejo `|| FALLO=1`, que no registra
    cuál falló."""
    texto = _SCRIPT.read_text(encoding="utf-8")
    assert "|| FALLO=1" not in texto
    for nombre in ("scraper", "bochica", "sheets", "etl", "respaldo"):
        assert re.search(rf"^\s*paso {nombre} ", texto, flags=re.M), nombre
