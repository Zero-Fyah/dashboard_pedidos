"""`scripts/preparar_sandbox.py` — el `.env` del sandbox (DEC-166).

Lo que importa fijar: ninguna variable que produzca efectos reales fuera de la
máquina (email, hojas de Google, respaldo remoto) llega al sandbox, y esos
pasos quedan además apagados en el ciclo sin perder los que ya lo estaban.
"""

import pytest

from scripts.preparar_sandbox import env_seguro

pytestmark = pytest.mark.unit

_PRODUCCION = """# credenciales
SCRAPER_USUARIO=usuario
SCRAPER_URL_PEDIDOS=https://origen.example/country/CO/orders
SMTP_HOST=smtp.example.com
SMTP_PASSWORD=secreta
GOOGLE_SHEETS_CREDENTIALS=data/credenciales/cuenta.json
SHEETS_ID_INVENTARIO_ARENA=abc
SHEETS_PESTANA=Nombre
RESPALDO_REMOTO_HOST=otro.example
CICLO_PASOS_APAGADOS=bochica
"""


def test_quita_email_hojas_de_google_y_respaldo_remoto():
    salida = env_seguro(_PRODUCCION)
    for clave in (
        "SMTP_HOST",
        "SMTP_PASSWORD",
        "GOOGLE_SHEETS_CREDENTIALS",
        "SHEETS_ID_INVENTARIO_ARENA",
        "RESPALDO_REMOTO_HOST",
    ):
        assert f"\n{clave}=" not in salida, clave


def test_conserva_lo_demas_y_lo_marca_como_sandbox():
    salida = env_seguro(_PRODUCCION)
    assert salida.startswith("# SANDBOX (DEC-166)")
    assert "SCRAPER_USUARIO=usuario" in salida
    assert "SCRAPER_URL_PEDIDOS=https://origen.example/country/CO/orders" in salida
    assert "SHEETS_PESTANA=Nombre" in salida  # no es credencial ni produce efectos
    assert "# credenciales" in salida


def test_suma_los_pasos_apagados_a_los_que_ya_estaban():
    salida = env_seguro(_PRODUCCION)
    lineas = [x for x in salida.splitlines() if x.startswith("CICLO_PASOS_APAGADOS=")]
    assert lineas == ["CICLO_PASOS_APAGADOS=bochica,sheets,respaldo_remoto"]


def test_sin_pasos_apagados_previos_apaga_los_del_sandbox():
    salida = env_seguro("SCRAPER_USUARIO=x\n")
    assert "CICLO_PASOS_APAGADOS=sheets,respaldo_remoto" in salida
