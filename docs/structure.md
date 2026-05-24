# dashboard_pedidos — Arquitectura técnica

Documento de referencia para entender cómo está construido el sistema:
configuración de workers, flujo de datos, esquema de base de datos y
lógica de selección de modo de extracción.
Para el contexto de negocio que origina estas decisiones, ver `docs/integral.md`.

---

## Stack técnico

| Componente | Detalle |
|---|---|
| Lenguaje | Python 3.14 ⚠️ alpha — ver `docs/decisions.md` |
| Scraping | Playwright (Chromium) · asyncio |
| Base de datos | SQLite · modo WAL · `data/pedidos.db` |
| Async DB | aiosqlite 0.22.1 |
| Logging | JSONL estructurado · `logs/scraper.log` |
| Entorno | Windows 11 · VS Code · `.venv/` |

---

## Configuración de workers (valores actuales)

> **Nota:** el docstring de `scraper_principal.py` menciona "3 workers" en la
> estimación de tiempos, pero el valor activo en CONFIG es `NUM_WORKERS=5`.
> El docstring está desactualizado. El valor operativo es 5.

```python
NUM_WORKERS            = 5       # workers paralelos de scraping
QUEUE_MAXSIZE          = 100     # tamaño máximo de la cola de pedidos
PAUSA_ENTRE_PEDIDOS_S  = 1.2     # pausa entre pedidos por worker (segundos)
PAUSA_PAGINACION_S     = 2.0     # pausa entre páginas de la lista
NAV_TIMEOUT_MS         = 45_000  # timeout de navegación (ms)
ELEM_TIMEOUT_MS        = 20_000  # timeout de elementos DOM (ms)
MAX_REINTENTOS         = 5       # reintentos por pedido con backoff exponencial
BACKOFF_BASE_S         = 2       # base del backoff (fórmula: base^intento + jitter)
BACKOFF_MAX_S          = 60      # techo del backoff
RATE_LIMIT_WAIT_S      = 45      # espera ante HTTP 429 (o el valor de Retry-After)
MAX_SCREENSHOTS        = 50      # máximo de screenshots de error guardados
MAX_HTML_DEBUG         = 50      # máximo de HTMLs de debug guardados
```

**Circuit breaker (por worker, independiente):**
```python
CIRCUIT_FAILURE_THRESHOLD = 5   # fallos consecutivos que abren el circuito
CIRCUIT_COOLDOWN_S        = 90  # segundos de pausa cuando el circuito abre
CIRCUIT_MAX_REOPENINGS    = 3   # reaperturas máximas; al superarlas el worker termina
```

---

## Flujo de ejecución

```
main()
  │
  ├─ validar_config()              # falla rápido si faltan variables de entorno
  ├─ init_db()                     # crea tablas, aplica migraciones, crea índices
  ├─ limpiar_errores_resueltos()   # elimina de errores los pedidos ya completos/cerrados
  │
  ├─── Modo completo ─────────────────────────────────────────────────────────────
  │    └─ login + obtener_lista_pedidos(desde, hasta)   # recorre todas las páginas
  │
  └─── Modo incremental ──────────────────────────────────────────────────────────
       ├─ Proceso 1: ids_activos  ← consulta DB (scraping_completo=1 + subpedido abierto)
       ├─ Proceso 2: ids_error    ← consulta tabla errores (no completos/cerrados)
       ├─ Proceso 3: ids_nuevos   ← login + obtener_lista_pedidos(ayer, hoy) − ids en DB
       └─ ids_pendientes = dict.fromkeys(activos + errores + nuevos)  # sin duplicados
  │
  ├─ Crear NUM_WORKERS BrowserContexts con login independiente
  │    (viewport y user-agent asignados por índice rotativo entre 4 opciones cada uno)
  │
  ├─ asyncio.Queue  pedidos_queue    (maxsize=QUEUE_MAXSIZE)
  ├─ asyncio.Queue  resultados_queue (sin límite)
  │
  ├─ Task: persistencia_worker(resultados_queue)   ← 1 sola task, sin Lock contention
  ├─ Tasks: scraper_worker × NUM_WORKERS
  ├─ Task: _fill()   ← llena pedidos_queue + coloca N sentinel None al final (uno por worker)
  │
  ├─ asyncio.gather(fill_task, *worker_tasks)
  ├─ resultados_queue.put(None)   ← sentinel que termina persistencia_worker
  │
  ├─ Dead-letter: hasta 3 pases con 1 worker y MAX_REINTENTOS=2
  │    (reintenta pedidos que siguen en errores después del gather principal)
  │
  └─ Resumen JSON: tiempo_total_min, pedidos_procesados, pedidos_error,
                   tasa_exito_pct, pedidos_por_minuto
       exit(0) si tasa ≥ 95% · exit(1) si menor
```

