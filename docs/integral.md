# dashboard_pedidos — Visión y objetivo de negocio

Documento de referencia para entender el problema que resuelve este proyecto,
el alcance de los datos y las reglas de negocio que gobiernan el pipeline.

---

## La empresa

Empresa colombiana que gestiona su propia operación logística de forma integral:
almacenamiento, alistamiento, inspección, despacho y transporte. Trabaja con su
propio sistema administrativo interno (SPA Vue.js + Element Plus) como herramienta
central de operación diaria.

El sistema administrativo es funcional para la operación del día a día, pero
**es una caja negra para el análisis**: no expone API, no genera reportes
estructurados y no permite visibilidad histórica del comportamiento operacional.
Los datos de pedidos, subpedidos, productos, tiempos de ciclo y diferencias en
envíos quedan atrapados en tablas paginadas dentro del sistema, inaccesibles
para cualquier análisis externo.

---

## El problema

Los equipos de operaciones de la empresa no tienen visibilidad analítica oportuna
ni estructurada sobre:

- **Estado de pedidos:** cuántos pedidos están activos, en qué etapa van,
  cuáles están bloqueados.
- **Inventario comprometido:** qué productos y cantidades están asociadas a
  pedidos abiertos y aún no despachados.
- **Ciclos de alistamiento e inspección:** cuánto tiempo tarda cada subpedido
  en pasar por cada etapa del proceso.
- **Diferencias en envíos:** qué tan frecuentes son, en qué montos y en qué
  productos se concentran.
- **Rendimiento por operador:** tiempos y volúmenes por alistador e inspector.

La consecuencia directa es que las decisiones de reposición, asignación de
recursos y detección de cuellos de botella se toman con información incompleta
o desactualizada.

---

## El objetivo del proyecto

Construir una capa de inteligencia por encima del sistema administrativo que
libere los datos operacionales y los ponga disponibles para análisis y
visualización, sin intervenir ni modificar el sistema existente.

El pipeline se estructura en tres etapas:

| Etapa | Qué hace |
|---|---|
| **1 — Scraper** | Extrae los datos del sistema administrativo y los almacena en SQLite |
| **2 — ETL** | Normaliza los datos y crea VIEWs analíticas sobre la base de datos |
| **3 — Dashboard** | Visualiza los datos para el equipo de operaciones |

---

## Alcance temporal

El proyecto recopila pedidos creados **desde el 2026-05-01 en adelante**.

Esta fecha es el corte de negocio del proyecto, no un parámetro técnico
arbitrario. Los pedidos anteriores a esa fecha no forman parte del alcance
y no deben procesarse.

---

## Qué se extrae de cada pedido

De cada pedido se extraen **hasta 8 secciones de información**:

| # | Sección | Contenido | Alimenta en el dashboard |
|---|---|---|---|
| 1 | **Timeline** | Línea de tiempo de pasos del pedido: fechas y estados por etapa | Vista de ciclos operacionales |
| 2 | **Información básica** | ID, fecha de creación, cliente, vendedor, forma de pago, destino | Filtros globales y vista operacional |
| 3 | **Información de entrega** | Destinatario, dirección, ciudad, modalidad de entrega | Segmentación geográfica y por modalidad |
| 4 | **Información de productos** | Subpedidos y mercancía: SKU, descripción, código de barras, cantidades compradas y entregadas, precios, almacén, caja | Vista de inventario comprometido |
| 5 | **Estadísticas de monto** | Monto a pagar, descuentos, diferencias detectadas, monto final | KPIs financieros en vista operacional e histórica |
| 6 | **Gestión de diferencias** | Resumen de la diferencia entre lo pedido y lo despachado | Vista de diferencias en envíos |
| 7 | **Detalle de diferencias** | Desglose por producto: cantidades, motivos, valores | Vista de diferencias en envíos |
| 8 | **Registro de operaciones** | Log de acciones: quién hizo qué y cuándo | Trazabilidad y rendimiento por operador |

> **Secciones condicionales:** las secciones 6 y 7 solo se extraen cuando el
> pedido tiene al menos una diferencia registrada entre lo pedido y lo despachado.
> Si no hay diferencias, se extraen únicamente las secciones 1 a 5 y 8.

---

## Reglas de negocio del pipeline

### Carga inicial

En la primera ejecución, el scraper recorre **todos los pedidos desde 2026-05-01
hasta la fecha actual** y extrae las secciones aplicables para cada uno
(hasta 8, según si hay diferencias). Es una operación de única vez; el tiempo
de ejecución depende del volumen acumulado.

### Modo incremental (operación continua)

Una vez completada la carga inicial, el pipeline opera en tres carriles
en cada ejecución:

**1. Pedidos nuevos**
Pedidos del rango reciente (ayer-hoy en el sistema administrativo) que aún no
están en la base de datos local. Se extrae la información completa aplicable
(hasta 8 secciones según si hay diferencias).

