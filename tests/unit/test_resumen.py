"""Tests del resumen final del run (Fase 2: N-1 + AUD-B2 + HAL-005).

construir_resumen() calcula las métricas a partir de los resultados del
propio run (sets "ok"/"error" poblados por persistencia_worker), no del
estado acumulado en DB. La tasa decide el exit code del scraper, así que
estos tests protegen la veracidad de la señal hacia el Task Scheduler.
"""

import pytest

from scraper.scraper_principal import construir_resumen

_CARRILES = {"activos": 5, "errores": 2, "nuevos": 3}


def _stats(ok=(), error=()):
    return {"ok": set(ok), "error": set(error)}


@pytest.mark.unit
def test_run_vacio_tasa_100():
    """Sin pedidos encolados el run es trivialmente exitoso (exit 0)."""
    r = construir_resumen(
        0,
        _stats(),
        t_min=0.5,
        modo="incremental",
        carriles={"activos": 0, "errores": 0, "nuevos": 0},
    )
    assert r["pedidos_procesados"] == 0
    assert r["pedidos_error"] == 0
    assert r["tasa_exito_pct"] == 100.0


@pytest.mark.unit
def test_exito_se_mide_por_resultados_del_run():
    """N-1: la tasa refleja lo que pasó en ESTE run. 3 encolados con solo
    1 persistido OK → 33.33%, sin importar qué diga scraping_completo."""
    r = construir_resumen(
        3, _stats(ok=["A"], error=["B", "C"]), t_min=1.0, modo="incremental", carriles=_CARRILES
    )
    assert r["pedidos_procesados"] == 1
    assert r["pedidos_error"] == 2
    assert r["tasa_exito_pct"] == 33.33


@pytest.mark.unit
def test_todo_fallido_da_tasa_cero():
    """N-1 (caso que motivó el fix): run donde todo falló → tasa 0, no ~100."""
    r = construir_resumen(
        2, _stats(error=["A", "B"]), t_min=1.0, modo="incremental", carriles=_CARRILES
    )
    assert r["pedidos_procesados"] == 0
    assert r["pedidos_error"] == 2
    assert r["tasa_exito_pct"] == 0.0


@pytest.mark.unit
def test_error_recuperado_en_dead_letter_cuenta_como_exito():
    """Un pedido que falló y luego persistió OK en un pase dead-letter
    está en ambos sets: cuenta como éxito y no como error."""
    r = construir_resumen(
        2, _stats(ok=["A", "B"], error=["B"]), t_min=1.0, modo="incremental", carriles=_CARRILES
    )
    assert r["pedidos_procesados"] == 2
    assert r["pedidos_error"] == 0
    assert r["tasa_exito_pct"] == 100.0


@pytest.mark.unit
def test_errores_son_pedidos_distintos():
    """AUD-B2: reintentos múltiples del mismo pedido no inflan el conteo
    (sets deduplican por construcción, a diferencia de las filas de la
    tabla errores)."""
    stats = _stats()
    for _ in range(3):
        stats["error"].add("A")  # 3 pases dead-letter fallidos
    r = construir_resumen(1, stats, t_min=1.0, modo="incremental", carriles=_CARRILES)
    assert r["pedidos_error"] == 1


@pytest.mark.unit
def test_carriles_incremental_en_resumen():
    """HAL-005: el resumen desglosa los 3 carriles del incremental."""
    r = construir_resumen(10, _stats(ok=["A"]), t_min=1.0, modo="incremental", carriles=_CARRILES)
    assert r["modo"] == "incremental"
    assert r["pedidos_activos"] == 5
    assert r["pedidos_error_reintentos"] == 2
    assert r["pedidos_nuevos"] == 3


@pytest.mark.unit
def test_modo_completo_sin_carriles():
    """HAL-005: en modo completo los campos de carril son None (no 0,
    que se confundiría con un incremental vacío)."""
    r = construir_resumen(10, _stats(ok=["A"]), t_min=1.0, modo="completo", carriles=None)
    assert r["modo"] == "completo"
    assert r["pedidos_activos"] is None
    assert r["pedidos_error_reintentos"] is None
    assert r["pedidos_nuevos"] is None
