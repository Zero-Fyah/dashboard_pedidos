![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/Licencia-MIT-green)

# dashboard_pedidos

**Actualizado:** 2026-05-24

Scraper asíncrono de pedidos para un sistema administrativo interno (SPA Vue.js + Element Plus)
de una empresa colombiana que gestiona su propia operación logística. Extrae pedidos, subpedidos,
líneas de producto, línea de tiempo de alistamiento y registros operacionales; los almacena en
SQLite en 9 tablas normalizadas y sirve como base de datos para un dashboard de análisis
operacional. Los datos recopilados servirán como insumo para un futuro sistema de predicción
de demanda.

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

Este scraper extrae esa información de forma automatizada, la normaliza en 9 tablas SQLite
y la deja lista para análisis y visualización.

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
   │  3. Nuevos hoy      →  ids_nuevos[]            │
   └────┬───────────────────────────────────────────┘
        │  ids_pendientes[] (unión sin duplicados)
        ▼
  ┌──────────────────────────────────────────────┐
  │   asyncio.Queue (pedidos_queue)              │
  │   sentinel None al final, uno por worker     │
  └──┬──────┬───────┬─────────┬────────┬─────────┘
     │      │       │         │        │
   W-0    W-1     W-2       W-3      W-4    ← 5 workers
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
        etl/etl_principal.py   ← normalización de montos + 7 VIEWs analíticas
```

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
├── CLAUDE.md                 # Guía de arranque para Claude Code
├── conftest.py               # sys.path para imports de tests/
├── pytest.ini                # configuración de pytest y marcadores
├── README.md
└── requirements.txt
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
| `errores` | Pedidos que fallaron el scraping, disponibles para reintento automático |

---

## Instalación

```bash
# 1. Clonar el repositorio
git clone https://github.com/Zero-Fyah/dashboard_pedidos.git
cd dashboard_pedidos

# 2. Crear entorno virtual e instalar dependencias
python -m venv .venv
.venv\Scriptsctivate
pip install -r requirements.txt

# 3. Instalar el navegador que usa Playwright
playwright install chromium

# 4. Configurar credenciales
copy .env.example .env
# Editar .env con usuario, contraseña y URLs reales

# 5. (Opcional) Programar ejecución incremental automática
# Registrar scraper/actualizar_pedidos.bat en Windows Task Scheduler
```

---

## Uso

```bash
# Modo completo — procesa todos los pedidos del rango desde cero
py scraper/scraper_principal.py --desde 2026-05-01

# Modo incremental — actualiza activos, reintenta errores
# y captura pedidos nuevos del día
py scraper/scraper_principal.py --modo incremental
```

Al finalizar, el scraper imprime un resumen JSON con tiempo total, pedidos procesados, errores
y tasa de éxito. Código de salida `0` si la tasa de éxito es ≥ 95 %, `1` si es menor.

---

## Cómo funciona el modo incremental

El modo incremental evita recorrer todo el historial en cada ejecución mediante tres carriles
independientes:

- **Activos:** consulta la DB directamente para obtener pedidos con `scraping_completo = 1`
  que tienen al menos un subpedido en estado no cerrado. No abre ninguna página del servidor.
- **Errores:** lee la tabla `errores` para identificar pedidos que fallaron en ejecuciones
  previas y aún no están completos, y los encola para reintento.
- **Nuevos:** consulta el servidor solo para el rango ayer-hoy, descarta los IDs ya presentes
  en la DB y encola únicamente los pedidos nuevos del día.

Los tres conjuntos se combinan con `dict.fromkeys()` para eliminar duplicados y se procesan
en una sola pasada de workers paralelos.

---

## Estado del proyecto

| Etapa | Estado |
|---|---|
| Etapa 1 — Scraper (extracción) | ✅ Completa |
| Etapa 2 — ETL (normalización + VIEWs SQL) | ✅ Completa |
| Etapa 3 — Dashboard (visualización) | 🔲 Pendiente |

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