> **Path de la base de datos:** resuelto con BUG-007.
> `get_db_path()` calcula la ruta absoluta a `data/pedidos.db`
> usando `Path(__file__).parent.parent / "data" / "pedidos.db"`,
> independientemente del directorio desde el que se ejecute el script.

---

## Lógica de selección de modo por pedido

Antes de navegar al detalle, `procesar_pedido()` consulta la DB para
determinar qué modo aplicar:

```
¿El pedido existe en la DB?
  NO  → modo = "completo"
  SÍ  → ¿Algún subpedido tiene cantidades_definitivas=0
          Y estado en ESTADOS_CERRADOS?
          SÍ → modo = "con_cantidades"
          NO → modo = "solo_estado"
```

**ESTADOS_FIJAN_CANTIDADES** (frozenset conservado como referencia histórica del dominio — ya no determina el modo):
```
"pendiente de confirmación"
"pendiente de envío (pago inmediato)"
"pendiente de envío (crédito)"
"pendiente de envío (contra entrega)"
"pendiente de entrega"
"enviado"
"período contable"
"completado"
"cancelado"
"comentado"
```

**ESTADOS_CERRADOS** (frozenset, subconjunto del anterior):
```
"completado"
"cancelado"
"comentado"
```

### Qué hace cada modo

| Modo | Navega con | Extrae | Persiste |
|---|---|---|---|
| `completo` | `networkidle` + scroll | info_general, subpedidos (expandidos), timeline, info_entrega, estadisticas, gestion_dif, detalle_dif, registro_ops | INSERT OR REPLACE en `pedidos`; DELETE+INSERT en `subpedidos`, `lineas_pedido`, `timeline_pedido`; marca `scraping_completo=1` |
| `con_cantidades` | `domcontentloaded` | subpedidos (expandidos), timeline, info_entrega, estadisticas, gestion_dif, detalle_dif, registro_ops | UPDATE `cantidad_entregada` + `estado` + `cantidades_definitivas=1` en `subpedidos`; reemplaza `timeline_pedido`; **no marca** `scraping_completo=1` |
| `solo_estado` | `domcontentloaded` | estado de subpedidos (sin expandir), timeline, estadisticas, gestion_dif, detalle_dif, registro_ops | UPDATE `estado` en `subpedidos`; reemplaza `timeline_pedido`; marca `scraping_completo=1` solo si todos los subpedidos están en ESTADOS_CERRADOS |

---

## Helper de normalización numérica

```python
def to_num(val: str) -> float | None:
    """Convierte "1.234,56" → 1234.56. Retorna None si falla.

    Args:
        val: Cadena en formato numérico español (punto como
             separador de miles, coma como decimal).

    Returns:
        float si la conversión es exitosa, None si falla
        (cadena vacía, texto no numérico, None literal).
    """
    try:
        cleaned = val.strip().replace(".", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, AttributeError):
        return None
```

Esta función existe en el scraper pero **solo se usa para `cantidad_comprada`
y `cantidad_entregada`**. Todos los demás campos monetarios se almacenan como
TEXT sin convertir. El ETL debe aplicar esta misma lógica para normalizar el
resto de los campos a REAL.

---

## Esquema de base de datos

Base de datos: `data/pedidos.db` · SQLite · modo WAL · `busy_timeout=5000ms` · `foreign_keys=ON`

### Tabla `pedidos`

Cabecera del pedido. Una fila por pedido.

