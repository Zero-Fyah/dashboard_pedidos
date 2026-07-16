"""Tests de la validación de fechas del CLI (Fase 6: N-5).

_fecha_iso() se testea directo; el wiring con argparse se testea via
build_arg_parser().parse_args(), donde un valor inválido debe terminar
en SystemExit (argparse convierte ArgumentTypeError en error de uso).
"""

import argparse

import pytest

from scraper.orquestador import _fecha_iso
from scraper.scraper_principal import build_arg_parser


@pytest.mark.unit
@pytest.mark.parametrize("valor", ["2026-05-01", "2026-12-31", "2025-02-28"])
def test_fecha_iso_valida(valor):
    assert _fecha_iso(valor) == valor


@pytest.mark.unit
@pytest.mark.parametrize(
    "valor",
    [
        "2026-5-1",  # sin ceros — formato laxo que la SPA ignora en silencio
        "01-05-2026",  # orden DD-MM-YYYY
        "2026/05/01",  # separador incorrecto
        "20260501",  # ISO básico — fromisoformat lo aceptaría, el regex no
        "ayer",
        "",
    ],
)
def test_fecha_iso_formato_invalido(valor):
    with pytest.raises(argparse.ArgumentTypeError, match="formato requerido"):
        _fecha_iso(valor)


@pytest.mark.unit
@pytest.mark.parametrize("valor", ["2026-13-01", "2026-02-30", "2026-00-10"])
def test_fecha_iso_calendario_invalido(valor):
    with pytest.raises(argparse.ArgumentTypeError, match="calendario"):
        _fecha_iso(valor)


@pytest.mark.unit
def test_argparse_rechaza_desde_invalido(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_arg_parser().parse_args(["--desde", "2026-5-1"])
    assert exc_info.value.code == 2
    assert "fecha inválida" in capsys.readouterr().err


@pytest.mark.unit
def test_argparse_rechaza_hasta_invalido():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--hasta", "2026-02-30"])


@pytest.mark.unit
def test_argparse_acepta_fechas_validas():
    args = build_arg_parser().parse_args(["--desde", "2026-05-01", "--hasta", "2026-06-15"])
    assert args.desde == "2026-05-01"
    assert args.hasta == "2026-06-15"
