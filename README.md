![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/Licencia-MIT-green)

# dashboard_pedidos

**Actualizado:** 2026-08-26

Pipeline de datos en tres etapas para un sistema administrativo interno (SPA Vue.js + Element
Plus) de una empresa colombiana que gestiona su propia operación logística. Un scraper asíncrono
extrae pedidos, subpedidos, líneas de producto, línea de tiempo de alistamiento, registros de
pago y registros operacionales; un ETL los normaliza; y un dashboard de 17 páginas los publica.
Todo vive en SQLite: **35 tablas y 24 VIEWs**. Los datos recopilados servirán como insumo para
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

Este pipeline extrae esa información de forma automatizada, la normaliza en 30 tablas SQLite
y la publica en un dashboard. Corre desatendido cada hora.

---

## Arquitectura técnica

```
Windows Task Scheduler
        │
        ▼
scraper/actualizar_pedidos.bat
        │
        ▼
scraper/scraper_principal.py
        │
   ┌────┴───────────────────────────────────────────┐
   │           Modo incremental (diario)            │
   │  1. Activos en DB   →  ids_activos[]           │
   │  2. Con errores     →  ids_error[]             │
   │  3. Nuevos (watermark) → ids_nuevos[]          │
   └────┬───────────────────────────────────────────┘
        │  ids_pendientes[] (unión sin duplicados)
        ▼
  ┌──────────────────────────────────────────────┐
  │   asyncio.Queue (pedidos_queue)              │
  │   sentinel None al final, uno por worker     │
  └──┬──────┬───────┬─────────┬────────┬─────────┘
     │      │       │         │        │
   W-0    W-1     W-2       W-3      W-4    W-5   ← 6 workers
     │      │       │         │        │      BrowserContext independiente
     ┴──────┴───────┴─────────┴────────┘      circuit breaker + re-login
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
   inventario.persistencia   ← cruce con el layout de bodega y escritura de tablas derivadas
                │
                ▼
   scraper.cambios_inventario + scraper.movimientos_bochica   ← captura diaria de
                │                                                movimientos (admin + Bochica)
                ▼
        etl/etl_principal.py   ← normalización de montos + VIEWs analíticas + ANALYZE
```

El mismo `.bat` encadena siete pasos en ese orden: el ETL va último para que normalice a
`_num` todo lo que los pasos anteriores acaban de capturar. Un ciclo completo tarda **~30-32
min** en producción real (auditoría de rendimiento, 2026-08-26 — navegación interna vía Vue
Router en vez de recargar la SPA por cada pedido).

El pipeline termina en `dashboard/app.py` (Streamlit), que consume `data/pedidos.db` **solo por
lectura** y, salvo excepción medida, a través de VIEWs.

### Principios de diseño

El proyecto se rige por **SOLID, DRY, KISS, YAGNI y bajo acoplamiento**,
aplicados de forma verificable, no declarativa: las etapas se comunican
solo por contratos de datos (SQLite + VIEWs, nunca imports cruzados), el
dominio compartido vive una única vez en `comun/`, y las optimizaciones
requieren medición previa que las justifique. Cada decisión de diseño no
trivial queda registrada con su trade-off antes de implementarse.

---

## Estructura del repositorio

```text
dashboard_pedidos/
├── .claude/                  # Configuración Claude Code
├── .github/
│   └── workflows/
│       └── ci.yml            # CI: ruff+mypy, pytest (ubuntu+windows), gitleaks
├── comun/                    # Módulo común — único origen de verdad del dominio
│   ├── __init__.py           # to_num, get_db_path, ESTADOS_*, umbrales
│   ├── entregas.py           # los tres formatos de hora_entrega y el OTIF
│   ├── motivos.py            # clasificación del motivo de cancelación
│   ├── arena.py              # dominio compartido del módulo Arena (DEC-117)
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
│   ├── db.py                 # Capa de lectura (VIEWs de SQLite)
│   ├── filtros.py            # Filtros globales compartidos por session_state
│   ├── tareas_db.py          # Tareas manuales — data/tareas.db
│   ├── conteos_io.py         # Recibe y archiva las hojas de conteo
│   └── pages/                # 17 páginas, en cuatro secciones:
│                             #   inventario → estado del área, alertas, bodega vs.
│                             #     sistema, salud, ABC-XYZ, mapa, plan de conteo,
│                             #     faltantes, Arena (módulo aparte, checkpoint DEC-118)
│                             #   pedidos    → consolidado, excepciones, productividad
│                             #   comercial  → ventas, cobranza
│                             #   fuera del alcance del área → ciclo de vida,
│                             #     cumplimiento de entrega
│                             # (+ tareas, que se oculta sola si no hay pendientes)
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
│   ├── hallazgos.py          # Detectores de calidad de datos
│   ├── alertas.py            # Centro de excepciones
│   ├── arena.py              # Inventario del módulo Arena por ciudad (DEC-117)
│   └── persistencia.py       # Esquema, VIEWs y escritura transaccional
├── docs/                     # Contexto persistente del proyecto
│   ├── integral.md           # Visión, problema y objetivo de negocio
│   ├── structure.md          # Arquitectura técnica y esquema de datos
│   ├── agent.md              # Instrucciones de comportamiento para Claude
│   ├── decisions.md          # Registro de decisiones y bugs conocidos
│   └── testing.md            # Estrategia de tests y fixtures
├── etl/                      # Etapa 2 — normalización y VIEWs SQL
│   ├── __init__.py           # paquete importable por tests/
│   └── etl_principal.py      # normalización de montos y VIEWs
├── logs/                     # Logs de ejecución — gitignored
├── scraper/                  # Etapa 1 — extracción de datos (paquete de 9 módulos)
│   ├── __init__.py           # paquete importable por tests/
│   ├── archive/              # Versión inicial del scraper — solo referencia
│   ├── migrations/           # Scripts de migración de única ejecución
│   │   ├── reset_timeline_incompleto.py
│   │   ├── corregir_entrega_ruta.py
│   │   ├── separar_descuento_tipo.py
│   │   ├── backfill_cambios_inventario.py
│   │   ├── cargar_inicial_movimientos_bochica.py
│   │   └── borrar_lineas_fantasma_total.py
│   ├── actualizar_pedidos.bat
│   ├── config.py             # CONFIG, credenciales, locks, rate limit, logging JSONL
│   ├── db.py                 # esquema SQLite, migraciones y watermark
│   ├── extractores.py        # login, listado de pedidos, extractores del detalle,
│   │                         # navegación interna vía Vue Router (DEC-124)
│   ├── persistencia.py       # persistencia_worker + helpers transaccionales
│   ├── workers.py            # selección de modo, scraping por pedido, circuit breaker
│   ├── orquestador.py        # main(): carriles, dead-letter, resumen y CLI
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
│   ├── iniciar_dashboard.bat # arranque del dashboard como tarea de Windows
│   └── verify_db.py          # utilitario de inspección manual de la DB
├── .env                      # Credenciales locales — gitignored
├── .env.example              # Plantilla de variables de entorno
├── .gitignore
├── CLAUDE.md                 # Guía de arranque para Claude Code
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
| `catalogo_productos` | Catálogo de productos del sistema administrativo, por producto y almacén |
| `errores` | Pedidos que fallaron el scraping, disponibles para reintento automático |
| `meta` | Watermark de la última corrida OK del incremental |

Además, el módulo `inventario/` mantiene sus propias tablas derivadas
(comparación bodega ↔ sistema, línea SKU-posición, mapa de posiciones,
clasificación ABC-XYZ, salud, tiempos de operación, conteos físicos,
alertas y hallazgos de calidad), todas reconstruidas en cada corrida del
scheduler salvo las que guardan historia.

En total: **35 tablas y 24 VIEWs** (verificado en vivo el 2026-08-26). El
dashboard consume las VIEWs como contrato de datos estable, no las tablas
crudas del scraper.

---

## Instalación

```bash
# 1. Clonar el repositorio
git clone https://github.com/Zero-Fyah/dashboard_pedidos.git
cd dashboard_pedidos