| Columna | Tipo | Notas |
|---|---|---|
| `id_pedido` | TEXT PK | Identificador único del pedido |
| `fecha` | TEXT | Fecha de creación (YYYY-MM-DD) |
| `hora` | TEXT | Hora de creación |
| `servicio_cliente` | TEXT | Nombre del cliente o servicio |
| `vendedor` | TEXT | Vendedor asignado |
| `forma_pago` | TEXT | Forma de pago |
| `comprobante` | TEXT | Número de comprobante |
| `nombre_empresa` | TEXT | Razón social del cliente |
| `nit` | TEXT | NIT del cliente |
| `metodo_entrega` | TEXT | Método de entrega |
| `destinatario` | TEXT | Nombre del destinatario |
| `telefono` | TEXT | Teléfono del destinatario |
| `direccion_envio` | TEXT | Dirección de envío |
| `observaciones` | TEXT | Observaciones generales |
| `alistador_pedido` | TEXT | Alistador asignado al pedido |
| `inspector_pedido` | TEXT | Inspector asignado al pedido |
| `movil_cliente` | TEXT | Celular del cliente |
| `despachador` | TEXT | Despachador asignado |
| `hora_entrega` | TEXT | Hora de entrega registrada |
| `obs_entrega` | TEXT | Observaciones de entrega |
| `entrega_ruta_tag` | TEXT | Tag de ruta de entrega |
| `entrega_descuento_tag` | TEXT | Tag de descuento en entrega |
| `hay_diferencia` | INTEGER DEFAULT 0 | 1 si el pedido tiene diferencias |
| `scraping_completo` | INTEGER DEFAULT 0 | 1 cuando se completa el primer scraping completo |
| `actualizado_en` | TEXT | Timestamp UTC ISO de la última actualización |

> `scraping_completo` se inserta en `0` en modo `completo` y se actualiza a `1`
> al final de la misma transacción. En modo `solo_estado` se marca `1` solo
> cuando todos los subpedidos están en ESTADOS_CERRADOS. El modo `con_cantidades`
> **no** modifica `scraping_completo`.

### Tabla `subpedidos`

Un pedido puede tener uno o más subpedidos.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK → `pedidos` | — |
| `numero_subpedido` | TEXT | Número del subpedido |
| `tipo_subpedido` | TEXT | Tipo (ej. normal, especial) |
| `estado` | TEXT | Estado actual del subpedido |
| `inicio_alistamiento` | TEXT | Fecha/hora de inicio de alistamiento |
| `alistamiento_completado` | TEXT | Fecha/hora de fin de alistamiento |
| `alistador` | TEXT | Operario de alistamiento |
| `inicio_inspeccion` | TEXT | Fecha/hora de inicio de inspección |
| `inspeccion_completada` | TEXT | Fecha/hora de fin de inspección |
| `inspector` | TEXT | Operario de inspección |
| `cantidades_definitivas` | INTEGER DEFAULT 0 | 1 cuando las cantidades ya son definitivas (migración) |

**Índice:** `idx_subpedidos_pedido ON subpedidos(id_pedido)`

### Tabla `lineas_pedido`

Productos por subpedido. Una fila por producto por subpedido.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK → `pedidos` | — |
| `numero_subpedido` | TEXT | Subpedido al que pertenece |
| `tipo_subpedido` | TEXT | Tipo del subpedido |
| `nombre_producto` | TEXT | Nombre del producto |
| `referencia` | TEXT | Referencia interna |
| `codigo_barras` | TEXT | Código de barras |
| `presentacion` | TEXT | Presentación / unidad |
| `almacen` | TEXT | Almacén de origen |
| `cantidad_comprada` | REAL | Cantidad pedida |
| `cantidad_entregada` | REAL | Cantidad efectivamente entregada |
| `precio_unitario` | TEXT | ⚠️ Formato texto español ("1.234,56") |
| `descuento` | TEXT | ⚠️ Formato texto |
| `precio_descuento` | TEXT | ⚠️ Formato texto |
| `monto_pagar` | TEXT | ⚠️ Formato texto |
| `monto_final` | TEXT | ⚠️ Formato texto |
| `iva` | TEXT | ⚠️ Formato texto |
| `peso_total` | TEXT | ⚠️ Formato texto |
| `observaciones` | TEXT | Observaciones de la línea |
| `numero_caja` | TEXT | Número de caja (columna agregada por migración) |
| `tipo` | TEXT | Tipo de línea (columna agregada por migración) |

