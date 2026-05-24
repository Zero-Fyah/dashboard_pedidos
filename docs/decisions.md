# dashboard_pedidos — Decisiones de diseño y bugs conocidos

Registro acumulativo de decisiones técnicas ya tomadas y problemas identificados.
Este archivo es propiedad exclusiva del Arquitecto. La IA lo lee, nunca lo modifica.

**Regla:** no reabrir una decisión registrada aquí sin documentar la justificación
en la misma sesión en que se propone el cambio.

---

## Decisiones de diseño

### DEC-001 — SQLite como base de datos principal

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** usar SQLite con modo WAL como única capa de persistencia para la
Etapa 1 (Scraper).

**Justificación:** el volumen de pedidos de la operación actual (decenas a pocos
cientos por día) está dentro de la capacidad de SQLite. La concurrencia de
escritura es mínima porque existe un único `persistencia_worker`. La concurrencia
de lectura (dashboard consultando mientras el scraper escribe) está resuelta con
WAL mode. PostgreSQL añadiría infraestructura sin beneficio real a esta escala.

**Alternativa descartada:** PostgreSQL — sobrecarga operativa innecesaria para
el volumen actual.

**DEC-010 resuelta:** SQLite confirmado como capa
analítica. No se migrará a DuckDB. Ver DEC-010 para
justificación y umbral de reevaluación futura.

---

### DEC-002 — Scraping como única fuente de datos

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** extraer datos exclusivamente via Playwright sobre el sistema
administrativo. No hay integración directa con la base de datos del sistema
ni acceso a API.

**Justificación:** el sistema administrativo no expone API y no hay acceso
a su base de datos subyacente. El scraping es la única opción técnicamente
viable dado el contexto.

**Riesgo aceptado:** fragilidad ante cambios de UI del sistema administrativo.
Mitigado con circuit breaker, dead-letter queue y selectores semánticos.

---

### DEC-003 — Tres modos de extracción por pedido

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** antes de navegar al detalle de un pedido, consultar la DB para
elegir el modo de menor costo posible:
- `completo` → pedido nuevo; extrae las 8 secciones completas
- `con_cantidades` → subpedido con `cantidades_definitivas=0` en estado que fija cantidades; actualiza estado y cantidades entregadas
- `solo_estado` → solo actualiza estado; el más liviano

**Justificación:** en modo incremental, la mayoría de pedidos activos solo
necesitan una actualización de estado. Navegar y extraer todo en cada ciclo
sería ineficiente y aumentaría la carga sobre el sistema administrativo.

**Ver también:** BUG-005 — posible desalineación entre la condición de activación
de `con_cantidades` y la regla de negocio de cantidades solo al cierre.

---

### DEC-004 — Worker de persistencia único y serializado

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** un único `persistencia_worker` consume la `resultados_queue` y
escribe en SQLite. Los workers de scraping no escriben directamente a la DB.

**Justificación:** elimina la contención de escritura entre workers. Con SQLite
y múltiples writers concurrentes, incluso en WAL mode, hay riesgo de `SQLITE_BUSY`.
Un writer único con transacciones atómicas es más simple y más robusto.

---

### DEC-005 — asyncio.Queue como canal productor/consumidor

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** usar `asyncio.Queue` para desacoplar la obtención de IDs (`_fill`),
el procesamiento (`scraper_worker`) y la persistencia (`persistencia_worker`).

**Justificación:** patrón estándar para pipelines async en Python. Permite que
cada componente opere a su propio ritmo sin bloquear al productor. El sentinel
`None` en `resultados_queue` cierra limpiamente el ciclo de vida.

---

### DEC-006 — Circuit breaker por worker (no global)

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** cada worker tiene su propio circuit breaker independiente.
Parámetros activos: 5 fallos consecutivos → cooldown 90s → máx 3 reaperturas → worker termina.

**Justificación:** un fallo en un worker (sesión expirada, timeout puntual) no
debe paralizar a los demás. El aislamiento por worker permite que el resto continúe
mientras el afectado se recupera o termina. Un circuit breaker global bloquearía
todo el pipeline ante un fallo individual.

---

