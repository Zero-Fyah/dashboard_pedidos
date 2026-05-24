# dashboard_pedidos — Estrategia de testing

Documento de referencia para la estrategia de tests del proyecto.
Este archivo es propiedad exclusiva del Arquitecto. La IA lo lee, nunca lo modifica.

**Versión:** 1.0 · **Actualizado:** 2026-05-22
**Protocolo base:** `protocolo_desarrollo_ia.md` v1.1

---

## Filosofía de testing para este proyecto

Este no es un proyecto web. No hay endpoints, autenticación ni multi-tenant.
Los riesgos que los tests deben cubrir son distintos:

- **Integridad de datos:** que el scraper no corrompa ni duplique registros en SQLite.
- **Cumplimiento de reglas de negocio:** que el código respete las 4 reglas de
  `docs/integral.md` (alcance temporal, pedido cerrado, cantidades al cierre,
  subpedido histórico inmutable).
- **Idempotencia:** que re-ejecutar el scraper sobre los mismos datos no genere
  duplicados ni sobreescriba datos ya correctos.
- **Correctitud del ETL:** que la normalización de montos produzca valores numéricos
  correctos y que las VIEWs retornen los datos esperados.

Los tests de seguridad clásicos (token expirado, roles, inyección SQL) son
irrelevantes aquí. Los tests de race condition y de Playwright con browser real
son costosos y se tratan de forma especial.

---

## Stack de testing

| Herramienta | Rol |
|---|---|
| `pytest` | Framework principal |
| `pytest-asyncio` | Soporte para funciones `async` |
| `pytest-mock` | Mocking con fixture `mocker` |
| `pytest-cov` | Reporte de cobertura |
| `pytest-playwright` | Tests E2E con browser real (lentos, opcionales) |

Agregar al `requirements.txt` en sección separada:
```
# Testing
pytest>=8.0
pytest-asyncio>=0.24
pytest-mock>=3.14
pytest-cov>=5.0
pytest-playwright>=0.5
```

### Configuración — `pytest.ini` en la raíz del proyecto

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
markers =
    unit: tests unitarios sin I/O externo
    integration: tests de integración con SQLite en archivo temporal
    e2e: tests con browser Playwright real (requieren sistema administrativo)
```

### Configuración de imports — `conftest.py` raíz

Para que pytest resuelva `from scraper.scraper_principal import ...` desde
cualquier subcarpeta de `tests/`, agregar en la raíz del proyecto:

```python
# conftest.py (raíz del proyecto, no dentro de tests/)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
```

Y agregar `scraper/__init__.py` vacío para que Python trate `scraper/`
como paquete importable.

### Activar los tests E2E (marcador condicional)

Los tests `e2e` requieren acceso al sistema administrativo y son lentos.
Se excluyen por defecto. Agregar en `tests/conftest.py`:

```python
def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", default=False,
                     help="Ejecutar tests E2E con browser real")

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--e2e"):
        skip_e2e = pytest.mark.skip(reason="Usar --e2e para ejecutar tests de browser")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_e2e)
```

Comandos de ejecución:
```bash
# Solo unitarios (segundos)
pytest -m unit

# Unit + integration (< 1 min)
pytest -m "not e2e"

# Todo incluyendo browser
pytest --e2e
```

---

## Estructura de carpetas

```text
tests/
├── conftest.py                    # fixtures + opciones de pytest
├── unit/
│   ├── test_helpers.py            # to_num, validar_config
│   ├── test_modo_seleccion.py     # lógica de selección de modo
│   └── test_reglas_negocio.py     # invariantes de negocio en el código
├── integration/
│   ├── test_init_db.py            # schema, migraciones, índices
│   ├── test_persistencia.py       # los 3 modos de persistencia
│   ├── test_idempotencia.py       # re-ejecuciones y ciclo incremental
│   └── test_etl.py                # 4 tests ETL (normalización + VIEWs)
└── e2e/                           # tests con browser real — lentos
    └── test_extraccion_real.py    # extracción contra sistema administrativo real
