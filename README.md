![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/Licencia-MIT-green)

# dashboard_pedidos

**Actualizado:** 2026-09-27

Pipeline de datos en tres etapas para un sistema administrativo interno (SPA Vue.js + Element
Plus) de una empresa colombiana que gestiona su propia operación logística. Un scraper asíncrono
extrae pedidos, subpedidos, líneas de producto, línea de tiempo de alistamiento, registros de
pago y registros operacionales; un ETL los normaliza; y un dashboard de 18 páginas los publica.
Todo vive en SQLite: **33 tablas y 24 VIEWs**. Los datos recopilados servirán como insumo para
un futuro sistema de predicción de demanda.

Sobre esa base se construyó además un **módulo de inventario** que cruza el catálogo del sistema
administrativo, el reporte del sistema de bodega y el layout físico para estimar el inventario
de picking, y que cierra un ciclo de conteo físico completo (el dashboard emite la hoja, se sube
contada, el scheduler la ingiere y calcula el IRA).

---

## Problema que resuelve

El sistema administrativo de la empresa no expone una API: todos los datos de pedidos viven en
una SPA que renderiza tablas paginadas. Los equipos de operaciones no tienen visibilidad
analítica sobre:

- **Estado de pedidos:** cuántos están activos, en qué etapa van, cuáles están bloqueados.
- **Inventario comprometido:** qué productos y cantidades están en pedidos abiertos sin despachar.
- **Ciclos de alistamiento e inspección:** cuánto tiempo tarda cada subpedido por etapa.
- **Diferencias en envíos:** frecuencia, montos y productos con mayor incidencia.
- **Rendimiento por operador:** tiempos y volúmenes por alistador e inspector.

Este pipeline extrae esa información de forma automatizada, la normaliza en SQLite
y la publica en un dashboard. Corre desatendido cada hora.

---

## Arquitectura técnica

```
systemd timer (dashboard_pedidos_ciclo.timer, cada hora)
        │
        ▼
scraper/actualizar_pedidos.sh
        │
        ▼
scraper/scraper_principal.py
        │
   ┌────┴───────────────────────────────────────────┐
   │         Modo incremental (cada hora)           │
   │  1. Activos en DB   →  ids_activos[]           │
   │  2. Con errores     →  ids_error[]             │
   │  3. Nuevos (watermark) → ids_nuevos[]          │
   │  4. Re-extracción del histórico por tandas     │
   │     (modo completo forzado en memoria)         │
   └────┬───────────────────────────────────────────┘
        │  ids_pendientes[] (unión sin duplicados)
        ▼
  ┌──────────────────────────────────────────────┐
  │   asyncio.Queue (pedidos_queue)              │
  │   sentinel None al final, uno por worker     │
  └──┬──────┬───────┬─────────┬────────┬─────────┘
     │      │       │         │        │
   W-0    W-1     W-2       W-3      W-4   ← 5 workers (SCRAPER_NUM_WORKERS)
     │      │       │         │        │     BrowserContext independiente,
     ┴──────┴───────┴─────────┴────────┘     página nueva cada N pedidos,
                                             circuit breaker + re-login;
                                             un worker que no logra
                                             loguearse reintenta durante
                                             el run sin tumbar el ciclo
                │
                ▼
        resultados_queue
                │
                ▼
      persistencia_worker()    ← tarea dedicada, sin Lock contention
                │
                ▼
        data/pedidos.db (SQLite · modo WAL)
                │
                ▼
   scraper.inventario + scraper.bochica   ← descarga de las dos fuentes de inventario
                │
                ▼
   scraper.cambios_inventario + scraper.movimientos_bochica   ← captura diaria de
                │                                                movimientos (admin + Bochica)
                ▼
   inventario.persistencia   ← cruce con el layout de bodega y escritura de tablas derivadas
                │
                ▼
   integraciones.sheets_cliente   ← exportación a Google Sheets
                │
                ▼
        etl/etl_principal.py   ← normalización de montos + VIEWs analíticas + ANALYZE
                │
                ▼
   scripts/respaldar_db.py   ← respaldo diario verificado (una vez por día)
```