### DEC-007 — Dead-letter queue con hasta 3 pases

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** al finalizar el gather principal, reintentar pedidos que siguen
en la tabla `errores` en hasta 3 pases adicionales, con 1 worker y
`MAX_REINTENTOS=2` (reducido respecto al valor normal de 5).

**Justificación:** algunos pedidos fallan por condiciones transitorias (rate
limiting puntual, timeout de red). Una segunda oportunidad con menor paralelismo
reduce la carga sobre el sistema y aumenta la tasa de éxito sin complejidad
adicional en el código principal.

---

### DEC-008 — Fecha de corte 2026-05-01

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** el proyecto solo procesa pedidos creados desde el 2026-05-01.

**Justificación:** decisión de negocio. Los pedidos anteriores no forman parte
del alcance analítico del proyecto.

**Implicación técnica:** el argumento `--desde` del scraper debe usar esta
fecha como default. Actualmente tiene un bug (ver BUG-002).

---

### DEC-009 — Cinco workers paralelos (NUM_WORKERS=5)

**Fecha:** 2026-05-22
**Estado:** ✅ Activa

**Decisión:** el valor operativo de `NUM_WORKERS` en CONFIG es 5.

**Justificación:** con 5 workers y una pausa de 1.2s entre pedidos por worker,
el throughput estimado es ~4 pedidos/minuto por worker = ~20 pedidos/minuto en
total. Suficiente para la carga histórica en tiempo razonable y para el modo
incremental diario.

**Nota de deuda técnica:** el docstring de `scraper_principal.py` menciona
"3 workers" en la estimación de tiempos. Está desactualizado. Corregir en la
próxima edición del archivo.

---

### DEC-010 — Evaluación SQLite vs DuckDB para el ETL

**Fecha:** 2026-05-22
**Estado:** ✅ Resuelta — 2026-05-23

**Decisión tomada:** mantener SQLite como capa
analítica para el ETL y el dashboard.

**Justificación basada en datos reales:** tras la
carga histórica completa (2026-05-01 → 2026-05-23),
el volumen de la DB es:

- `pedidos`: 3.065 filas
- `subpedidos`: 3.501 filas
- `lineas_pedido`: 92.191 filas
- `timeline_pedido`: 15.023 filas
- `estadisticas_monto`: 35.424 filas
- `gestion_diferencias`: 474 filas
- `detalle_diferencias`: 649 filas
- `registro_operaciones`: 20.647 filas
- **Total: ~167.000 filas**

Proyectando un año completo (~12x el volumen actual),
`lineas_pedido` llegaría a ~1,1 millones de filas —
manejable para SQLite con los índices correctos.
DuckDB agregaría complejidad sin beneficio real a
esta escala. Reevaluar si `lineas_pedido` supera
los 5 millones de filas en el futuro.

**Decisión pendiente (original):** determinar si SQLite es suficiente como capa analítica
para el dashboard o si conviene migrar a DuckDB.

**Criterios para decidir:**

| Condición | Recomendación |
|---|---|
| Volumen total < 100.000 filas en `lineas_pedido` | SQLite es suficiente |
| Volumen total > 100.000 filas o VIEWs con múltiples JOINs y agregaciones | Evaluar DuckDB |
| Herramienta de dashboard con conector SQLite nativo | Mantener SQLite |
| Herramienta de dashboard sin conector SQLite | DuckDB o exportar a formato compatible |

**Sobre DuckDB:** es embebido como SQLite (sin servidor), pero usa almacenamiento
columnar y tiene un optimizador de queries analíticas. Leer y escribir el mismo
archivo `.db` de SQLite desde DuckDB no es posible; implicaría una migración
o una capa ETL que exporte a un archivo DuckDB separado.

**Implicación:** la tecnología del dashboard puede depender de esta decisión.
Resolverla antes de diseñar las VIEWs del ETL.

### DEC-011 — Arquitectura del ETL

**Fecha:** 2026-05-24
**Estado:** ✅ Activa

**Decisión:** el ETL vive en `etl/etl_principal.py`,
corre después de cada ciclo incremental del scraper
via `actualizar_pedidos.bat`, y escribe en la misma
`data/pedidos.db`.

