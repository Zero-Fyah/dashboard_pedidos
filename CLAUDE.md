# CLAUDE.md — dashboard_pedidos

Instrucciones de arranque para Claude Code. Este archivo se carga automáticamente
al abrir el proyecto. Para contexto detallado, consultar los archivos en `docs/`.

---

## Qué es este proyecto

Pipeline de datos en tres etapas para una empresa colombiana que gestiona su propia operación logística. 
Su sistema administrativo interno (SPA Vue.js + Element Plus) no expone API; los datos se extraen mediante scraping automatizado y se almacenan en SQLite para análisis y visualización.

| Etapa | Carpeta | Estado |
|---|---|---|
| 1 — Scraper (extracción) | `scraper/` | ✅ Construida |
| 2 — ETL (normalización + VIEWs) | `etl/` | ✅ Construida |
| 3 — Dashboard (visualización) | `dashboard/` | 🔲 Pendiente |

---

## Estructura del repositorio

```text
dashboard_pedidos/
├── .claude/                  # Configuración Claude Code
├── data/                     # Datos locales — gitignored
│   ├── pedidos.db            # Base de datos SQLite
│   ├── debug/                # HTMLs de debug — pueden contener PII
│   └── errors/               # Screenshots de errores del scraper
├── dashboard/                # Etapa 3 — visualización
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
├── scraper/                  # Etapa 1 — extracción de datos
│   ├── __init__.py           # paquete importable por tests/
│   ├── archive/              # Versión inicial del scraper — solo referencia
│   ├── actualizar_pedidos.bat
│   └── scraper_principal.py
├── tests/                    # Suite de tests
│   ├── conftest.py           # Fixtures y opciones de pytest
│   ├── unit/                 # Tests unitarios sin I/O externo
│   ├── integration/          # Tests de integración con SQLite temporal
│   └── e2e/                  # Tests con browser real — lentos
├── .env                      # Credenciales locales — gitignored
├── .env.example              # Plantilla de variables de entorno
├── .gitignore
├── CLAUDE.md                 # Este archivo
├── conftest.py               # sys.path para imports de tests/
├── pytest.ini                    # configuración de pytest y marcadores
├── README.md
└── requirements.txt
```

---

## Cómo arrancar cada sesión de trabajo

Antes de escribir cualquier línea de código o proponer cambios, leer en este orden:

1. **`docs/agent.md`** — define cómo debe comportarse Claude en este proyecto, qué tono usar y qué restricciones respetar.
2. **`docs/integral.md`** — explica el problema de negocio, el objetivo del proyecto y el alcance esperado del dashboard.
3. **`docs/structure.md`** — documenta la arquitectura técnica actual: esquema de las 9 tablas SQLite, los modos de extracción del scraper y las VIEWs planificadas para el ETL.
4. **`docs/decisions.md`** — registra decisiones de diseño ya tomadas y bugsconocidos. No reabrir lo que ya está decidido sin justificación explícita documentada en la misma sesión.
5. **`docs/testing.md`** — solo en sesiones que involucren escritura o modificación de tests.

---

## Entorno de desarrollo

- **SO:** Windows 11
- **Editor:** VS Code
- **Python:** 3.14 — ⚠️ versión alpha, ver nota de riesgo en Stack técnico
- **Entorno virtual:** `.venv/` — activar con `.venv\Scripts\activate`
- **Dependencias:** `pip install -r requirements.txt`
- **Navegador:** Playwright Chromium — `playwright install chromium`
- **Variables de entorno:** copiar `.env.example` → `.env` y completar valores

**Ejecutar el scraper (siempre desde la raíz del proyecto):**
```bash
# Carga histórica desde una fecha
py scraper/scraper_principal.py --desde 2026-05-01

# Modo incremental (activos + errores + nuevos del día)
py scraper/scraper_principal.py --modo incremental
```

---

## Stack técnico

| Componente | Tecnología | Nota |
|---|---|---|
| Lenguaje | Python 3.14 | ⚠️ Alpha — considerar migrar a 3.11/3.12 antes de producción |
| Scraping | Playwright (Chromium) + asyncio | Workers paralelos con circuit breaker |
| Base de datos | SQLite · modo WAL | Confirmado como capa analítica (DEC-010 resuelta 2026-05-23) |
| Async DB | aiosqlite | |
| Logging | JSONL estructurado via `log_event()` | |
| Scheduler | Windows Task Scheduler | Suficiente hoy; requiere alertas de fallo a mediano plazo |

