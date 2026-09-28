"""DEC-152: re-extracción dirigida con `--ids-archivo` (reparación D1/D2/D3)."""

import argparse

import pytest

import scraper.orquestador as orq

pytestmark = pytest.mark.integration


def test_leer_ids_archivo_ignora_vacios_comentarios_y_repetidos(tmp_path):
    archivo = tmp_path / "ids.txt"
    archivo.write_text("# D1\n111\n\n  222  \n111\n333 # D3\n", encoding="utf-8")
    assert orq.leer_ids_archivo(archivo) == ["111", "222", "333"]


def test_parser_acepta_ids_archivo(tmp_path):
    ruta = tmp_path / "ids.txt"
    args = orq.build_arg_parser().parse_args(["--modo", "completo", "--ids-archivo", str(ruta)])
    assert args.ids_archivo == ruta


def test_parser_sin_ids_archivo_queda_en_none():
    assert orq.build_arg_parser().parse_args([]).ids_archivo is None


@pytest.mark.parametrize("modo", ["incremental", "mantenimiento"])
async def test_ids_archivo_fuera_del_modo_completo_aborta(tmp_path, modo, monkeypatch):
    """Se ignoraría en silencio: mejor fallar antes de abrir el navegador."""
    monkeypatch.setattr(orq, "log_event", lambda *a, **k: None)
    args = argparse.Namespace(modo=modo, ids_archivo=tmp_path / "ids.txt")
    assert await orq.main(args) == 2