`actualizar_pedidos.sh` encadena esos nueve pasos en ese orden (más una pasada de
mantenimiento el día 1 de cada mes) y deja en el log el código de salida y la duración de
cada uno. El ETL va después de todo lo que captura para que normalice a `_num` lo recién
extraído. Con la configuración de producción (5 workers, página nueva cada 10 pedidos, V8
optimizado para tamaño) un ciclo de ~1.800 pedidos tarda **24-27 min** y su pico de memoria
es de **~1,4 GB** (medido el 2026-09-27; antes ~4,9 GB).

El pipeline termina en `dashboard/app.py` (Streamlit), que consume `data/pedidos.db` **solo por
lectura**. Puede leer las tablas del scraper, pero los montos siempre como valor numérico
normalizado por el ETL (`*_num` o sus VIEWs), nunca como el texto del origen — lo verifica un
test (DEC-157).

### Principios de diseño

El proyecto se rige por **SOLID, DRY, KISS, YAGNI y bajo acoplamiento**,
aplicados de forma verificable, no declarativa: las etapas se comunican
solo por contratos de datos (SQLite + VIEWs, nunca imports cruzados), el
dominio compartido vive una única vez en `comun/`, y las optimizaciones
requieren medición previa que las justifique. Cada decisión de diseño no
trivial queda registrada con su trade-off antes de implementarse.

Las referencias `DEC-NNN`, `BUG-NNN` y `AUD-*` que aparecen en este README y en
el código remiten a ese registro de decisiones, que es interno y no se publica
con el repositorio.

---

## Estructura del repositorio

