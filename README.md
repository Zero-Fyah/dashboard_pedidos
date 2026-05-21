![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/Licencia-MIT-green)

# dashboard_pedidos

Scraper asíncrono de pedidos para un sistema administrativo interno (SPA Vue.js + Element Plus) de una empresa colombiana de logística 3PL. Extrae pedidos, subpedidos, líneas de producto y línea de tiempo de alistamiento; los almacena en SQLite y sirve como base de datos para un dashboard de inventario comprometido y un sistema de predicción de demanda.

---

## Problema que resuelve

El sistema administrativo de la empresa no expone una API: todos los datos de pedidos viven en una SPA que renderiza tablas paginadas en el cliente. Los equipos de operaciones necesitan visibilidad en tiempo real del estado de los pedidos, niveles de inventario comprometido y ciclos de alistamiento-inspección para tomar decisiones de reposición. Este scraper extrae esa información de forma automatizada, la normaliza en cinco tablas SQLite y la deja lista para análisis y visualización.

---

## Arquitectura técnica

\`\`\`
Windows Task Scheduler
        │
        ▼
actualizar_pedidos.bat
        │
        ▼
scraper_principal.py
        │
   ┌────┴──────────────────────────────────────────┐
   │           Modo incremental (diario)            │
   │  1. Activos en DB   →  ids_activos[]           │
   │  2. Con errores     →  ids_error[]             │
   │  3. Nuevos hoy      →  ids_nuevos[]            │
   └────┬──────────────────────────────────────────┘
        │  ids_pendientes[] (unión sin duplicados)
        ▼
  ┌───────────────────────────────────┐
  │          asyncio.Queue            │
  └──┬──────┬───────┬─────────┬───────┘
     │      │       │         │
   W-0    W-1     W-2       W-3     ← BrowserContext independiente
     │      │       │         │       circuit breaker + re-login
  └──┴──────┴───────┴─────────┘
                │
                ▼
        resultados_queue
                │
                ▼
      persistencia_worker()         ← tarea dedicada, sin Lock contention
                │
                ▼
           pedidos.db (SQLite · modo WAL)
\`\`\`

---

## Esquema de base de datos

| Tabla | Propósito |
|---|---|
| \`pedidos\` | Cabecera del pedido: cliente, vendedor, forma de pago, destino |
| \`subpedidos\` | Subpedidos asociados con estado, alistador e inspector |
| \`lineas_pedido\` | Productos por subpedido: cantidades, precios, almacén, caja |
| \`timeline_pedido\` | Línea de tiempo de pasos por subpedido para análisis de ciclos |
| \`errores\` | Pedidos que fallaron el scraping, disponibles para reintento automático |

---

## Instalación

\`\`\`bash
# 1. Clonar el repositorio
git clone https://github.com/Zero-Fyah/dashboard_pedidos.git
cd dashboard_pedidos

# 2. Crear entorno virtual e instalar dependencias
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. Instalar el navegador que usa Playwright
playwright install chromium

# 4. Configurar credenciales
copy .env.example .env
# Editar .env con usuario y contraseña reales

# 5. (Opcional) Programar ejecución diaria automática
# Registrar actualizar_pedidos.bat en Windows Task Scheduler
\`\`\`

---

## Uso

\`\`\`bash
# Modo completo — recorre todas las páginas del rango desde cero
py scraper_principal.py --desde 2026-01-01 --hasta 2026-05-21

# Modo incremental — actualiza activos, reintenta errores y captura pedidos nuevos del día
py scraper_principal.py --desde 2026-01-01 --hasta 2026-05-21 --modo incremental
\`\`\`

Al finalizar, el scraper imprime un resumen JSON con tiempo total, pedidos procesados, errores y tasa de éxito. Código de salida \`0\` si la tasa de éxito es ≥ 95 %, \`1\` si es menor.

---

## Cómo funciona el modo incremental

El modo incremental evita recorrer todo el historial en cada ejecución mediante tres procesos independientes:

- **Activos**: consulta la DB directamente para obtener los pedidos con \`scraping_completo = 1\` que tienen al menos un subpedido en estado no cerrado. No abre ninguna página del servidor.
- **Errores**: lee la tabla \`errores\` para identificar pedidos que fallaron en ejecuciones previas y aún no están completos y cerrados, y los encola para reintento.
- **Nuevos**: consulta el servidor solo para el rango ayer-hoy, descarta los IDs ya presentes en la DB y encola únicamente los pedidos nuevos del día.
- Los tres conjuntos se combinan con \`dict.fromkeys()\` para eliminar duplicados y se procesan en una sola pasada de workers paralelos.

---

## Nota de privacidad

Las credenciales de acceso **nunca se incluyen en el código**. Se leen desde variables de entorno al iniciar el proceso:

\`\`\`bash
SCRAPER_USUARIO=tu_usuario
SCRAPER_PASSWORD=tu_password
\`\`\`

Copiar \`.env.example\` a \`.env\`, completar los valores reales y verificar que \`.env\` esté en \`.gitignore\` (ya incluido en este repositorio).

---

## Licencia

MIT