```

> Agregar `tests/` a la estructura del repositorio en `CLAUDE.md`
> cuando se cree la primera carpeta.
> `docs/structure.md` documenta arquitectura técnica,
> no la estructura de directorios del repositorio.

---

## División de responsabilidades

### Tests que Claude genera (bajo TASK explícito)

- Tests unitarios de funciones puras (`to_num`, `validar_config`, helpers)
- Tests de integración de DB: schema, migraciones, índices
- Tests de los 3 modos de persistencia con fixtures controlados
- Tests de normalización del ETL
- Tests de VIEWs SQL con datos controlados

### Tests que el Arquitecto escribe (Claude no los hace bien)

- **Tests de idempotencia de ciclo completo:** verificar que ejecutar el ciclo
  incremental dos veces produce exactamente el mismo estado en DB.
- **Tests de reglas de negocio end-to-end:** verificar que un pedido cerrado
  no reaparece en el siguiente ciclo incremental (requiere simular `main()`).
- **Tests E2E con browser real:** requieren acceso al sistema administrativo
  y conocimiento de qué pedidos existen en el entorno real.
- **Tests de modo `con_cantidades` vs regla de negocio:** bloqueados hasta
  que se resuelva BUG-005 en `docs/decisions.md`.

---

## Prerequisito — Refactor de testabilidad

Antes de escribir tests unitarios de selección de modo, extraer la lógica
a una función pura en `scraper/scraper_principal.py`:

```python
def determinar_modo(es_nuevo: bool, subs_db: list[tuple[str, int]]) -> str:
    """Determina el modo de extracción sin consultar la DB ni navegar.

    Args:
        es_nuevo: True si el pedido no existe en la DB.
        subs_db: Lista de (estado, cantidades_definitivas) de los subpedidos.

    Returns:
        "completo", "con_cantidades" o "solo_estado".
    """
    if es_nuevo:
        return "completo"
    if any(cd == 0 and estado.lower() in ESTADOS_FIJAN_CANTIDADES
           for estado, cd in subs_db):
        return "con_cantidades"
    return "solo_estado"
```

En `procesar_pedido()`, reemplazar la lógica inline por `determinar_modo(es_nuevo, subs_db)`.
El comportamiento no cambia; solo se hace testeable sin browser.

---

## Etapa 1 — Scraper

### Fixtures (`tests/conftest.py`)

Los tests de integración usan archivos temporales de SQLite, no `:memory:`,
porque el PRAGMA `WAL` no aplica a bases en memoria.

> **Nota crítica sobre `PEDIDO_BARCODE_VACIO`:** el spread `**PEDIDO_SIN_DIFERENCIAS`
> hace copia superficial. Para evitar que la mutación del fixture afecte al
> original, usar `copy.deepcopy`.

```python
import copy
import pytest
import pytest_asyncio
import aiosqlite
from scraper.scraper_principal import init_db

@pytest_asyncio.fixture
async def db_path(tmp_path):
    """Archivo SQLite temporal con schema completo aplicado."""
    path = str(tmp_path / "test_pedidos.db")
    await init_db(path)
    return path

# ── Datos de prueba ────────────────────────────────────────────────────────────
# IDs siempre con prefijo TEST- para distinguirlos de datos reales.