**Justificación:**
- Separación de responsabilidades: el scraper extrae,
  el ETL normaliza y agrega valor analítico.
- Misma DB: el volumen (~167.000 filas) no justifica
  una DB separada. Las VIEWs y columnas `_num` viven
  junto a los datos crudos.
- Idempotente: puede re-ejecutarse sin efectos
  secundarios.

**Alcance:**
- 24 columnas `_num` REAL en 4 tablas
  (lp=7, em=3, gd=4, dd=10)
- 7 VIEWs analíticas
- Integrado al scheduler cada 2 horas

---

## Bugs conocidos

### BUG-001 — `headless=False` hardcodeado

**Archivo:** `scraper/scraper_principal.py` · línea ~2250
**Severidad:** 🔴 Alta — bloquea ejecución en servidor sin pantalla
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:**
```python
browser = await pw.chromium.launch(headless=False, slow_mo=50)
```
Valor hardcodeado. No puede correr en entorno sin GUI (servidor, CI).

**Corrección propuesta:**

Paso 1 — Agregar las claves al `ConfigDict` (TypedDict):
```python
class ConfigDict(TypedDict):
    # ... claves existentes ...
    HEADLESS: bool
    SLOW_MO:  int
```

Paso 2 — Agregar los valores al dict `CONFIG`:
```python
CONFIG: ConfigDict = {
    # ... valores existentes ...
    "HEADLESS": False,  # False para desarrollo local, True para producción
    "SLOW_MO":  50,     # 0 para producción
}
```

Paso 3 — Reemplazar la llamada a `launch`:
```python
browser = await pw.chromium.launch(
    headless=CONFIG["HEADLESS"],
    slow_mo=CONFIG["SLOW_MO"],
)
```

Esto también permite sobreescribir los valores via `.env` si se desea,
siguiendo el patrón ya establecido en CONFIG.

---

### BUG-002 — Default de `--desde` apunta a 2025-05-01

**Archivo:** `scraper/scraper_principal.py` · línea ~2514
**Severidad:** 🔴 Alta — puede procesar pedidos fuera del alcance del proyecto
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:**
```python
parser.add_argument("--desde", default="2025-05-01", ...)
```
El default es un año antes de la fecha de corte real del proyecto.

**Corrección:**
```python
parser.add_argument("--desde", default="2026-05-01", ...)
```

---

### BUG-003 — `actualizado_en` no se refresca en modo `con_cantidades`

**Archivo:** `scraper/scraper_principal.py` · `persistencia_worker` · rama `con_cantidades`
**Severidad:** 🟡 Media — el dashboard no puede saber cuándo fue la última actualización de ese pedido
**Estado:** 🔲 Pendiente

**Problema:** los modos `completo` y `solo_estado` actualizan `actualizado_en`
en `pedidos`. El modo `con_cantidades` no lo hace, por lo que un pedido
actualizado en ese modo quedará con un timestamp desactualizado.

**Corrección propuesta:** agregar el UPDATE **antes del COMMIT**, dentro de
la misma transacción, en la rama `con_cantidades` de `persistencia_worker`:

```python
# Añadir antes de: await db.execute("COMMIT")
await db.execute(
    "UPDATE pedidos SET actualizado_en = ? WHERE id_pedido = ?",
    (datetime.now(timezone.utc).isoformat(), id_pedido),
)
# await db.execute("COMMIT")  ← ya existente, no duplicar
```

---

### BUG-004 — `numero_subpedido` no se popula en `timeline_pedido`

**Archivo:** `scraper/scraper_principal.py` · `persistencia_worker` · ramas `con_cantidades` y `solo_estado`
**Severidad:** 🟡 Media — impide correlacionar pasos del timeline con subpedido específico
**Estado:** 🔲 Pendiente · Requiere decisión de diseño

**Problema:** la columna `numero_subpedido` existe en `timeline_pedido` pero
ninguna de las ramas de persistencia la popula al insertar. El timeline se
trata como dato del pedido completo, no del subpedido individual.

**Decisión requerida antes de corregir:**

- **Opción A — Timeline a nivel de pedido (comportamiento actual):**
  La columna `numero_subpedido` en `timeline_pedido` es innecesaria.
  Acción: eliminar la columna via migración y actualizar `docs/structure.md`.