**Índice:** `idx_lineas_pedido ON lineas_pedido(id_pedido, numero_subpedido)`

> ⚠️ **Deuda ETL:** los campos marcados están en formato texto español. La
> función `to_num()` del scraper convierte este formato a float pero solo se
> aplica a `cantidad_comprada` y `cantidad_entregada`. La normalización del
> resto es responsabilidad de la Etapa 2 (ETL).

### Tabla `timeline_pedido`

Línea de tiempo de pasos por pedido.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK → `pedidos` | — |
| `numero_subpedido` | TEXT | Subpedido asociado (ver nota) |
| `paso` | INTEGER | Número de orden del paso |
| `titulo` | TEXT | Nombre del paso (ej. "Alistamiento") |
| `fecha_hora` | TEXT | Fecha y hora del paso |
| `completado` | INTEGER DEFAULT 0 | 1 si el paso está completado |

> `numero_subpedido` existe en el schema pero **no se popula** en los modos
> `con_cantidades` ni `solo_estado` al insertar. El timeline es a nivel de
> pedido, no de subpedido individual. Ver `docs/decisions.md`.

### Tabla `estadisticas_monto`

Totales y conceptos financieros del pedido. Reemplazada en cada actualización.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK NOT NULL → `pedidos` | — |
| `orden` | INTEGER | Orden de aparición del concepto |
| `concepto` | TEXT | Nombre del concepto (ej. "Total pedido") |
| `concepto_tag` | TEXT | Tag CSS del concepto |
| `monto_pagar` | TEXT | ⚠️ Formato texto |
| `monto_final` | TEXT | ⚠️ Formato texto |
| `diferencia` | TEXT | ⚠️ Formato texto |

**Índice:** `idx_estadisticas_pedido ON estadisticas_monto(id_pedido)`

### Tabla `gestion_diferencias`

Resumen de diferencias. Solo se popula si `hay_diferencia=1` en el pedido.
Reemplazada en cada actualización.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK NOT NULL → `pedidos` | — |
| `total_pagar_pedido` | TEXT | ⚠️ Formato texto |
| `monto_final_pagar` | TEXT | ⚠️ Formato texto |
| `monto_pagado` | TEXT | ⚠️ Formato texto |
| `monto_diferencia` | TEXT | ⚠️ Formato texto |

**Índice:** `idx_gestion_dif_pedido ON gestion_diferencias(id_pedido)`

### Tabla `detalle_diferencias`

Desglose por producto de las diferencias. Solo se popula si `hay_diferencia=1`.
Reemplazada en cada actualización.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK NOT NULL → `pedidos` | — |
| `nombre_producto` | TEXT | — |
| `especificacion` | TEXT | — |
| `tipo` | TEXT | Tipo de diferencia |
| `precio_unitario` | TEXT | ⚠️ Formato texto |
| `descuento` | TEXT | ⚠️ Formato texto |
| `precio_descuento` | TEXT | ⚠️ Formato texto |
| `cantidad_pedido` | TEXT | ⚠️ Formato texto |
| `cantidad_entregada` | TEXT | ⚠️ Formato texto |
| `diferencia_cantidad` | TEXT | ⚠️ Formato texto |
| `monto_pagar_pedido` | TEXT | ⚠️ Formato texto |
| `monto_final_pagar` | TEXT | ⚠️ Formato texto |
| `iva` | TEXT | ⚠️ Formato texto |
| `monto_diferencia` | TEXT | ⚠️ Formato texto |

**Índice:** `idx_detalle_dif_pedido ON detalle_diferencias(id_pedido)`

### Tabla `registro_operaciones`

