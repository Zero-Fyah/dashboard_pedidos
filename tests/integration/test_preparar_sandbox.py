"""`scripts/preparar_sandbox.py` con git, SQLite y archivos reales (DEC-166).

El riesgo que estas pruebas cubren es uno solo: escribir sobre producción. Se
verifica que el script se niega en el árbol principal de un repositorio real y
acepta su worktree, que la copia de una base no puede ir sobre sí misma, y que
preparar y borrar el sandbox dejan el «árbol principal» de prueba intacto.
"""

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import comun
import scripts.preparar_sandbox as ps
from comun import MARCA_BODEGA

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("git") is None, reason="requiere git"),
]


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=prueba", "-c", "user.email=prueba@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_se_niega_en_el_arbol_principal_y_acepta_un_worktree(tmp_path):
    principal = tmp_path / "repo"
    principal.mkdir()
    _git("init", "-q", cwd=principal)
    (principal / "LEEME").write_text("x")
    _git("add", "LEEME", cwd=principal)
    _git("commit", "-q", "-m", "inicio", cwd=principal)
    _git("worktree", "add", "-q", str(tmp_path / "wt"), cwd=principal)

    with pytest.raises(ps.SandboxInseguroError, match="árbol principal"):
        ps.arbol_principal(principal)
    assert ps.arbol_principal(tmp_path / "wt") == principal.resolve()


def test_se_detiene_si_el_comun_importado_no_es_el_de_su_raiz(tmp_path):
    with pytest.raises(ps.SandboxInseguroError, match="comun"):
        ps.verificar_comun(tmp_path)


def _base(ruta: Path, marca: int = 0) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    if marca:
        con.execute(f"PRAGMA application_id = {marca}")
    con.execute("CREATE TABLE pedidos (id_pedido TEXT)")
    con.execute("INSERT INTO pedidos VALUES ('TEST-1')")
    con.commit()
    con.close()
    return ruta


def test_la_copia_conserva_datos_y_marca_y_nunca_va_sobre_si_misma(tmp_path):
    origen = _base(tmp_path / "a" / "pedidos.db", MARCA_BODEGA["bodega2"])
    destino = tmp_path / "b" / "pedidos.db"
    ps.copiar_base(origen, destino)
    con = sqlite3.connect(destino)
    assert con.execute("PRAGMA application_id").fetchone()[0] == MARCA_BODEGA["bodega2"]
    assert con.execute("SELECT id_pedido FROM pedidos").fetchall() == [("TEST-1",)]
    con.close()
    with pytest.raises(ps.SandboxInseguroError):
        ps.copiar_base(origen, origen)


@pytest.fixture
def produccion_y_sandbox(tmp_path, monkeypatch):
    principal = tmp_path / "produccion"
    _base(principal / "data" / "pedidos.db")
    _base(principal / "data" / "tareas.db")
    _base(principal / "data" / "bodega2" / "pedidos.db", MARCA_BODEGA["bodega2"])
    (principal / ".env").write_text("SCRAPER_USUARIO=u\nSMTP_PASSWORD=secreta\n")
    (principal / ".env.bodega2").write_text("SCRAPER_PAIS=CA\nSHEETS_ID_INVENTARIO_ARENA=x\n")
    (principal / "data" / "inventario").mkdir(parents=True)
    (principal / "data" / "inventario" / "layout_bodega.xlsx").write_bytes(b"layout")
    (principal / "data" / "sesiones").mkdir(parents=True)
    (principal / "data" / "sesiones" / "bochica_storage_state.json").write_text("{}")
    (principal / ".venv" / "bin").mkdir(parents=True)
    huella = {p: p.read_bytes() for p in principal.rglob("*") if p.is_file()}

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(comun, "_RAIZ_PROYECTO", sandbox)
    monkeypatch.delenv("BODEGA", raising=False)
    yield principal, sandbox
    # Pase lo que pase en la prueba, producción no cambió ni un byte.
    assert {p: p.read_bytes() for p in principal.rglob("*") if p.is_file()} == huella


def test_prepara_el_sandbox_sin_tocar_produccion(produccion_y_sandbox):
    principal, sandbox = produccion_y_sandbox
    ps.preparar(sandbox, principal, esperar=False)

    for ruta in ("data/pedidos.db", "data/tareas.db", "data/bodega2/pedidos.db"):
        assert (sandbox / ruta).exists(), ruta
    assert (sandbox / "data" / "inventario" / "layout_bodega.xlsx").read_bytes() == b"layout"
    env = (sandbox / ".env").read_text()
    assert "SMTP_PASSWORD" not in env and "SCRAPER_USUARIO=u" in env
    assert "SHEETS_ID_" not in (sandbox / ".env.bodega2").read_text()
    for privado in (".env", ".env.bodega2", "data/sesiones/bochica_storage_state.json"):
        assert (sandbox / privado).stat().st_mode & 0o077 == 0, privado
    # El hook pre-commit busca `.venv/bin/python` en la raíz del worktree.
    assert (sandbox / ".venv").is_symlink()
    assert (sandbox / ".venv").resolve() == (principal / ".venv").resolve()


def test_borrar_quita_solo_los_datos_del_sandbox(produccion_y_sandbox):
    principal, sandbox = produccion_y_sandbox
    (sandbox / "scripts").mkdir()
    (sandbox / "scripts" / "codigo.py").write_text("x = 1\n")
    ps.preparar(sandbox, principal, esperar=False)

    ps.borrar(sandbox)

    assert not (sandbox / "data" / "pedidos.db").exists()
    assert not (sandbox / "data" / "bodega2" / "pedidos.db").exists()
    assert not (sandbox / ".env").exists()
    assert (sandbox / "scripts" / "codigo.py").exists()  # el código no se toca