_PEDIDO_BASE = {
    "tipo": "completo",
    "id_pedido": "TEST-001",
    "info_general": {
        "id_pedido":         "TEST-001",
        "fecha":             "2026-05-22 10:30:00",
        "hora":              "10:30:00",
        "vendedor":          "Vendedor Test",
        "servicio_cliente":  "Cliente Test",
        "forma_pago":        "Crédito",
        "comprobante":       "COMP-001",
        "nombre_empresa":    "Empresa Test",
        "nit":               "900000000-1",
        "metodo_entrega":    "Domicilio",
        "destinatario":      "Destinatario Test",
        "telefono":          "3001234567",
        "direccion_envio":   "Calle Test 123",
        "observaciones":     "",
        "alistador_pedido":  "",
        "inspector_pedido":  "",
        "movil_cliente":     "",
    },
    "subpedidos": [
        {
            "numero_subpedido":        "SUB-001",
            "tipo_subpedido":          "Normal",
            "estado":                  "en alistamiento",
            "inicio_alistamiento":     "2026-05-22 08:00:00",
            "alistamiento_completado": "",
            "alistador":               "Alistador Test",
            "inicio_inspeccion":       "",
            "inspeccion_completada":   "",
            "inspector":               "",
            "lineas": [
                {
                    "nombre_producto":  "Producto Test",
                    "referencia":       "REF-001",
                    "codigo_barras":    "7700000000001",
                    "presentacion":     "Unidad",
                    "almacen":          "Almacén Principal",
                    "cantidad_comprada":  10.0,
                    "cantidad_entregada": 0.0,
                    "precio_unitario":  "10.000,00",
                    "descuento":        "0,00",
                    "precio_descuento": "10.000,00",
                    "monto_pagar":      "100.000,00",
                    "monto_final":      "100.000,00",
                    "iva":              "0,00",
                    "peso_total":       "1,00",
                    "observaciones":    "",
                    "numero_caja":      "",
                    "tipo":             "",
                },
            ],
        }
    ],
    "timeline": [
        {"id_pedido": "TEST-001", "paso": 1, "titulo": "Recibido",
         "fecha_hora": "2026-05-22 07:00:00", "completado": 1},
    ],
    "info_entrega":  {"despachador": "", "hora_entrega": "", "obs_entrega": "",
                      "entrega_ruta_tag": "", "entrega_descuento_tag": ""},
    "estadisticas":  [],
    "hay_diferencia": 0,
    "gestion_dif":   None,
    "detalle_dif":   [],
    "registro_ops":  [],
}

@pytest.fixture
def pedido_sin_diferencias():
    return copy.deepcopy(_PEDIDO_BASE)

@pytest.fixture
def pedido_barcode_vacio():
    """Copia profunda del pedido base con código de barras vacío (BUG-006)."""
    p = copy.deepcopy(_PEDIDO_BASE)
    p["id_pedido"] = "TEST-002"
    p["info_general"]["id_pedido"] = "TEST-002"
    p["subpedidos"][0]["lineas"][0]["codigo_barras"] = ""
    return p
```

### Tests unitarios (`tests/unit/`)

**`test_helpers.py`:**

```python
import pytest
from scraper.scraper_principal import to_num

@pytest.mark.parametrize("entrada,esperado", [
    ("1.234,56",   1234.56),
    ("200",        200.0),
    ("0,50",       0.5),
    (",50",        0.5),
    ("-1.000,00",  -1000.0),
    (" 1.234,56 ", 1234.56),  # espacios al inicio/fin
    ("",           None),
    ("N/A",        None),
    ("—",          None),
    ("None",       None),
])
@pytest.mark.unit
def test_to_num(entrada, esperado):
    assert to_num(entrada) == esperado
```

**`test_modo_seleccion.py`:**

```python
import pytest
from scraper.scraper_principal import determinar_modo  # requiere el refactor previo

@pytest.mark.unit
def test_modo_pedido_nuevo_sin_subs():
    assert determinar_modo(True, []) == "completo"

@pytest.mark.unit
def test_modo_pedido_nuevo_ignora_estado_subs():
    """Pedido nuevo siempre es completo, sin importar el estado de sus subs."""
    assert determinar_modo(True, [("enviado", 0)]) == "completo"

@pytest.mark.unit
def test_modo_con_cantidades_sub_pendiente():
    assert determinar_modo(False, [("enviado", 0)]) == "con_cantidades"