Log de acciones realizadas sobre el pedido. Reemplazada en cada actualización.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT FK NOT NULL → `pedidos` | — |
| `momento` | TEXT | Fecha y hora de la acción |
| `usuario` | TEXT | Usuario que realizó la acción |
| `tipo_usuario` | TEXT | Rol del usuario |
| `accion` | TEXT | Descripción de la acción |
| `referencia` | TEXT | Referencia adicional |

**Índice:** `idx_registro_ops_pedido ON registro_operaciones(id_pedido)`

### Tabla `errores`

Pedidos que fallaron el scraping. Disponibles para reintento automático.
Los registros de pedidos ya completos se limpian al inicio de cada ejecución.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Clave interna |
| `id_pedido` | TEXT | ID del pedido que falló (sin FK formal) |
| `momento` | TEXT | Timestamp ISO del fallo |
| `detalle` | TEXT | Descripción del error |

**Índice:** `idx_errores_pedido ON errores(id_pedido)`

---

## Relaciones entre tablas

```
pedidos (id_pedido PK)
  ├── subpedidos           (id_pedido FK)
  ├── lineas_pedido        (id_pedido FK · numero_subpedido)
  ├── timeline_pedido      (id_pedido FK)
  ├── estadisticas_monto   (id_pedido FK)
  ├── gestion_diferencias  (id_pedido FK)  ← solo si hay_diferencia=1
  ├── detalle_diferencias  (id_pedido FK)  ← solo si hay_diferencia=1
  └── registro_operaciones (id_pedido FK)

errores (id_pedido referencia lógica, sin FK formal declarada)
```

---

## Logging JSONL

Cada evento emite una línea JSON al stdout y a `logs/scraper.log`:

```json
{
  "ts":          "2026-05-22T10:30:00.123Z",
  "level":       "INFO",
  "worker_id":   2,
  "id_pedido":   "2053125504",
  "duracion_ms": 4821,
  "event":       "pedido_ok",
  "msg":         "modo=completo | 2 subpedidos | intento 1"
}
```

`worker_id` y `id_pedido` son `null` en eventos del proceso principal.

**Catálogo de eventos:**

| Evento | Nivel | Quién lo emite | Descripción |
|---|---|---|---|
| `scraper_iniciado` | INFO | main | Arranque con modo y rango de fechas |
| `db_init` | INFO | init_db | Base de datos lista |
| `errores_limpios` | INFO | main | Errores de pedidos ya completos eliminados |
| `login_ok` | INFO | login | Autenticación exitosa |
| `login_error` | WARNING | login | Fallo de autenticación (con reintento) |
| `session_expired` | WARNING | procesar_pedido | Sesión expirada mid-scraping; re-login |
| `pagina_extraida` | INFO | obtener_lista_pedidos | Página de lista procesada |
| `lista_completa` | INFO | obtener_lista_pedidos | Total de IDs obtenidos |
| `ids_filtrados` | INFO | main | Resumen de IDs por carril (modo completo) |
| `pedido_ok` | INFO | procesar_pedido | Pedido extraído con éxito |
| `pedido_error` | WARNING | procesar_pedido | Fallo en un intento (con reintento pendiente) |
| `pedido_error` | ERROR | procesar_pedido | Fallo total tras MAX_REINTENTOS |
| `db_guardado` | INFO | persistencia_worker | Pedido persistido en SQLite |
| `db_error` | ERROR | persistencia_worker | Fallo al persistir (rollback ejecutado) |
| `rate_limited` | INFO | scraper_worker | HTTP 429 recibido; esperando Retry-After o RATE_LIMIT_WAIT_S |
| `circuit_open` | WARNING | scraper_worker | Circuit breaker abierto; cooldown activo |
| `circuit_closed` | INFO | scraper_worker | Circuit breaker cerrado; reanudando |
| `worker_terminated` | ERROR | scraper_worker | Worker terminó por exceso de reaperturas |
| `worker_exception` | ERROR | main | Excepción no capturada en un worker |
| `dead_letter_pass` | INFO | main | Pase de reintento de dead-letter queue |
| `scraper_finalizado` | INFO | main | Resumen final con métricas |

---

## ETL — Normalización y VIEWs