```text
dashboard_pedidos/
├── .claude/                  # Configuración Claude Code
├── .github/
│   └── workflows/
│       └── ci.yml            # CI: ruff+mypy, pytest (ubuntu), gitleaks
├── comun/                    # Módulo común — único origen de verdad del dominio
│   ├── __init__.py           # to_num, get_db_path, ESTADOS_*, umbrales
│   ├── entregas.py           # los tres formatos de hora_entrega y el OTIF
│   ├── motivos.py            # clasificación del motivo de cancelación
│   ├── arena.py              # demanda y punto de reorden de Arena por ciudad
│   └── reposicion.py         # punto de reorden y cantidad sugerida
├── data/                     # Datos locales — gitignored
│   ├── pedidos.db            # Base de datos SQLite
│   ├── debug/                # HTMLs de debug — pueden contener PII
│   └── errors/               # Screenshots de errores del scraper
├── .streamlit/
│   └── config.toml           # Red, puerto y telemetría del dashboard
├── dashboard/                # Etapa 3 — visualización (Streamlit)
│   ├── __init__.py
│   ├── app.py                # Entry point: tema + st.navigation
│   ├── theme.py              # Paleta e inyección de CSS global
│   ├── db.py                 # Capa de lectura de SQLite (solo lectura)
│   ├── filtros.py            # Filtros globales compartidos por session_state
│   ├── tareas_db.py          # Tareas manuales — data/tareas.db
│   ├── conteos_io.py         # Recibe y archiva las hojas de conteo
│   └── pages/                # 18 páginas, en cinco secciones:
│                             #   principal  → estado del área, centro de alertas
│                             #   pedidos    → consolidado
│                             #   inventario → bodega vs. sistema, salud,
│                             #     ABC-XYZ, mapa, plan de conteo, faltantes,
│                             #     Arena (checkpoint DEC-118), catálogo no-Arena
│                             #   operación  → tiempos y productividad, excepciones
│                             #   fuera del alcance del área → ciclo de vida,
│                             #     cumplimiento de entrega, ventas, cobranza
│                             # (+ tareas / calidad de datos, que se oculta sola
│                             #   si no hay pendientes)
├── inventario/               # Cruce bodega ↔ sistema y plan de conteo
│   ├── layout.py             # Clasificación de ubicaciones del layout
│   ├── normalizador.py       # Carga y normalización de las 3 fuentes
│   ├── comparacion.py        # Cruce y cálculo de sobrante
│   ├── ubicaciones.py        # Línea SKU-posición y mapa de bodega
│   ├── clasificacion.py      # ABC-XYZ jerárquico + ABC global
│   ├── salud.py              # Cobertura, movimiento y quiebres
│   ├── operacion.py          # Tiempos de ciclo y capacidad del equipo
│   ├── conteos.py            # Ingesta de conteos físicos desde Excel
│   ├── cancelaciones.py      # Mercancía alistada que se canceló
│   ├── hallazgos.py          # 14 detectores de calidad de datos
│   ├── alertas.py            # Centro de excepciones
│   ├── arena.py              # Inventario del módulo Arena por ciudad (DEC-118)
│   ├── catalogo_no_arena.py  # Catálogo sin Arena: venta, ABC-XYZ e inventario
│   └── persistencia.py       # Esquema, VIEWs y escritura transaccional
├── integraciones/            # Consumidores externos de pedidos.db
│   ├── sheets_arena.py       # Consultas del inventario Arena y pedidos previos
│   │                         # a picking, por ciudad (12, Bogotá incluida)
│   └── sheets_cliente.py     # Escritura a Google Sheets (cuenta de servicio)
├── docs/                     # Contexto interno del proyecto — no se publica en este repo
│   ├── integral.md           # Visión, problema y objetivo de negocio
│   ├── structure.md          # Arquitectura técnica y esquema de datos
│   ├── agent.md              # Instrucciones de comportamiento para Claude
│   ├── decisions.md          # Registro de decisiones y bugs conocidos
│   └── testing.md            # Estrategia de tests y fixtures
├── etl/                      # Etapa 2 — normalización y VIEWs SQL
│   ├── __init__.py           # paquete importable por tests/
│   └── etl_principal.py      # normalización de montos y VIEWs
├── logs/                     # Logs de ejecución — gitignored
├── scraper/                  # Etapa 1 — extracción de datos (paquete de 11 módulos)
│   ├── __init__.py           # paquete importable por tests/
│   ├── archive/              # Versión inicial del scraper — solo referencia
│   ├── migrations/           # Scripts de migración de única ejecución
│   │   ├── reset_timeline_incompleto.py
│   │   ├── corregir_entrega_ruta.py
│   │   ├── separar_descuento_tipo.py
│   │   ├── backfill_cambios_inventario.py
│   │   ├── cargar_inicial_movimientos_bochica.py
│   │   ├── borrar_lineas_fantasma_total.py
│   │   └── mover_peso_entregado.py  # repara `observaciones` corrida por «Peso entregado»
│   ├── actualizar_pedidos.sh # ciclo horario (systemd); el .bat es referencia histórica
│   ├── config.py             # CONFIG, credenciales, locks, rate limit, logging JSONL
│   ├── db.py                 # esquema SQLite, migraciones y watermark
│   ├── extractores.py        # login (espera el formulario definitivo de Cognito),
│   │                         # listado, extractores del detalle leídos por
│   │                         # etiqueta/encabezado, navegación vía Vue Router y
│   │                         # espera de las secciones que cargan tarde
│   ├── persistencia.py       # persistencia_worker + helpers transaccionales
│   ├── workers.py            # selección de modo, scraping por pedido, circuit breaker,
│   │                         # página nueva cada N pedidos
│   ├── orquestador.py        # main(): carriles, dead-letter, resumen y CLI
│   │                         # (incluye re-extracción dirigida con --ids-archivo)
│   ├── inventario.py         # descarga del catálogo del sistema administrativo
│   ├── bochica.py            # descarga del reporte del sistema de bodega
│   ├── cambios_inventario.py # captura diaria de "Cambios de inventario" del admin
│   ├── movimientos_bochica.py # captura diaria de movimientos de BOCHICA (DEC-123)
│   └── scraper_principal.py  # entry point + facade de re-exports
├── tests/                    # Suite de tests
│   ├── conftest.py           # Fixtures y opciones de pytest
│   ├── unit/                 # Tests unitarios sin I/O externo
│   ├── integration/          # Tests de integración con SQLite temporal
│   └── e2e/                  # Tests con browser real — lentos
├── scripts/
│   ├── hooks/
│   │   └── pre-commit        # gate: ruff check + format + mypy comun
│   ├── systemd/              # units del dashboard, del ciclo horario y del monitor
│   ├── iniciar_dashboard.sh  # arranque del dashboard (dashboard_pedidos.service)
│   ├── respaldar_db.py       # respaldo diario verificado de las dos bases
│   ├── respaldar_remoto.py   # copia del respaldo fuera de esta máquina (opcional, por SSH)
│   ├── monitor_laptop_tailscale.py  # registro de conexión de un equipo remoto al tailnet
│   ├── notificar_fallo_scheduler.sh # aviso ante fallo del ciclo (requiere sesión gráfica)
│   ├── notificar_fallo_email.py     # aviso por email (opcional, por SMTP)
│   ├── *.bat / *.ps1         # versiones Windows — referencia histórica
│   └── verify_db.py          # utilitario de inspección manual de la DB
├── .env                      # Credenciales locales — gitignored
├── .env.example              # Plantilla de variables de entorno
├── .gitignore
├── CLAUDE.md                 # Guía de arranque para Claude Code — no se publica
├── pyproject.toml            # empaquetado editable + configuración de ruff y mypy
├── pytest.ini                # configuración de pytest y marcadores
├── README.md
├── requirements.txt          # dependencias de runtime
└── requirements-dev.txt      # ruff, mypy, pytest-cov
```

