"""`respaldar_remoto.py` — copia del respaldo diario fuera de la máquina,
best-effort (DEC-161).

Sin red ni SSH real: `subprocess.run` queda sustituido. Sin destino
configurado, o si `rsync` falla de cualquier forma, el contrato es el mismo
que `notificar_fallo_email.py`: nunca lanza, `main()` siempre devuelve 0.
"""

import datetime
import subprocess

import pytest

import scripts.respaldar_remoto as rr

pytestmark = pytest.mark.unit

HOY = datetime.date.today().isoformat()


@pytest.fixture
def con_respaldo_de_hoy(tmp_path, monkeypatch):
    """Simula que respaldar_db.py ya corrió hoy — usa la fecha real, sin
    tocar el módulo `datetime` (es un singleton global; parchearlo se
    filtraría a cualquier otro test que corra en el mismo proceso)."""
    carpeta = tmp_path / "backups"
    carpeta.mkdir()
    monkeypatch.setattr(rr, "CARPETA_BACKUPS", carpeta)
    (carpeta / f"pedidos_{HOY}.db").write_text("x")
    (carpeta / f"tareas_{HOY}.db").write_text("y")
    return carpeta


def _resultado(rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


@pytest.mark.parametrize("faltante", ["RESPALDO_REMOTO_HOST", "RESPALDO_REMOTO_RUTA"])
def test_sin_host_o_ruta_no_intenta_copiar(monkeypatch, faltante, con_respaldo_de_hoy):
    variables = {
        "RESPALDO_REMOTO_HOST": "backup.ejemplo.com",
        "RESPALDO_REMOTO_RUTA": "/srv/respaldos",
    }
    for k, v in variables.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(faltante, raising=False)
    llamado = []
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: llamado.append(a) or _resultado())

    assert rr.copiar() is False
    assert llamado == []


def test_sin_respaldo_de_hoy_no_intenta_copiar(monkeypatch, tmp_path):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")
    monkeypatch.setattr(rr, "CARPETA_BACKUPS", tmp_path / "vacia")
    (tmp_path / "vacia").mkdir()
    llamado = []
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: llamado.append(a) or _resultado())

    assert rr.copiar() is False
    assert llamado == []


def test_con_destino_y_respaldo_llama_rsync_con_los_archivos(monkeypatch, con_respaldo_de_hoy):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")
    monkeypatch.setenv("RESPALDO_REMOTO_USUARIO", "zero")
    capturado = {}

    def _run(comando, **kwargs):
        capturado["comando"] = comando
        capturado["kwargs"] = kwargs
        return _resultado(rc=0)

    monkeypatch.setattr(rr.subprocess, "run", _run)

    assert rr.copiar() is True
    comando = capturado["comando"]
    assert comando[0] == "rsync"
    assert comando[-1] == "zero@backup.ejemplo.com:/srv/respaldos"
    assert any(f"pedidos_{HOY}.db" in c for c in comando)
    assert any(f"tareas_{HOY}.db" in c for c in comando)
    assert capturado["kwargs"]["timeout"] == rr._TIMEOUT_S


def test_sin_usuario_el_destino_no_lleva_arroba(monkeypatch, con_respaldo_de_hoy):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")
    monkeypatch.delenv("RESPALDO_REMOTO_USUARIO", raising=False)
    capturado = {}

    def _run(c, **k):
        capturado["comando"] = c
        return _resultado()

    monkeypatch.setattr(rr.subprocess, "run", _run)

    rr.copiar()

    assert capturado["comando"][-1] == "backup.ejemplo.com:/srv/respaldos"


def test_puerto_personalizado_va_en_el_comando_ssh(monkeypatch, con_respaldo_de_hoy):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")
    monkeypatch.setenv("RESPALDO_REMOTO_PUERTO", "2222")
    capturado = {}

    def _run(c, **k):
        capturado["comando"] = c
        return _resultado()

    monkeypatch.setattr(rr.subprocess, "run", _run)

    rr.copiar()

    assert "ssh -p 2222" in capturado["comando"][capturado["comando"].index("-e") + 1]


def test_rsync_con_codigo_de_error_devuelve_false(monkeypatch, con_respaldo_de_hoy):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")
    monkeypatch.setattr(
        rr.subprocess, "run", lambda *a, **k: _resultado(rc=255, stderr="ssh: connect refused")
    )

    assert rr.copiar() is False


@pytest.mark.parametrize(
    "excepcion", [FileNotFoundError("rsync no instalado"), subprocess.TimeoutExpired("rsync", 120)]
)
def test_excepcion_de_subprocess_no_se_propaga(monkeypatch, con_respaldo_de_hoy, excepcion):
    monkeypatch.setenv("RESPALDO_REMOTO_HOST", "backup.ejemplo.com")
    monkeypatch.setenv("RESPALDO_REMOTO_RUTA", "/srv/respaldos")

    def _run(*a, **k):
        raise excepcion

    monkeypatch.setattr(rr.subprocess, "run", _run)

    assert rr.copiar() is False  # no lanza


def test_main_siempre_devuelve_0(monkeypatch):
    for k in ("RESPALDO_REMOTO_HOST", "RESPALDO_REMOTO_RUTA"):
        monkeypatch.delenv(k, raising=False)
    assert rr.main() == 0
