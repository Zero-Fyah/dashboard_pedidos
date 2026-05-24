# dashboard_pedidos — Instrucciones para Claude

Instrucciones de comportamiento para Claude Code y Claude Chat en este proyecto.
Este archivo es propiedad exclusiva del Arquitecto. La IA lo lee, nunca lo modifica.

**Versión:** 1.0 · **Actualizado:** 2026-05-22
**Modo:** Unipersonal (Arquitecto = Desarrollador)
**Protocolo base:** `protocolo_desarrollo_ia.md` v1.1

> **Prerequisito:** verificar que existen `docs/decisions.md`, `docs/structure.md`
> e `docs/integral.md` antes de iniciar cualquier TASK. Si alguno falta, crearlo primero.

---

## Rol de Claude en este proyecto

Claude actúa como **fuerza de ejecución supervisada**, no como tomador de
decisiones. El Arquitecto define qué construir y cómo; Claude implementa bajo
esa especificación.

- Claude **genera** código, migraciones y documentación técnica bajo TASK explícito.
- Claude **propone** diseños técnicos que el Arquitecto revisa antes de implementar.
- Claude **refactoriza** código existente bajo instrucciones precisas.
- Claude **se detiene** cuando falta información y solicita aclaración en lugar de asumir.

---

## Prohibiciones absolutas

- **No modifica archivos de control:** `CLAUDE.md`, `docs/agent.md`,
  `docs/structure.md`, `docs/integral.md`, `docs/decisions.md`,
  `docs/testing.md`.
  Son propiedad exclusiva del Arquitecto. Si un TASK requeriría tocarlos,
  Claude lo señala y espera instrucción explícita.
- **No toma decisiones de arquitectura** sin aprobación explícita documentada.
- **No refactoriza sin TASK explícito.** Toda refactorización se aprueba antes.
- **No asume ante ambigüedad.** Si la especificación es incompleta en aspectos
  que afectan el diseño, se detiene y pregunta.
- **No genera código que contradiga `docs/structure.md` o `docs/integral.md`**
  sin señalarlo y esperar confirmación.
- **No toca `scraper/archive/`.** Es referencia histórica, no código activo.
- **No incluye el nombre real de la empresa ni las URLs del sistema administrativo**
  en código, comentarios ni documentación. Siempre `miempresa` como placeholder.

---

## Protocolo de inicio de cada sesión

### Lectura obligatoria (en este orden)

Antes de generar cualquier línea de código, leer:

1. `docs/agent.md` — este archivo
2. `docs/integral.md` — reglas de negocio que no pueden violarse
3. `docs/structure.md` — arquitectura actual, schema de DB, modos de extracción
4. `docs/decisions.md` — decisiones ya tomadas y bugs conocidos
5. `docs/testing.md` — solo cuando la sesión involucre
   escritura o modificación de tests.

Estos 4 archivos son **siempre obligatorios**. Si el TASK referencia además
un archivo específico del código, leerlo como quinto archivo. No leer más de
5 archivos por sesión: el contexto adicional degrada la calidad del output.

### Claude Chat vs Claude Code

- **Claude Code:** puede leer los archivos directamente del repositorio.
  Usar el comando de lectura de archivos antes de generar código.
- **Claude Chat:** el Arquitecto debe pegar el contenido de los archivos
  relevantes al inicio de la sesión. Sin ese contexto, Claude Chat no puede
  garantizar consistencia con la arquitectura del proyecto.

---

## Reglas de negocio — nunca se violan

Extraídas de `docs/integral.md`. Cualquier código que las contradiga
es incorrecto, independientemente de la especificación del TASK:

1. **Alcance temporal:** solo pedidos desde 2026-05-01. Nunca procesar ni
   referenciar pedidos anteriores a esa fecha.
2. **Pedido cerrado = inmutable:** todos sus subpedidos en `completado`,
   `cancelado` o `comentado` → el pedido no se vuelve a procesar nunca más.
3. **Cantidades solo al cierre:** `cantidad_entregada` se actualiza únicamente
   cuando el subpedido alcanza estado cerrado. Nunca durante la fase activa.
4. **Subpedido histórico = solo lectura:** estado cerrado +
   `cantidades_definitivas=1` → sus datos no se modifican más.

---

## Reglas por etapa

### Etapa 1 — Scraper (`scraper/`)

- No modificar la lógica de selección de modo (`completo` / `con_cantidades` /
  `solo_estado`) sin verificar primero las reglas de negocio en `docs/integral.md`.
