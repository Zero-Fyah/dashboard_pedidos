"""Tests de `dashboard.conteos_io` — subida y anulación de hojas (DEC-060)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))

import conteos_io  # noqa: E402


@pytest.fixture(autouse=True)
def carpeta_temporal(tmp_path, monkeypatch):
    """Aísla las escrituras: los tests no pueden tocar los conteos reales."""
    monkeypatch.setattr(conteos_io, "CARPETA_CONTEOS", tmp_path / "conteos")
    monkeypatch.setattr(conteos_io, "CARPETA_ANULADOS", tmp_path / "conteos" / "anulados")
    return tmp_path


def test_nombre_seguro_conserva_uno_normal():
    assert conteos_io.nombre_seguro("hoja_conteo_20260727.xlsx") == "hoja_conteo_20260727.xlsx"


def test_nombre_seguro_bloquea_el_salto_de_carpeta():
    """El nombre viene del navegador: es entrada no confiable.

    Sin sanear, `../../algo.xlsx` escribiría fuera de `data/conteos/`.
    """
    assert conteos_io.nombre_seguro("../../../evil.xlsx") == "evil.xlsx"
    assert conteos_io.nombre_seguro(r"C:\Windows\System32\x.xlsx") == "x.xlsx"
    assert "/" not in conteos_io.nombre_seguro("a/b/c.xlsx")


def test_nombre_seguro_normaliza_tildes_y_espacios():
    assert conteos_io.nombre_seguro("conteo áreá 1.xlsx") == "conteo_area_1.xlsx"


def test_nombre_seguro_rechaza_otras_extensiones():
    """Un .exe renombrado o un CSV no entran a la carpeta de conteos."""
    for malo in ("script.exe", "datos.csv", "hoja.xlsm"):
        with pytest.raises(ValueError, match="xlsx"):
            conteos_io.nombre_seguro(malo)


def test_nombre_seguro_rechaza_el_nombre_vacio():
    with pytest.raises(ValueError):
        conteos_io.nombre_seguro(".xlsx")


def test_guardar_crea_la_carpeta_si_no_existe():
    destino = conteos_io.guardar("hoja.xlsx", b"contenido")

    assert destino.exists()
    assert destino.read_bytes() == b"contenido"
    assert destino.parent == conteos_io.CARPETA_CONTEOS


def test_guardar_el_mismo_nombre_reemplaza():
    """Es el flujo de 'corregir una hoja': el valor nuevo pisa al anterior."""
    conteos_io.guardar("hoja.xlsx", b"version 1")
    conteos_io.guardar("hoja.xlsx", b"version 2")

    assert (conteos_io.CARPETA_CONTEOS / "hoja.xlsx").read_bytes() == b"version 2"
    assert conteos_io.listar_activas() == ["hoja.xlsx"]


def test_anular_saca_del_calculo_sin_borrar():
    """La operación de deshacer de DEC-058: el archivo sobrevive."""
    conteos_io.guardar("hoja.xlsx", b"datos")
    destino = conteos_io.anular("hoja.xlsx")

    assert not (conteos_io.CARPETA_CONTEOS / "hoja.xlsx").exists()
    assert destino.exists()
    assert destino.read_bytes() == b"datos"
    assert conteos_io.listar_activas() == []


def test_anular_dos_veces_el_mismo_nombre_no_pisa_la_primera():
    """Anular, volver a subir y volver a anular no puede perder la original."""
    conteos_io.guardar("hoja.xlsx", b"primera")
    conteos_io.anular("hoja.xlsx")
    conteos_io.guardar("hoja.xlsx", b"segunda")
    conteos_io.anular("hoja.xlsx")

    anuladas = sorted(p.read_bytes() for p in conteos_io.CARPETA_ANULADOS.glob("*.xlsx"))

    assert anuladas == [b"primera", b"segunda"]


def test_anular_una_hoja_inexistente_avisa():
    with pytest.raises(FileNotFoundError):
        conteos_io.anular("no_existe.xlsx")


def test_listar_activas_ignora_los_temporales_de_excel():
    """`~$archivo.xlsx` aparece mientras Excel tiene el archivo abierto."""
    conteos_io.guardar("hoja.xlsx", b"datos")
    (conteos_io.CARPETA_CONTEOS / "~$hoja.xlsx").write_bytes(b"temporal")

    assert conteos_io.listar_activas() == ["hoja.xlsx"]


def test_listar_activas_no_mira_dentro_de_anulados():
    conteos_io.guardar("activa.xlsx", b"a")
    conteos_io.guardar("vieja.xlsx", b"v")
    conteos_io.anular("vieja.xlsx")

    assert conteos_io.listar_activas() == ["activa.xlsx"]


def test_existe_responde_sobre_las_activas():
    conteos_io.guardar("hoja.xlsx", b"datos")

    assert conteos_io.existe("hoja.xlsx")
    assert not conteos_io.existe("otra.xlsx")