---

## Esquema de base de datos

| Tabla | Propósito |
|---|---|
| `pedidos` | Cabecera del pedido: cliente, vendedor, forma de pago, destino |
| `subpedidos` | Subpedidos con estado, tiempos de alistamiento e inspección |
| `lineas_pedido` | Productos por subpedido: cantidades, precios, almacén, caja |
| `timeline_pedido` | Línea de tiempo de pasos por pedido para análisis de ciclos |
| `estadisticas_monto` | Totales financieros del pedido: montos, descuentos, diferencias |
| `gestion_diferencias` | Resumen de diferencias entre lo pedido y lo despachado |
| `detalle_diferencias` | Desglose por producto de las diferencias detectadas |
| `registro_operaciones` | Log de acciones realizadas sobre el pedido: quién hizo qué y cuándo |
| `registros_pago` | Comprobantes de pago: banco, cuenta receptora, monto, revisor y estado de revisión |
| `catalogo_productos` | Puente (referencia, código de barras) → IDs de producto del catálogo del sistema administrativo (la escribe `inventario/`) |
| `errores` | Pedidos que fallaron el scraping, disponibles para reintento automático |
| `meta` | Watermark de la última corrida OK del incremental |

Además, el módulo `inventario/` mantiene sus propias tablas derivadas
(comparación bodega ↔ sistema, línea SKU-posición, mapa de posiciones,
clasificación ABC-XYZ, salud, tiempos de operación, conteos físicos,
alertas y hallazgos de calidad), todas reconstruidas en cada corrida del
scheduler salvo las que guardan historia.

En total: **33 tablas y 24 VIEWs** (verificado en vivo el 2026-09-27). El
dashboard abre la base solo para lectura; las VIEWs son el contrato de datos
preferido.

---

## Instalación

Entorno de referencia: **Linux Mint / Ubuntu** (migrado desde Windows en
2026-09, ver `docs/decisions.md`). El proyecto no depende de nada específico
de Windows — todas las rutas usan `pathlib`, sin separadores hardcodeados.

```bash
# 1. Clonar el repositorio
git clone https://github.com/Zero-Fyah/dashboard_pedidos.git
cd dashboard_pedidos

# 2. Crear entorno virtual con Python 3.12 (DEC-015) e instalar dependencias
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, mypy, pytest-cov (desarrollo)
pip install -e .                      # paquete en modo editable — requerido por los tests

# 3. Instalar el navegador que usa Playwright + sus dependencias de sistema
playwright install --with-deps chromium

# 4. Configurar credenciales
cp .env.example .env
# Editar .env con usuario, contraseña y URLs reales

# 5. Activar el gate de calidad pre-commit (una vez por clon, DEC-016)
git config core.hooksPath scripts/hooks
cp .confidencial.example .confidencial   # términos que el hook bloquea (DEC-042)

# 6. (Opcional) Automatizar el ciclo horario y el arranque del dashboard
# Unidades systemd listas en scripts/systemd/ — instrucciones de instalación
# en el comentario de cada archivo (dashboard_pedidos.service,
# dashboard_pedidos_ciclo.service + .timer). Reemplazan a lo que en Windows
# era el Task Scheduler ejecutando scraper/actualizar_pedidos.bat.
```

Nota para quien viene de la versión anterior en Windows: `scripts/*.bat` y
`scripts/notificar_fallo_scheduler.ps1` se conservan solo como referencia
histórica — la operación real corre por los `.sh` equivalentes y las
unidades systemd de `scripts/systemd/`.

---

## Uso