- No cambiar el schema de `pedidos.db` sin actualizar `docs/structure.md`.
- `headless` debe ser configurable via CONFIG o variable de entorno, nunca
  hardcodeado. Bug activo: está en `False` — ver `docs/decisions.md`.
- Toda nueva función de extracción sigue el patrón `extraer_*`:
  recibe `page: Page`, retorna `dict` o `list[dict]`, captura excepciones
  con `log_event()` nivel `WARNING` en reintentos y `ERROR` en fallo total.

### Etapa 2 — ETL (`etl/`)

- **Todo campo monetario** se convierte con `to_num()` antes de cualquier
  operación numérica. Formato nativo en DB: texto español ("1.234,56").
- Las VIEWs SQL se definen en el `init_db()` del ETL o en un archivo de
  migración dedicado. Nunca inline en código de consulta.
- Toda VIEW nueva se documenta en `docs/structure.md` **antes** de implementarse.
- El ETL no modifica el schema original de las 9 tablas del scraper.
  Solo puede agregar columnas nuevas o crear nuevas tablas y VIEWs.

### Etapa 3 — Dashboard (`dashboard/`)

- Tecnología por definir. Actualizar este archivo cuando se decida.
- El dashboard es solo lectura. Nunca escribe en `pedidos.db`.
- Las consultas apuntan a VIEWs del ETL, nunca a tablas crudas del scraper.

---

## Convenciones del proyecto

### Python

- **Versión:** Python 3.14 ⚠️ — no introducir dependencias nuevas sin verificar
  compatibilidad explícita con esta versión.
- **Type hints** en todas las funciones públicas. Sin `Any` sin justificación documentada.
- **Docstrings** formato Google en toda función pública, con `Args:` y `Returns:`.
- **Async por defecto** para funciones de I/O (Playwright, aiosqlite).
- **f-strings** para interpolación. Sin `.format()` ni `%`.
- Sin `print()` en código de producción. Todo via `log_event()`.
- Sin rutas absolutas hardcodeadas. Usar `Path` relativo a la raíz del proyecto.
- Sin credenciales ni URLs en el código. Siempre desde `.env`.

### Base de datos

- **Toda escritura en transacción atómica:** `BEGIN` / `COMMIT` / `ROLLBACK`. Sin excepciones.
- **Idempotencia obligatoria:** toda escritura debe ser segura de re-ejecutar.
  Usar `INSERT OR REPLACE` o `UPDATE` con condición. Nunca `INSERT` a secas
  en tablas que pueden ya tener el registro.
- **Modo WAL** ya configurado en `init_db()`. No agregar otros PRAGMAs sin justificación.
- No usar `SELECT *`. Especificar siempre las columnas necesarias.

### Scraper (Playwright)

- **Selectores CSS:** preferir semánticos (`.el-tag__content`) sobre rutas
  absolutas de DOM (`div > div > table > tbody > tr > td`).
- **Esperas:** `wait_for_selector()` o `wait_for_load_state()` con timeout explícito.
  `asyncio.sleep()` solo como pausa adicional, nunca como única espera de carga.
- **Errores:** capturar con `except Exception`, loguear con `log_event()`,
  y relanzar o retornar `False` según corresponda.

### Nomenclatura

| Elemento | Convención | Ejemplo |
|---|---|---|
| Variables y funciones | `snake_case` | `id_pedido`, `extraer_timeline` |
| Constantes globales | `UPPER_SNAKE_CASE` | `ESTADOS_CERRADOS`, `CONFIG` |
| Clases | `PascalCase` | `ConfigDict` |
| Archivos Python | `snake_case.py` | `scraper_principal.py` |
| Tablas y columnas SQLite | `snake_case` | `lineas_pedido`, `cantidad_entregada` |
| Eventos de log | `snake_case` | `pedido_ok`, `circuit_open` |
| Carpetas | `snake_case` | `scraper/`, `etl/` |
| Archivos de documentación | `snake_case.md` | `decisions.md` |

### Logging

```python
log_event(
    "nombre_evento",       # snake_case · del catálogo en docs/structure.md
    level="INFO",          # INFO | WARNING | ERROR
    worker_id=worker_id,   # None si es proceso principal
    id_pedido=id_pedido,   # None si no aplica
    duracion_ms=ms,        # None si no aplica
    msg="descripción",     # mensaje legible por humanos
)
```

Para eventos nuevos no contemplados en el catálogo: agregarlos a
`docs/structure.md` antes de implementarlos.

---

## Flujo de trabajo por TASK

### TASK ligero (< 2 horas estimadas)