**2. Pedidos activos (abiertos)**
Pedidos que ya están en la base de datos y tienen al menos un subpedido en
fase activa. Solo se actualiza el estado de sus subpedidos. Cuando un subpedido
alcanza un estado de cierre en esa actualización, se registran también sus
cantidades entregadas finales (ver ciclo de vida del subpedido más abajo).

**3. Pedidos con error**
Pedidos que fallaron en ejecuciones anteriores y aún no están procesados
correctamente. Se reintenta su extracción completa.

> **Limitación y procedimiento de recuperación:** el modo incremental busca
> pedidos nuevos únicamente en el rango ayer-hoy. Si el scheduler falla varios
> días consecutivos, los pedidos de esos días no se capturan automáticamente
> en el siguiente ciclo. **Acción correctiva:** ejecutar manualmente el scraper
> con el rango de fechas perdidas:
> ```
> py scraper/scraper_principal.py --desde YYYY-MM-DD --hasta YYYY-MM-DD
> ```

### Ciclo de vida de un subpedido

Cada subpedido pasa por dos fases claramente diferenciadas:

**Fase activa**
El subpedido está en un estado intermedio del proceso operacional. En cada
ciclo incremental solo se actualiza su estado. Las cantidades entregadas
**no se modifican** en esta fase. Los estados intermedios posibles están
definidos en `docs/structure.md`.

**Cierre**
Cuando el subpedido alcanza uno de los siguientes estados:

- `cancelado`
- `completado`
- `comentado`

Se realiza una **última actualización** que registra el estado final y las
cantidades entregadas definitivas. Después de esta actualización, el subpedido
pasa al **histórico** y es **inmutable**: no recibe ningún procesamiento
adicional. Sus datos alimentan el dashboard en las vistas de ciclos
operacionales, inventario y diferencias en envíos.

### Definición de pedido cerrado

Un pedido se considera **cerrado** cuando **todos** sus subpedidos han
completado su ciclo de vida (están en estado `cancelado`, `completado`
o `comentado`).

Si un pedido tiene múltiples subpedidos, basta con que **uno** esté en
fase activa para que el pedido completo siga siendo considerado abierto.

Los pedidos cerrados **no se vuelven a procesar** en el modo incremental.
Todo su contenido es histórico e inmutable.

Para la implementación técnica de estas reglas → `docs/structure.md`

---

## Qué no es este proyecto

Delimitar el alcance evita trabajo innecesario y expectativas incorrectas:

- No es una integración con el sistema administrativo: no escribe en él,
  no lo modifica, no interactúa con su base de datos directamente.
- No reemplaza el sistema administrativo: es una capa de lectura y análisis.
- No cubre pedidos anteriores al 2026-05-01.
- No garantiza completitud absoluta de datos: si el scraper falla en un
  pedido individual, ese pedido queda en la tabla de errores para reintento,
  pero no hay detección automática de pedidos silenciosamente omitidos.
- No detecta automáticamente gaps de días completos si el scheduler falla
  varios días seguidos (ver procedimiento de recuperación en la sección anterior).
- No es un sistema de alertas operativas en tiempo real; la actualización
  es cada 2 horas (evolución futura posible, fuera del alcance actual).

---

## Objetivo del dashboard

El dashboard es el destino final de los datos. Debe darle al equipo de
operaciones la visibilidad analítica que el sistema administrativo no provee.

### Usuarios objetivo

Equipo interno de operaciones: gerencia, supervisores de almacén y coordinadores
de despacho. El dashboard es una sola herramienta compartida por todos los roles,
con filtros que permiten a cada usuario acotar la vista a su área de interés.

### Vistas y métricas esperadas

**Vista operacional** — actualización cada 2 horas
Fuente: secciones 2, 4 y 5
- Pedidos activos: cuántos hay, en qué estado, cuánto tiempo llevan abiertos
- Subpedidos por estado: distribución actual
- Pedidos con diferencias detectadas: listado y monto involucrado
- Filtros por vendedor, forma de pago y destino

**Vista de inventario comprometido**
Fuente: sección 4
- Productos con unidades comprometidas en pedidos abiertos (cantidad comprada
  menos cantidad entregada hasta el momento)
- Comparación entre cantidad total comprada y cantidad entregada por SKU

**Vista de ciclos operacionales**
Fuente: secciones 1 y 8
- Tiempo promedio por etapa del timeline (alistamiento, inspección, despacho)
- Rendimiento por alistador e inspector: volumen y tiempo procesado

**Vista de diferencias en envíos**
Fuente: secciones 5, 6 y 7
- Tasa de diferencias sobre el total de pedidos
- Productos con mayor frecuencia de diferencias
- Monto total involucrado en diferencias por período

**Vista histórica**
Fuente: secciones 2 y 5
- Evolución del volumen de pedidos desde 2026-05-01
- Tendencias de diferencias y cancelaciones por semana y mes

### Frecuencia de actualización

Los datos se refrescan **cada 2 horas** mediante el modo incremental automatizado.
No se requiere tiempo real estricto.