**⚠️ Riesgo Python 3.14:** Es versión alpha. Playwright, aiosqlite y otras dependencias
pueden tener comportamientos no testeados. Documentado en `docs/decisions.md`.
Evaluar migración a Python 3.11 o 3.12 antes de considerar el proyecto en producción.

---

## Comportamiento del scraper — idempotencia

El scraper usa **upsert** (`INSERT OR REPLACE`) por `id_pedido`. Re-ejecutar sobre
el mismo rango de fechas no genera duplicados: sobreescribe los registros existentes
con los datos más recientes. Los modos de extracción son:

- **`completo`** — extrae las 8 secciones del pedido. Se usa en la carga inicial y para pedidos nuevos.
- **`con_cantidades`** — actualiza cantidades entregadas y estado. Para pedidos activos con subpedidos que aún no tienen cantidades definitivas (cantidades_definitivas=0) y cuyo estado está en ESTADOS_FIJAN_CANTIDADES.
- **`solo_estado`** — actualiza únicamente el estado. El más liviano; para pedidos activos sin cambios en cantidades.

Un pedido se considera **cerrado** cuando todos sus subpedidos están en estado
`completado`, `cancelado` o `comentado`. Los pedidos cerrados no se vuelven
a procesar en modo incremental.

---

## Reglas del proyecto

- Las URLs del sistema administrativo y el nombre real de la empresa son
**confidenciales**. Usar siempre `miempresa` como placeholder en documentación, comentarios y ejemplos públicos.
- Nunca hardcodear URLs, credenciales ni rutas absolutas en el código. Todo desde `.env`.
- **Scraper:** logging con `log_event()` en formato
  JSONL. Nunca `print()` sueltos.
- **ETL y otros módulos:** usar `logging` stdlib con
  `logger = logging.getLogger("nombre_modulo")`.
  No importar `log_event()` fuera del scraper para
  evitar acoplamiento entre módulos.
- Rutas siempre relativas a la raíz del proyecto.
- Toda decisión de diseño no trivial debe quedar registrada en `docs/decisions.md` antes de implementarse, no después.
- `scraper/archive/` es solo referencia histórica: no modificar ni agregar archivos.

---

## Secuencia del proyecto

Las etapas son secuenciales por dependencia técnica, no por dogma:

```
Etapa 1 (Scraper) → Etapa 2 (ETL) → Etapa 3 (Dashboard)
```

El ETL es prerequisito del dashboard porque:
- Sin normalización de montos a REAL, no hay sumas ni promedios en SQL.
- Sin VIEWs definidas, el dashboard no tiene contratos de datos estables.

**DEC-010 resuelta:** SQLite confirmado como capa
analítica. No se migrará a DuckDB a este volumen.
Ver `docs/decisions.md` para justificación y umbral
de reevaluación futura.

---

## Restricciones técnicas absolutas

- No modificar el esquema de `pedidos.db` sin actualizar `docs/structure.md`.
- No subir `data/`, `logs/`, `.env` ni archivos `*.db` al repositorio.
- `data/debug/` puede contener datos sensibles (PII de pedidos y clientes). Nunca compartir su contenido. Limpiar manualmente antes de demos o capturas de pantalla. No crear scripts que lean esa carpeta sin advertencia explícita.
- No reabrir decisiones registradas en `docs/decisions.md` sin documentar la justificación en la misma sesión.

---

## Estado actual

**Etapa 1 — Scraper ✅ Completa**
- 9 tablas SQLite con migraciones forward-compatible
- Modos de extracción: `completo`, `con_cantidades`, `solo_estado`
- Lógica de pedido cerrado implementada y funcional
- Para detalles técnicos completos → `docs/structure.md`

**Etapa 2 — ETL ✅ Completa**
- 24 columnas `_num` REAL normalizadas en 4 tablas
- 7 VIEWs analíticas creadas en `data/pedidos.db`
- SQLite confirmado como capa analítica (DEC-010)
- ETL integrado al scheduler — corre cada 2 horas
- Para detalles técnicos → `docs/structure.md`

**Automatización ✅ Activa**
- Windows Task Scheduler configurado — cada 2 horas
- Secuencia: scraper incremental → ETL → log en
  `logs/scraper_scheduler.log`
- Pendiente a mediano plazo: notificación de fallo
  por email o similar

**Etapa 3 — Dashboard 🔲 Pendiente**
- Tecnología por definir. SQLite confirmado como
  capa de datos (DEC-010 resuelta 2026-05-23)
- Requisitos de negocio → `docs/integral.md`