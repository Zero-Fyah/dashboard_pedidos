"""Tests del módulo de Tareas (DEC-046).

Van en integration/ porque ejercitan SQLite real: lo que hay que verificar
es la sincronización por id, la preservación de `creada_en` y que la
siembra no reviva tareas borradas — nada de eso lo cubre un mock.

`tareas_db` vive en dashboard/, que Streamlit pone en sys.path al correr
la app; acá se agrega explícitamente por la misma razón.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))

import tareas_db  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Aísla cada test en su propia base."""
    ruta = tmp_path / "tareas.db"
    monkeypatch.setattr(tareas_db, "DB_PATH", ruta)
    return ruta


def _filas(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM tareas").fetchone()[0]
    finally:
        con.close()


# ─────────────────────────────────────────────
# Siembra
# ─────────────────────────────────────────────


def test_init_siembra_el_backlog_documentado(db):
    tareas_db.init_db()
    assert _filas(db) == len(tareas_db._SEMILLA)
    assert _filas(db) > 0, "la semilla no puede quedar vacía"


def test_init_es_idempotente(db):
    """Se llama en cada carga de la página: no puede duplicar."""
    tareas_db.init_db()
    tareas_db.init_db()
    assert _filas(db) == len(tareas_db._SEMILLA)


def test_borrar_todas_no_las_revive(db):
    """El caso que motivó la marca `sembrado`: con "¿está vacía?" volvían."""
    tareas_db.init_db()
    tareas_db.guardar_tareas(pd.DataFrame(columns=tareas_db.COLUMNAS))
    assert _filas(db) == 0
    tareas_db.init_db()  # simula recargar la página
    assert _filas(db) == 0, "las tareas borradas no deben reaparecer"


def test_la_semilla_trae_origen_trazable(db):
    """Cada tarea apunta a dónde está documentado el hallazgo."""
    tareas_db.init_db()
    df = tareas_db.listar_tareas()
    assert (df["origen"].str.len() > 0).all()
    assert df["titulo"].is_unique


# ─────────────────────────────────────────────
# Guardado
# ─────────────────────────────────────────────


def test_guardar_actualiza_sin_cambiar_el_id(db):
    tareas_db.init_db()
    df = tareas_db.listar_tareas()
    id_original = int(df.loc[0, "id"])
    df.loc[0, "estado"] = "En progreso"

    nuevas, actualizadas, borradas = tareas_db.guardar_tareas(df)
    assert (nuevas, borradas) == (0, 0)
    assert actualizadas == len(tareas_db._SEMILLA)

    despues = tareas_db.listar_tareas()
    fila = despues[despues["id"] == id_original].iloc[0]
    assert fila["estado"] == "En progreso"


def test_guardar_conserva_creada_en(db):
    """Por eso se sincroniza por id en vez de borrar y reinsertar."""
    tareas_db.init_db()
    con = sqlite3.connect(db)
    creada_antes = con.execute("SELECT id, creada_en FROM tareas ORDER BY id").fetchall()
    con.close()

    df = tareas_db.listar_tareas()
    df.loc[0, "titulo"] = "Título editado"
    tareas_db.guardar_tareas(df)

    con = sqlite3.connect(db)
    creada_despues = con.execute("SELECT id, creada_en FROM tareas ORDER BY id").fetchall()
    con.close()
    assert creada_antes == creada_despues


def test_guardar_inserta_filas_nuevas(db):
    tareas_db.init_db()
    df = tareas_db.listar_tareas()
    nueva = {
        "id": None,
        "titulo": "Revisar unidades de peso",
        "detalle": "",
        "categoria": "Otro",
        "prioridad": "Media",
        "estado": "Pendiente",
        "origen": "",
    }
    df = pd.concat([df, pd.DataFrame([nueva])], ignore_index=True)

    nuevas, _, _ = tareas_db.guardar_tareas(df)
    assert nuevas == 1
    assert _filas(db) == len(tareas_db._SEMILLA) + 1
    assert "Revisar unidades de peso" in set(tareas_db.listar_tareas()["titulo"])


def test_guardar_borra_las_que_faltan(db):
    tareas_db.init_db()
    conservadas = 2
    df = tareas_db.listar_tareas().iloc[:conservadas]
    _, _, borradas = tareas_db.guardar_tareas(df)
    assert borradas == len(tareas_db._SEMILLA) - conservadas
    assert _filas(db) == conservadas


def test_fila_sin_titulo_se_ignora(db):
    """El editor deja filas en blanco al agregar: no deben persistirse."""
    tareas_db.init_db()
    df = tareas_db.listar_tareas()
    vacia = dict.fromkeys(tareas_db.COLUMNAS, None)
    df = pd.concat([df, pd.DataFrame([vacia])], ignore_index=True)

    nuevas, _, _ = tareas_db.guardar_tareas(df)
    assert nuevas == 0
    assert _filas(db) == len(tareas_db._SEMILLA)


def test_valores_por_defecto_al_guardar(db):
    """Una fila nueva sin estado ni prioridad no debe quedar en NULL."""
    tareas_db.init_db()
    nueva = pd.DataFrame(
        [
            {
                "id": None,
                "titulo": "Sin metadatos",
                "detalle": None,
                "categoria": None,
                "prioridad": None,
                "estado": None,
                "origen": None,
            }
        ]
    )
    tareas_db.guardar_tareas(nueva)
    fila = tareas_db.listar_tareas().iloc[0]
    assert fila["estado"] == "Pendiente"
    assert fila["prioridad"] == "Media"


def test_orden_prioriza_lo_activo_y_urgente(db):
    """En progreso primero, luego bloqueadas y pendientes; completadas al final."""
    tareas_db.init_db()
    df = tareas_db.listar_tareas()
    df.loc[df.index[-1], "estado"] = "En progreso"
    df.loc[df.index[0], "estado"] = "Completada"
    tareas_db.guardar_tareas(df)

    orden = list(tareas_db.listar_tareas()["estado"])
    assert orden[0] == "En progreso"
    assert orden[-1] == "Completada"


def test_resumen_cuenta_por_estado(db):
    tareas_db.init_db()
    r = tareas_db.resumen()
    assert r["total"] == len(tareas_db._SEMILLA)
    assert r["Pendiente"] == len(tareas_db._SEMILLA)
    assert r["Completada"] == 0