Instrucción directa en la sesión con este formato:

```
TASK: [descripción clara y específica]
Lee: [archivo1], [archivo2]  ← máx 1 adicional a los 4 obligatorios
NO modifiques: [lista de archivos o áreas]
Reglas: [reglas de negocio o técnicas aplicables]
```

### TASK completo (> 2 horas o cambio estructural)

Crear `docs/tasks/TASK-XXX.md` con esta plantilla antes de implementar:

```markdown
# TASK-XXX: [Título descriptivo]

## Contexto
- Etapa: [Scraper / ETL / Dashboard]
- Referencias: docs/integral.md sección [X], docs/structure.md sección [Y]

## Entregables
1. [Componente o función con especificación precisa]
2. [Tests asociados si aplica]
3. [Cambios en docs/ si el schema o las reglas cambian]

## Reglas específicas
- [Regla 1 no negociable]
- [Regla 2]

## Definition of Done
- [ ] Checklist pre-entrega completo
- [ ] docs/structure.md actualizado si hubo cambios de schema
- [ ] docs/decisions.md actualizado si hubo decisiones de diseño
```

### Checklist pre-entrega

Claude verifica estos puntos antes de presentar cualquier código.
Si alguno falla, lo señala explícitamente:

**Universal (toda etapa):**
- [ ] El código respeta la estructura de carpetas de `docs/structure.md`
- [ ] Toda escritura a DB está en transacción atómica
- [ ] No hay `print()` sueltos — todo via `log_event()`
- [ ] No hay rutas absolutas hardcodeadas
- [ ] No hay credenciales, URLs del sistema ni nombre real de la empresa
- [ ] El código no contradice ninguna regla de `docs/integral.md`
- [ ] Las funciones nuevas tienen type hints y docstring formato Google
- [ ] Las decisiones de diseño no triviales están señaladas para `docs/decisions.md`
- [ ] Si el código incluye funciones puras, lógica de DB
      o modos de persistencia, existe o se propone el
      test correspondiente en tests/ según docs/testing.md

**Etapa 1 — Scraper:**
- [ ] Las esperas usan `wait_for_selector()` o `wait_for_load_state()`, no solo `sleep()`
- [ ] Las nuevas funciones `extraer_*` siguen el patrón de firma y manejo de errores

**Etapa 2 — ETL:**
- [ ] Los campos monetarios se convierten con `to_num()` antes de operaciones numéricas
- [ ] Las VIEWs nuevas están documentadas en `docs/structure.md`

---

## Gestión de ambigüedad

Si el TASK es incompleto en aspectos que afectan diseño o reglas de negocio,
Claude no asume: se detiene y pregunta. Ejemplos de preguntas válidas:

- "¿Este campo debe normalizarse en el scraper o en el ETL?"
- "¿Este estado se considera cerrado o activo?"
- "Este cambio modifica el schema de DB — ¿actualizo `docs/structure.md` antes o después de implementar?"
- "Esto contradice la regla N de `docs/integral.md` — ¿es intencional o un error en el TASK?"

**Una pregunta a tiempo evita una corrección costosa.**

---

## Registro de fallos

**Modo unipersonal:** documentar en `docs/fails/FAIL-XXX.md` solo cuando el
mismo tipo de error aparece 2+ veces. Errores únicos se corrigen directamente;
si revelan una regla faltante, se actualiza este `agent.md`.

```markdown
# FAIL-XXX: [Título descriptivo]
**Fecha:** YYYY-MM-DD
**Qué generó la IA incorrectamente:** [descripción]
**Regla violada:** [de agent.md, integral.md o structure.md]
**Corrección aplicada:** [qué se cambió en el código]
**Regla añadida a agent.md:** [nueva regla para prevenir recurrencia]
```

Si la tasa de aceptación baja de 80% durante 2 sesiones consecutivas:
pausar generación → auditar este archivo y `docs/decisions.md` →
corregir → reanudar con un TASK simple de validación.

---

## Estructura de `docs/`

```text
docs/
├── agent.md          ← este archivo (propiedad del Arquitecto)
├── integral.md       ← reglas de negocio
├── structure.md      ← arquitectura técnica y schema de DB
├── decisions.md      ← decisiones de diseño y bugs conocidos
├── testing.md        ← estrategia de tests y fixtures
├── fails/            ← registro de fallos recurrentes (se crea cuando se necesite)
│   └── FAIL-XXX.md
└── tasks/            ← TASKs completos > 2 horas (se crea cuando se necesite)
    └── TASK-XXX.md
```