- **Opción B — Timeline a nivel de subpedido:**
  Modificar `extraer_timeline()` para asociar cada paso al subpedido
  correspondiente, y actualizar los INSERT en las tres ramas de persistencia.

Confirmar con el Arquitecto qué representa el timeline del sistema administrativo
(¿es por pedido padre o por subpedido?) antes de implementar.

---

### BUG-005 — Modo `con_cantidades` puede activarse antes del cierre del subpedido

**Archivo:** `scraper/scraper_principal.py` · `procesar_pedido` · lógica de selección de modo
**Severidad:** 🔴 Alta — posible violación de regla de negocio
**Estado:** ✅ Resuelto — 2026-05-23
**Decisión tomada:** Opción B — las cantidades
entregadas se registran únicamente cuando el
subpedido alcanza estado cerrado (completado,
cancelado o comentado). La condición de activación
de con_cantidades fue cambiada de
ESTADOS_FIJAN_CANTIDADES a ESTADOS_CERRADOS.
ESTADOS_FIJAN_CANTIDADES se conserva en el código
con comentario explicativo. Se extrajo
determinar_modo() como función pura testeable y
build_arg_parser() para testabilidad del argparse.

**Problema:** la condición de activación del modo `con_cantidades` es:

```python
elif any(
    cd == 0 and estado.lower() in ESTADOS_FIJAN_CANTIDADES
    for estado, cd in subs_db
):
    modo = "con_cantidades"
```

`ESTADOS_FIJAN_CANTIDADES` incluye estados intermedios como `"enviado"`,
`"pendiente de entrega"` y `"período contable"`. Esto significa que
`cantidad_entregada` puede actualizarse **antes** de que el subpedido esté cerrado,
lo que podría violar la siguiente regla de negocio:

> `cantidad_entregada` se actualiza únicamente cuando el subpedido alcanza
> estado cerrado (`Completado`, `Cancelado` o `Comentado`).

**Dos interpretaciones posibles — confirmar cuál es correcta:**

**Opción A — El comportamiento actual es intencional:**
Los estados en `ESTADOS_FIJAN_CANTIDADES` son aquellos a partir de los cuales
las cantidades ya no cambian, aunque el subpedido aún no esté formalmente cerrado.
En ese caso la regla de negocio tal como está escrita en `docs/integral.md` es
imprecisa y debe actualizarse para reflejar esta distinción.

**Opción B — La regla de negocio es correcta y el código debe corregirse:**
Las cantidades solo se registran al cierre formal del subpedido. La corrección
implica dos cambios:

```python
# 1. Cambiar la condición de activación de con_cantidades:
elif any(
    cd == 0 and estado.lower() in ESTADOS_CERRADOS  # antes: ESTADOS_FIJAN_CANTIDADES
    for estado, cd in subs_db
):
    modo = "con_cantidades"
```

```python
# 2. Revisar si ESTADOS_FIJAN_CANTIDADES sigue siendo necesario en otro contexto.
#    Si no, puede eliminarse del código.
```

**Impacto de la opción B en el ciclo de vida:** con esta corrección, un subpedido
en estado intermedio (ej. "enviado") siempre recibirá modo `solo_estado`.
Solo cuando el subpedido transite a `completado`, `cancelado` o `comentado`
se activará `con_cantidades` para registrar las cantidades definitivas y marcar
`cantidades_definitivas=1`. En el siguiente ciclo, ese subpedido ya no activará
ningún modo de actualización (caerá en `solo_estado` y `cantidades_definitivas`
ya será `1`, por lo que no cumple la condición de `con_cantidades`).

---

### BUG-006 — Match por `codigo_barras` falla silenciosamente si está vacío

**Archivo:** `scraper/scraper_principal.py` · `persistencia_worker` · rama `con_cantidades`
**Severidad:** 🟠 Baja-Media — UPDATE sin efecto, sin warning visible
**Estado:** 🔲 Pendiente

**Problema:** el UPDATE de `cantidad_entregada` usa `codigo_barras` como
clave de match. Si el campo está vacío o es `NULL`, el UPDATE no matchea
ninguna fila y la cantidad no se actualiza, sin ningún error ni warning.

