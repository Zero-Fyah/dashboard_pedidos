"""`notificar_fallo_email.py` — aviso por email del ciclo, best-effort (DEC-161).

Sin red: un `smtplib.SMTP` falso registra qué se hubiera enviado. Lo que
importa fijar: sin las 4 variables requeridas queda en no-op explicado
(nunca lanza), y un fallo de SMTP tampoco se propaga — el aviso que falla
no debe sumar un fallo al ciclo que lo dispara.
"""

import smtplib

import pytest

import scripts.notificar_fallo_email as nfe

pytestmark = pytest.mark.unit

_VARS = {
    "SMTP_HOST": "smtp.ejemplo.com",
    "SMTP_USUARIO": "bot@ejemplo.com",
    "SMTP_PASSWORD": "clave",
    "SMTP_DESTINATARIO": "arquitecto@ejemplo.com",
}


class _SMTPFalso:
    """Sustituye a smtplib.SMTP — sin red."""

    instancias: list["_SMTPFalso"] = []
    falla_en: str | None = None  # "connect" | "login" | "send"

    def __init__(self, host, puerto, timeout=None):
        if self.falla_en == "connect":
            raise OSError("conexión rechazada")
        self.host, self.puerto, self.timeout = host, puerto, timeout
        self.llamadas: list[str] = []
        self.mensaje = None
        _SMTPFalso.instancias.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.llamadas.append("starttls")

    def login(self, usuario, password):
        if self.falla_en == "login":
            raise smtplib.SMTPAuthenticationError(535, b"credenciales invalidas")
        self.llamadas.append(f"login:{usuario}:{password}")

    def send_message(self, msg):
        if self.falla_en == "send":
            raise smtplib.SMTPServerDisconnected("desconectado a mitad de envío")
        self.llamadas.append("send")
        self.mensaje = msg


@pytest.fixture(autouse=True)
def _smtp_falso(monkeypatch):
    _SMTPFalso.instancias = []
    _SMTPFalso.falla_en = None
    monkeypatch.setattr(nfe.smtplib, "SMTP", _SMTPFalso)
    yield
    _SMTPFalso.falla_en = None


def _log_con_fallo(tmp_path):
    ruta = tmp_path / "scraper_scheduler_2026-09-28.log"
    ruta.write_text(
        '{"ts": "...", "event": "pedido_ok"}\n'
        "[paso] scraper — fin rc=0 en 1200 s\n"
        "[paso] sheets — fin rc=1 en 12 s\n"
        "[ciclo] fin — pasos con fallo: sheets\n"
    )
    return ruta


@pytest.mark.parametrize("faltante", list(_VARS))
def test_sin_una_variable_requerida_no_intenta_enviar(monkeypatch, tmp_path, faltante):
    for k, v in _VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(faltante, raising=False)

    assert nfe.enviar(_log_con_fallo(tmp_path)) is False
    assert _SMTPFalso.instancias == []


def test_con_todas_las_variables_envia_por_smtp(monkeypatch, tmp_path):
    for k, v in _VARS.items():
        monkeypatch.setenv(k, v)

    assert nfe.enviar(_log_con_fallo(tmp_path)) is True

    (smtp,) = _SMTPFalso.instancias
    assert smtp.host == "smtp.ejemplo.com"
    assert smtp.puerto == 587  # default
    assert "starttls" in smtp.llamadas
    assert "login:bot@ejemplo.com:clave" in smtp.llamadas
    assert "send" in smtp.llamadas
    assert smtp.mensaje["To"] == "arquitecto@ejemplo.com"
    assert smtp.mensaje["From"] == "bot@ejemplo.com"  # sin SMTP_REMITENTE, cae al usuario
    assert "sheets" in smtp.mensaje.get_content()


def test_remitente_explicito_se_respeta(monkeypatch, tmp_path):
    for k, v in _VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SMTP_REMITENTE", "no-responder@ejemplo.com")

    nfe.enviar(_log_con_fallo(tmp_path))

    assert _SMTPFalso.instancias[0].mensaje["From"] == "no-responder@ejemplo.com"


def test_puerto_personalizado_se_usa(monkeypatch, tmp_path):
    for k, v in _VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SMTP_PORT", "465")

    nfe.enviar(_log_con_fallo(tmp_path))

    assert _SMTPFalso.instancias[0].puerto == 465


@pytest.mark.parametrize("etapa", ["connect", "login", "send"])
def test_un_fallo_de_smtp_no_se_propaga(monkeypatch, tmp_path, etapa):
    for k, v in _VARS.items():
        monkeypatch.setenv(k, v)
    _SMTPFalso.falla_en = etapa

    assert nfe.enviar(_log_con_fallo(tmp_path)) is False  # no lanza


def test_resumen_del_log_solo_trae_lineas_de_paso_y_ciclo(tmp_path):
    ruta = _log_con_fallo(tmp_path)
    resumen = nfe._resumen_del_log(ruta)
    assert '"event": "pedido_ok"' not in resumen
    assert "[ciclo] fin — pasos con fallo: sheets" in resumen


def test_resumen_del_log_archivo_inexistente_no_lanza(tmp_path):
    resumen = nfe._resumen_del_log(tmp_path / "no-existe.log")
    assert "no se pudo leer" in resumen


def test_main_sin_argumento_devuelve_exit_2(monkeypatch, capsys):
    monkeypatch.setattr(nfe.sys, "argv", ["notificar_fallo_email.py"])
    assert nfe.main() == 2


def test_main_siempre_devuelve_0_aunque_no_se_pueda_enviar(monkeypatch, tmp_path):
    """Contrato con actualizar_pedidos.sh: este paso nunca debe fallar."""
    monkeypatch.setattr(nfe.sys, "argv", ["notificar_fallo_email.py", str(tmp_path / "x.log")])
    for k in _VARS:
        monkeypatch.delenv(k, raising=False)
    assert nfe.main() == 0