@pytest.mark.unit
def test_modo_solo_estado_cantidades_ya_definitivas():
    assert determinar_modo(False, [("enviado", 1)]) == "solo_estado"

@pytest.mark.unit
def test_modo_solo_estado_sub_cerrado_definitivo():
    assert determinar_modo(False, [("completado", 1)]) == "solo_estado"

@pytest.mark.unit
def test_modo_con_cantidades_mixto():
    """Un sub cerrado (definitivo) + uno abierto → con_cantidades."""
    subs = [("completado", 1), ("enviado", 0)]
    assert determinar_modo(False, subs) == "con_cantidades"

@pytest.mark.unit
def test_modo_solo_estado_todos_definitivos():
    """Todos los subs con cantidades definitivas → solo_estado."""
    subs = [("completado", 1), ("cancelado", 1)]
    assert determinar_modo(False, subs) == "solo_estado"
```

> **BUG-005 resuelto — opción B.** Los tests
> reflejan el comportamiento definitivo: estados
> intermedios del flujo operacional (enviado,
> pendiente de entrega, período contable, y otros
> de ESTADOS_FIJAN_CANTIDADES que no son cerrados)
> no activan con_cantidades. Únicamente los estados
> cerrados (completado, cancelado, comentado) con
> cantidades_definitivas=0 lo activan.

**`test_reglas_negocio.py`:**

```python
import pytest
from scraper.scraper_principal import ESTADOS_CERRADOS, ESTADOS_FIJAN_CANTIDADES

@pytest.mark.unit
def test_estados_cerrados_subconjunto_de_fijan_cantidades():
    assert ESTADOS_CERRADOS.issubset(ESTADOS_FIJAN_CANTIDADES)

@pytest.mark.unit
def test_estados_cerrados_exactamente_tres():
    assert ESTADOS_CERRADOS == frozenset({"completado", "cancelado", "comentado"})

@pytest.mark.unit
def test_estados_cerrados_en_minusculas():
    """Los estados deben estar en minúsculas para que .lower() no cambie nada."""
    for estado in ESTADOS_CERRADOS:
        assert estado == estado.lower(), f"Estado '{estado}' no está en minúsculas"

@pytest.mark.unit
def test_estados_fijan_cantidades_en_minusculas():
    for estado in ESTADOS_FIJAN_CANTIDADES:
        assert estado == estado.lower(), f"Estado '{estado}' no está en minúsculas"
```

### Tests de integración (`tests/integration/`)

**`test_init_db.py`:**

```python
import pytest
import aiosqlite
from scraper.scraper_principal import init_db

@pytest.mark.integration
async def test_nueve_tablas_existen(db_path):
    tablas_esperadas = {
        "pedidos", "subpedidos", "lineas_pedido", "timeline_pedido",
        "estadisticas_monto", "gestion_diferencias", "detalle_diferencias",
        "registro_operaciones", "errores",
    }
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tablas = {row[0] for row in await cursor.fetchall()}
    assert tablas_esperadas.issubset(tablas)

@pytest.mark.integration
async def test_pragma_wal_activo(db_path):
    async with aiosqlite.connect(db_path) as db:
        row = (await (await db.execute("PRAGMA journal_mode")).fetchone())
    assert row[0] == "wal"

@pytest.mark.integration
async def test_siete_indices_existen(db_path):
    indices_esperados = {
        "idx_subpedidos_pedido",  "idx_lineas_pedido",
        "idx_errores_pedido",     "idx_estadisticas_pedido",
        "idx_gestion_dif_pedido", "idx_detalle_dif_pedido",
        "idx_registro_ops_pedido",
    }
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indices = {row[0] for row in await cursor.fetchall()}
    assert indices_esperados.issubset(indices)

@pytest.mark.integration
async def test_init_db_es_idempotente(db_path):
    """Llamar init_db dos veces no lanza excepción ni duplica tablas."""
    await init_db(db_path)  # segunda llamada
    async with aiosqlite.connect(db_path) as db:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        )).fetchone())[0]
    assert count == 9