```bash
# (con el .venv activado — DEC-015)

# Modo completo — procesa todos los pedidos del rango desde cero
python scraper/scraper_principal.py --desde 2026-01-01

# Modo incremental — actualiza activos, reintenta errores
# y captura pedidos nuevos desde la última corrida OK
python scraper/scraper_principal.py --modo incremental

# Modo mantenimiento — re-extrae pedidos entregados cuya información de
# entrega el origen dejó de renderizar. Selecciona desde la base, no recorre
# el listado, y no avanza el watermark. Corre el día 1 de cada mes.
python scraper/scraper_principal.py --modo mantenimiento

# Re-extracción dirigida — exactamente los IDs del archivo (uno por línea), en
# modo completo, sin recorrer el listado ni mover el watermark
python scraper/scraper_principal.py --modo completo --ids-archivo ids.txt

# Normalizar montos y crear VIEWs analíticas (como módulo — E-7)
python -m etl.etl_principal

# Migración puntual: resetear pedidos sin timeline para re-scraping (ver BUG-012)
python scraper/migrations/reset_timeline_incompleto.py

# Iniciar el dashboard
python -m streamlit run dashboard/app.py
```

Al finalizar, el scraper imprime un resumen JSON con tiempo total, modo, pedidos procesados,
errores, tasa de éxito y — en modo incremental — el desglose por carril (activos, reintentos,
nuevos, re-extracción). Las métricas se miden sobre los resultados del propio run (pedidos persistidos con
COMMIT exitoso), no sobre el estado acumulado en la DB. Código de salida `0` si la tasa de
éxito es ≥ 95 %, `1` si es menor.

---

## Cómo funciona el modo incremental

El modo incremental evita recorrer todo el historial en cada ejecución mediante cuatro
carriles independientes:

- **Activos:** consulta la DB directamente para obtener pedidos con `scraping_completo = 1`
  que tienen al menos un subpedido en estado no cerrado. No abre ninguna página del servidor.
- **Errores:** lee la tabla `errores` para identificar pedidos que fallaron en ejecuciones
  previas y aún no están completos, y los encola para reintento.
- **Nuevos:** consulta el servidor desde el watermark de última corrida exitosa
  (`meta.ultima_corrida_ok` − 1 día, con tope de 7 días hacia atrás — DEC-012), descarta
  los IDs ya presentes en la DB y encola únicamente los pedidos nuevos. Un outage del
  scheduler de varios días se recupera solo en la primera corrida exitosa posterior.

- **Re-extracción:** una tanda de pedidos del histórico (`SCRAPER_REEXTRACCION_POR_CICLO`)
  a los que les faltan columnas agregadas después de su captura; se procesan en modo
  completo forzado en memoria y el carril se apaga solo cuando no queda ninguno.

Los cuatro conjuntos se combinan con `dict.fromkeys()` para eliminar duplicados y se procesan
en una sola pasada de workers paralelos.

---

## Estado del proyecto

| Etapa | Estado |
|---|---|
| Etapa 1 — Scraper (extracción) | ✅ Completa |
| Etapa 2 — ETL (normalización + VIEWs SQL) | ✅ Completa |
| Etapa 3 — Dashboard (visualización) | ✅ Construida — 18 páginas |
| Módulo de inventario (cruce bodega ↔ sistema + ciclo de conteo) | ✅ Construido |
| Módulo Arena (inventario por ciudad) | ✅ Construido — alcance ampliado bajo checkpoint de autorización (DEC-118) |

El pipeline corre desatendido cada hora con un timer de systemd (Linux) y la
suite tiene **1.224 tests** (1.212 passed + 12 e2e que requieren `--e2e`, medido
2026-09-27), todos marcados `unit`, `integration` o `e2e`.

El dashboard tiene dos propósitos: el trabajo diario del área de inventarios
—el principal— y la consulta y el análisis general para las demás áreas, que
además es la base de las propuestas de mejora al sistema administrativo.

---

## Nota de privacidad

Las credenciales de acceso **nunca se incluyen en el código**. Se leen desde variables de
entorno al iniciar el proceso:

```bash
SCRAPER_USUARIO=tu_usuario
SCRAPER_PASSWORD=tu_password
```

Copiar `.env.example` a `.env`, completar los valores reales y verificar que `.env` esté en
`.gitignore` (ya incluido en este repositorio).

---

## Licencia

MIT