El bloque afectado en `persistencia_worker` (rama `con_cantidades`):
```python
await db.execute(
    "UPDATE lineas_pedido SET cantidad_entregada = ? "
    "WHERE id_pedido = ? AND numero_subpedido = ? "
    "AND codigo_barras = ?",
    (linea["cantidad_entregada"], id_pedido, num_sub, linea["codigo_barras"]),
)
```

**Corrección propuesta:** capturar el cursor y verificar `rowcount`:
```python
cursor = await db.execute(
    "UPDATE lineas_pedido SET cantidad_entregada = ? "
    "WHERE id_pedido = ? AND numero_subpedido = ? "
    "AND codigo_barras = ?",
    (linea["cantidad_entregada"], id_pedido, num_sub, linea["codigo_barras"]),
)
if cursor.rowcount == 0:
    log_event(
        "update_sin_match",
        level="WARNING",
        id_pedido=id_pedido,
        msg=(
            f"cantidad_entregada no actualizada — "
            f"codigo_barras vacío o no encontrado en "
            f"subpedido {num_sub}"
        ),
    )
```

---

### BUG-007 — `db_path` relativo al directorio de ejecución

**Archivo:** `scraper/scraper_principal.py` · `main()` · línea ~2235
**Severidad:** 🟡 Media — la DB se crea donde se ejecuta el script, no en `data/`
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:**
```python
db_path = "pedidos.db"
```
Si el script se ejecuta desde `scraper/` en lugar de la raíz del proyecto,
la DB se crea en `scraper/pedidos.db` en lugar de `data/pedidos.db`.

**Corrección propuesta:**
```python
db_path = str(Path(__file__).parent.parent / "data" / "pedidos.db")
```
El path es ahora relativo a la ubicación del script (`scraper/scraper_principal.py`),
no al directorio de trabajo. `parent.parent` sube de `scraper/` a la raíz,
luego desciende a `data/pedidos.db`.

Asegurarse también de que `data/` exista antes de abrir la conexión:
```python
Path(db_path).parent.mkdir(parents=True, exist_ok=True)
```

---

### BUG-008 — `argparse` expone el nombre real de la empresa

**Archivo:** `scraper/scraper_principal.py` · línea ~2511
**Severidad:** 🟠 Baja — confidencialidad; visible en `--help` y en logs
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:**
```python
parser = argparse.ArgumentParser(description="Scraper de pedidos Calabaza Pets")
```

**Corrección:**
```python
parser = argparse.ArgumentParser(
    description="Scraper de pedidos — sistema administrativo interno"
)
```

---

### BUG-009 — Condición de carrera: 0 pedidos procesados

**Archivo:** `scraper/scraper_principal.py` · `_fill()`
y `scraper_worker()`
**Severidad:** 🔴 Crítica — el scraper termina sin procesar
ningún pedido
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:** `_fill()` encolaba todos los IDs en
`pedidos_queue` (capacidad 100) y luego enviaba
inmediatamente señales de parada a todas las
`stop_queues` antes de que cualquier worker consumiera
un ID. Al arrancar, los workers veían ambas colas con
ítems disponibles. `asyncio.wait` devolvía `stop_task`
como completado, el worker devolvía el ID y terminaba.
Resultado: 0 pedidos procesados, 0 errores, tasa 0%.

**Corrección aplicada:** eliminadas las `stop_queues`
independientes. El sentinel de parada (`None`) ahora
se coloca al final de `pedidos_queue`, después de todos
los IDs reales, uno por worker. `scraper_worker`
simplificado: `id_pedido = await pedidos_queue.get();
if id_pedido is None: break`. El sentinel llega a cada
worker solo después de que todos los IDs reales fueron
consumidos, eliminando la condición de carrera.

---

### BUG-010 — Selector de paginación incorrecto: solo se procesaba la primera página

**Archivo:** `scraper/scraper_principal.py` · `obtener_lista_pedidos()`
**Severidad:** 🔴 Crítica — el scraper solo extraía 10 pedidos (primera página) ignorando las restantes
**Estado:** ✅ Resuelto — 2026-05-23