```

**`test_persistencia.py`:**

```python
import pytest
import aiosqlite
import asyncio
from scraper.scraper_principal import persistencia_worker

async def persistir_uno(resultado: dict, db_path: str) -> None:
    """Helper: encola un resultado y ejecuta el worker hasta el sentinel."""
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(resultado)
    await queue.put(None)  # sentinel
    await persistencia_worker(queue, db_path)

@pytest.mark.integration
async def test_modo_completo_inserta_pedido(db_path, pedido_sin_diferencias):
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT scraping_completo FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone()
    assert row is not None
    assert row[0] == 1

@pytest.mark.integration
async def test_modo_completo_es_idempotente(db_path, pedido_sin_diferencias):
    """Insertar el mismo pedido dos veces no duplica registros."""
    await persistir_uno(pedido_sin_diferencias, db_path)
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone())[0]
    assert count == 1

@pytest.mark.integration
async def test_modo_solo_estado_no_modifica_lineas(db_path, pedido_sin_diferencias):
    """solo_estado actualiza estado del subpedido pero no toca lineas_pedido."""
    await persistir_uno(pedido_sin_diferencias, db_path)
    async with aiosqlite.connect(db_path) as db:
        antes = await (await db.execute(
            "SELECT cantidad_entregada FROM lineas_pedido WHERE id_pedido = 'TEST-001'"
        )).fetchall()

    solo_estado = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "en alistamiento"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(solo_estado, db_path)
    async with aiosqlite.connect(db_path) as db:
        despues = await (await db.execute(
            "SELECT cantidad_entregada FROM lineas_pedido WHERE id_pedido = 'TEST-001'"
        )).fetchall()
    assert antes == despues

@pytest.mark.integration
async def test_solo_estado_marca_pedido_cerrado(db_path, pedido_sin_diferencias):
    """Cuando todos los subpedidos pasan a estado cerrado, scraping_completo=1."""
    # Insertar pedido con subpedido abierto
    await persistir_uno(pedido_sin_diferencias, db_path)
    # Forzar cierre del subpedido
    cierre = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "completado"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(cierre, db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT scraping_completo FROM pedidos WHERE id_pedido = 'TEST-001'"
        )).fetchone()
    assert row[0] == 1

@pytest.mark.integration
async def test_pedido_cerrado_no_aparece_en_ids_activos(db_path, pedido_sin_diferencias):
    """Regla de negocio central: un pedido cerrado no se reprocesa.

    Esta es la query que usa el modo incremental para obtener ids_activos.
    Un pedido con todos los subpedidos cerrados no debe aparecer.
    """
    await persistir_uno(pedido_sin_diferencias, db_path)
    # Cerrar el único subpedido
    cierre = {
        "tipo": "solo_estado", "id_pedido": "TEST-001",
        "subpedidos": [{"numero_subpedido": "SUB-001", "estado": "completado"}],
        "timeline": [], "estadisticas": [], "hay_diferencia": 0,
        "gestion_dif": None, "detalle_dif": [], "registro_ops": [],
    }
    await persistir_uno(cierre, db_path)
    # Ejecutar la query exacta del modo incremental
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("""
            SELECT DISTINCT p.id_pedido
            FROM pedidos p
            JOIN subpedidos s ON p.id_pedido = s.id_pedido
            WHERE p.scraping_completo = 1
              AND LOWER(s.estado) NOT IN ('completado','cancelado','comentado')
        """)).fetchall()
    ids_activos = [r[0] for r in rows]
    assert "TEST-001" not in ids_activos