**Script:** `etl/etl_principal.py`
**Ejecución:** automática después de cada ciclo
incremental via `scraper/actualizar_pedidos.bat`
**Base de datos:** `data/pedidos.db` (misma que el scraper)

### Columnas normalizadas (_num)

| Tabla | Columnas REAL agregadas |
|---|---|
| `lineas_pedido` | `precio_unitario_num`, `descuento_num`, `precio_descuento_num`, `monto_pagar_num`, `monto_final_num`, `iva_num`, `peso_total_num` |
| `estadisticas_monto` | `monto_pagar_num`, `monto_final_num`, `diferencia_num` |
| `gestion_diferencias` | `total_pagar_pedido_num`, `monto_final_pagar_num`, `monto_pagado_num`, `monto_diferencia_num` |
| `detalle_diferencias` | `precio_unitario_num`, `descuento_num`, `precio_descuento_num`, `cantidad_pedido_num`, `cantidad_entregada_num`, `diferencia_cantidad_num`, `monto_pagar_pedido_num`, `monto_final_pagar_num`, `iva_num`, `monto_diferencia_num` |

### VIEWs analíticas

| VIEW | Propósito |
|---|---|
| `v_pedidos_activos` | Pedidos con al menos un subpedido abierto |
| `v_pedidos_cerrados` | Pedidos con todos los subpedidos cerrados |
| `v_inventario_comprometido` | Productos comprometidos en pedidos previos al picking |
| `v_diferencias_resumen` | Pedidos con diferencias y montos numéricos |
| `v_rendimiento_operadores` | Operaciones de alistamiento e inspección por operador |
| `v_variaciones_timeline` | Títulos únicos del timeline para estandarización |
| `v_variaciones_operaciones` | Acciones únicas del registro para estandarización |

### Estados que comprometen inventario
Pendiente de confirmación
Pendiente de pago (pago inmediato)
Pendiente de pago (crédito)
Pendiente de pago (contra entrega)
Pendiente de recolección
Aprobación de Pagos
Pendiente de envío (pago inmediato)
Pendiente de envío (crédito)
Pendiente de envío (contra entrega)
Pendiente de entrega
En inspección

---

## Deuda técnica pendiente

| # | Problema | Impacto | Prioridad |
|---|---|---|---|
| 1 | Campos monetarios almacenados como TEXT | No se pueden hacer SUM/AVG directamente en SQL | Alta — bloquea ETL |
| 2 | ✅ Verificar lógica de `con_cantidades` vs regla de negocio de cantidades al cierre | Cantidades pueden actualizarse antes del cierre del subpedido | Alta — Resuelto 2026-05-23 |
| 3 | ✅ `headless=False` hardcodeado en `main()` | No puede correr en servidor sin pantalla | Alta — Resuelto 2026-05-23 |
| 4 | ✅ `--desde` default apunta a `2025-05-01` | Debería ser `2026-05-01` | Alta — Resuelto 2026-05-23 |
| 5 | ✅ `db_path = "pedidos.db"` relativo al directorio de ejecución | La DB se crea donde se ejecuta el script, no necesariamente en `data/` | Media — Resuelto 2026-05-23 |
| 6 | `actualizado_en` no se refresca en modo `con_cantidades` | El dashboard no puede saber cuándo fue la última actualización de ese pedido | Media |
| 7 | `numero_subpedido` no se popula en `timeline_pedido` en modos incrementales | No se puede correlacionar paso de timeline con subpedido específico | Media |
| 8 | Match por `codigo_barras` en `con_cantidades` falla silenciosamente si el campo está vacío | UPDATE sin efecto, sin warning visible | Baja |
| 9 | Docstring del archivo menciona "3 workers" pero CONFIG tiene 5 | Confusión para quien lea el código | Baja — actualizar docstring |
| 10 | ✅ `argparse` expone el nombre real de la empresa en `--help` | Confidencialidad comprometida en logs públicos | Baja — Resuelto 2026-05-23 |
| 11 | ✅ `stop_queues` independientes causaban condición de carrera: 0 pedidos procesados | Scraper terminaba sin procesar ningún pedido — tasa 0% | Alta — Resuelto 2026-05-23 |

Decisiones y resolución → `docs/decisions.md`