**Problema:** el selector para detectar el botón
"siguiente página" era `button.btn-next.is-last`.
El botón real en el sistema administrativo nunca
tiene la clase `is-last` — solo tiene `btn-next`.
Al no encontrar el selector, el scraper entraba al
`else: break` y terminaba tras la primera página.
Con ~300 páginas de pedidos, solo se procesaban
los 10 primeros.

**Corrección aplicada:** selector cambiado a
`button.btn-next`. La condición de última página
se detecta ahora con `aria-disabled="true"`, que
es el mecanismo real de Element Plus:

```python
btn_next = await page.query_selector("button.btn-next")
if btn_next:
    aria_disabled = await btn_next.get_attribute("aria-disabled")
    if aria_disabled == "true":
        break
    await btn_next.click()
    ...
else:
    break
```

**Validación:** carga completa ejecutada 2026-05-23.
3.065 pedidos procesados en 330 minutos. Tasa: 100%.

---

### BUG-011 — Bucle infinito en ETL al normalizar valores NULL

**Archivo:** `etl/etl_principal.py` · `normalizar_montos()`
**Severidad:** 🔴 Crítica — el ETL no terminaba nunca
**Estado:** ✅ Resuelto — 2026-05-24

**Problema:** el patrón de batch usaba
`WHERE {col_num} IS NULL LIMIT 500` para identificar
filas pendientes de normalizar. Cuando `to_num()`
retorna `None` para un valor inválido (cadena vacía,
texto no numérico), el UPDATE escribe `NULL` en la
columna `_num`. En la siguiente iteración, la misma
query vuelve a encontrar esas filas porque siguen
teniendo `NULL`. El loop nunca terminaba.

**Corrección aplicada:** reemplazar el patrón
`WHERE col_num IS NULL` por paginación sobre `id`:

```python
last_id = 0
while True:
    rows = await (await db.execute(
        f"SELECT id, {col_src} FROM {tabla} "
        f"WHERE id > ? ORDER BY id LIMIT 500",
        (last_id,)
    )).fetchall()
    if not rows:
        break
    for row_id, val in rows:
        await db.execute(
            f"UPDATE {tabla} SET {col_num} = ? WHERE id = ?",
            (to_num(val) if val is not None else None, row_id),
        )
    last_id = rows[-1][0]
    await db.commit()
```

Este patrón avanza siempre hacia adelante por `id`
y termina cuando no hay más filas, independientemente
de si el valor normalizado es `NULL` o no.

---

## Pendientes de resolución antes de Etapa 2

Ordenados por prioridad. Los de severidad 🔴 deben resolverse antes de la
carga inicial de datos.

| Prioridad | # | Pendiente | Impacto si no se resuelve |
|---|---|---|---|
| ✅ Antes de carga inicial | BUG-002 | Corregir default `--desde` a 2026-05-01 | Procesa pedidos fuera del alcance |
| ✅ Antes de carga inicial | BUG-008 | Reemplazar nombre real de la empresa | Confidencialidad comprometida en logs |
| ✅ Antes de ETL | BUG-005 | Confirmar regla de cantidades al cierre | Posible corrupción lógica de datos |
| ✅ Antes de producción | BUG-001 | `headless` configurable | No puede correr en servidor |
| ✅ Antes de ETL | BUG-007 | `db_path` relativo al script | DB en ubicación incorrecta |
| 🟡 Antes de dashboard | BUG-003 | Refrescar `actualizado_en` en `con_cantidades` | Dashboard sin timestamp fiable |
| 🟡 Antes de dashboard | BUG-004 | Decidir nivel del timeline (pedido vs subpedido) | Vista de ciclos incompleta |
| 🟠 Cuando aplique | BUG-006 | Warning en match vacío de `codigo_barras` | Cantidades no actualizadas sin aviso |
| ✅ | DEC-010 | Decidir SQLite vs DuckDB — SQLite confirmado | Resuelto 2026-05-23 |
| ✅ | BUG-009 | Condición de carrera — sentinel en pedidos_queue | Resuelto 2026-05-23 |
| ✅ | BUG-011 | Bucle infinito en ETL — patrón WHERE col_num IS NULL | Resuelto 2026-05-24 |