```

**`test_idempotencia.py`** — Arquitecto escribe:

```python
# TODO — Arquitecto
# Verificar que el ciclo incremental completo (activos + errores + nuevos)
# produce exactamente el mismo estado en DB si se ejecuta dos veces.
# Requiere mockear la navegación y la obtención de IDs del servidor.
# Claude no puede generar este test sin conocer el estado completo
# del sistema entre ejecuciones.
```

### Tests E2E (`tests/e2e/`)

```python
import os
import pytest

@pytest.mark.e2e
async def test_extraccion_pedido_real(page):
    """Extrae un pedido real y verifica la estructura básica de datos retornados."""
    test_id = os.environ.get("TEST_PEDIDO_ID")
    if not test_id:
        pytest.skip("TEST_PEDIDO_ID no definido en .env")
    # Implementar cuando haya un pedido de prueba estable
    # en el sistema administrativo.
```

---

## Etapa 2 — ETL

### Tests de integración del ETL (`tests/integration/test_etl.py`)

Los tests del ETL se implementaron como tests de
integración. Archivo: `tests/integration/test_etl.py`
— 4 tests pasando:

- `test_columnas_num_creadas` — verifica que las
  24 columnas `_num` existen tras `normalizar_montos()`
- `test_normalizacion_es_idempotente` — verifica que
  ejecutar `normalizar_montos()` dos veces no genera
  errores
- `test_views_creadas` — verifica que las 7 VIEWs
  existen tras `normalizar_montos()` + `crear_views()`
- `test_views_son_idempotentes` — verifica que
  ejecutar `crear_views()` dos veces produce
  exactamente 7 VIEWs

> Los tests de normalización y VIEWs descritos
> previamente en este documento para `tests/etl/`
> no se crearon. La cobertura del ETL está en
> `tests/integration/test_etl.py`.

---

## Etapa 3 — Dashboard

Estrategia por definir cuando se decida la tecnología.
SQLite confirmado como capa de datos (DEC-010 resuelta).
Mínimo requerido al implementar:

- Test de que las consultas retornan datos correctos contra DB de test
- Test de que el dashboard **no escribe** en `pedidos.db`

---

## Cobertura mínima

| Componente | Meta |
|---|---|
| Funciones puras (`to_num`, helpers) | 100% |
| Lógica de selección de modo (`determinar_modo`) | 100% |
| Invariantes de negocio (frozensets, constantes) | 100% |
| Modos de persistencia (completo / con_cantidades / solo_estado) | ≥ 90% |
| `init_db` y migraciones | ≥ 80% |
| Funciones `extraer_*` | No aplica unitariamente — solo E2E |
| ETL — normalización de campos | 100% |
| ETL — VIEWs SQL | ≥ 80% |

```bash
pytest -m "not e2e" --cov=scraper --cov-report=term-missing
```

---

## Cuándo correr los tests

| Momento | Comando |
|---|---|
| Durante desarrollo | `pytest -m unit` |
| Antes de commit | `pytest -m "not e2e"` |
| Antes de la carga inicial | `pytest -m "not e2e"` + revisar cobertura |
| Antes de merge a main | `pytest --e2e` |
| Si tasa de aceptación IA < 80% | Auditar `tests/unit/test_reglas_negocio.py` |

---

## Datos de prueba — reglas

- IDs de pedido siempre con prefijo `TEST-`
- Cubrir casos edge: sin diferencias, con diferencias, múltiples subpedidos,
  código de barras vacío (BUG-006)
- Nunca contener datos reales de la operación
- No referenciar el nombre real de la empresa ni las URLs del sistema
- Usar `copy.deepcopy` al derivar fixtures de un fixture base para evitar
  mutaciones cruzadas entre tests

---

## Lo que NO se testea

- **La interfaz del sistema administrativo:** cambia sin aviso. Solo los tests
  E2E la tocan, marcados como opcionales.
- **El comportamiento interno de Playwright:** responsabilidad de la librería.
- **El scheduler de Windows:** es infraestructura, no código del proyecto.
- **La conectividad de red:** los tests de integración usan SQLite en archivo
  temporal y no dependen de la red.