# 2. Crear entorno virtual con Python 3.12 (DEC-015) e instalar dependencias
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, mypy, pytest-cov (desarrollo)
pip install -e .                      # paquete en modo editable — requerido por los tests

# 3. Instalar el navegador que usa Playwright
playwright install chromium

# 4. Configurar credenciales
copy .env.example .env
# Editar .env con usuario, contraseña y URLs reales

# 5. Activar el gate de calidad pre-commit (una vez por clon, DEC-016)
git config core.hooksPath scripts/hooks

# 6. (Opcional) Programar ejecución incremental automática
# Registrar scraper/actualizar_pedidos.bat en Windows Task Scheduler
# (el .bat invoca .venv\Scripts\python.exe — requiere el paso 2)
```

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

# Normalizar montos y crear VIEWs analíticas (como módulo — E-7)
python -m etl.etl_principal

# Migración puntual: resetear pedidos sin timeline para re-scraping (ver BUG-012)
python scraper/migrations/reset_timeline_incompleto.py

# Iniciar el dashboard
python -m streamlit run dashboard/app.py
```

Al finalizar, el scraper imprime un resumen JSON con tiempo total, modo, pedidos procesados,
errores, tasa de éxito y — en modo incremental — el desglose por carril (activos, reintentos,
nuevos). Las métricas se miden sobre los resultados del propio run (pedidos persistidos con
COMMIT exitoso), no sobre el estado acumulado en la DB. Código de salida `0` si la tasa de
éxito es ≥ 95 %, `1` si es menor.

---

## Cómo funciona el modo incremental

El modo incremental evita recorrer todo el historial en cada ejecución mediante tres carriles
independientes:

- **Activos:** consulta la DB directamente para obtener pedidos con `scraping_completo = 1`
  que tienen al menos un subpedido en estado no cerrado. No abre ninguna página del servidor.
- **Errores:** lee la tabla `errores` para identificar pedidos que fallaron en ejecuciones
  previas y aún no están completos, y los encola para reintento.
- **Nuevos:** consulta el servidor desde el watermark de última corrida exitosa
  (`meta.ultima_corrida_ok` − 1 día, con tope de 7 días hacia atrás — DEC-012), descarta
  los IDs ya presentes en la DB y encola únicamente los pedidos nuevos. Un outage del
  scheduler de varios días se recupera solo en la primera corrida exitosa posterior.

Los tres conjuntos se combinan con `dict.fromkeys()` para eliminar duplicados y se procesan
en una sola pasada de workers paralelos.

---

## Estado del proyecto

| Etapa | Estado |
|---|---|
| Etapa 1 — Scraper (extracción) | ✅ Completa |
| Etapa 2 — ETL (normalización + VIEWs SQL) | ✅ Completa |
| Etapa 3 — Dashboard (visualización) | ✅ Construida — 17 páginas |
| Módulo de inventario (cruce bodega ↔ sistema + ciclo de conteo) | ✅ Construido |
| Módulo Arena (inventario por ciudad) | ✅ Construido — alcance ampliado bajo checkpoint de autorización (DEC-118) |

El pipeline corre desatendido cada hora en Windows Task Scheduler y la suite
tiene **987 tests** (987 passed + 2 skipped, medido 2026-08-26).

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
