"""
hallazgos.py — Detectores de inconsistencias del sistema administrativo (DEC-047).

Cada detector mide una inconsistencia concreta sobre los datos **actuales**
y devuelve el listado de casos, no solo el conteo: eso es lo que permite
que el módulo de Tareas muestre "cuáles" y no apenas "cuántos".

Corren dentro del ciclo del scheduler, justo después de descargar el Excel
del sistema administrativo, así que cada corrida refleja el estado más
reciente. Cuando una inconsistencia se corrige en el origen, su detector
devuelve cero casos y la tarea desaparece sola.

Los detectores son funciones puras sobre DataFrames y una conexión de
lectura: no escriben nada. La persistencia vive en `persistencia.py`.
"""

import itertools
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

import pandas as pd

from comun import (
    ACCIONES_CONOCIDAS,
    CONCEPTOS_MONTO_CONOCIDOS,
    ESTADOS_CERRADOS,
    ESTADOS_CONOCIDOS,
    TIMELINE_CONOCIDO,
    VEREDICTOS_AUDITORIA,
)

logger = logging.getLogger("inventario.hallazgos")


class Hallazgo:
    """Una inconsistencia detectada, con su listado de casos.

    Attributes:
        clave: Identificador estable del detector. Es la llave de
            persistencia, así que **no debe cambiar** una vez publicado.
        titulo: Nombre corto para la tabla de tareas.
        explicacion: Por qué importa y qué hay que hacer.
        categoria/prioridad/origen: Metadatos de seguimiento.
        unidad: Qué cuenta `cantidad` ("códigos", "líneas"…).
        cantidad: Cuántos casos hay. **Se declara explícito y no se deriva
            de `len(filas)`**: el detalle puede tener varias filas por caso
            (un código de barras ambiguo lista todos sus ID), y confundir
            ambas cifras infla el número que el Arquitecto usa para
            priorizar.
        filas: Detalle, con el contexto de cada caso.
    """

    def __init__(
        self,
        clave: str,
        titulo: str,
        explicacion: str,
        categoria: str,
        prioridad: str,
        origen: str,
        unidad: str,
        cantidad: int,
        filas: pd.DataFrame,
    ) -> None:
        self.clave = clave
        self.titulo = titulo
        self.explicacion = explicacion
        self.categoria = categoria
        self.prioridad = prioridad
        self.origen = origen
        self.unidad = unidad
        self.cantidad = cantidad
        self.filas = filas


def _tabla_existe(con: sqlite3.Connection, nombre: str) -> bool:
    """True si la tabla ya está creada.

    Hace falta porque los detectores corren **antes** de que la corrida
    persista sus tablas: en una base recién creada, `inventario_ubicaciones`
    todavía no existe y un SELECT la haría fallar.
    """
    fila = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nombre,)
    ).fetchone()
    return fila is not None


def _norm_especificacion(valor: object) -> str:
    """Normaliza un texto de especificación/presentación para compararlo.

    Quita la etiqueta inicial ("Presentación:", "Sabores:", "规格:"),
    colapsa separadores y baja a minúsculas. Deja fuera solo las
    diferencias de formato: lo que quede distinto es contenido distinto.
    """
    texto = str(valor or "").strip()
    texto = re.sub(r"^[^:]*:\s*", "", texto)
    texto = re.sub(r"[;,.\s]+", " ", texto).strip()
    return texto.casefold()


def codigos_barras_multiples_ids(df_admin: pd.DataFrame) -> Hallazgo:
    """Códigos de barras que identifican más de un producto en el catálogo.

    Es la inconsistencia que impide usar el código de barras como llave de
    producto (DEC-045): un mismo código puede colgar de decenas de
    referencias cuando el artículo se vende bajo varias modalidades.
    """
    df = df_admin.copy()
    df["codigo_barras"] = df["codigo_barras"].astype(str).str.strip()
    por_codigo = df.groupby("codigo_barras")["id_especificacion"].nunique()
    afectados = por_codigo[por_codigo > 1].index

    filas = (
        df[df["codigo_barras"].isin(afectados)][
            ["codigo_barras", "id_especificacion", "referencia", "nombre_comercial"]
        ]
        .drop_duplicates()
        .sort_values(["codigo_barras", "referencia"])
        .rename(
            columns={
                "codigo_barras": "Código de barras",
                "id_especificacion": "ID de especificación",
                "referencia": "Referencia",
                "nombre_comercial": "Nombre comercial",
            }
        )
    )
    return Hallazgo(
        clave="codigos_barras_multiples_ids",
        titulo="Códigos de barras asociados a múltiples ID",
        explicacion=(
            "Un mismo código de barras identifica más de un producto en el catálogo. "
            "Mientras exista, el código no sirve como llave para relacionar el "
            "producto con los pedidos, y hay que apoyarse en la referencia."
        ),
        categoria="Códigos y referencias",
        prioridad="Alta",
        origen="DEC-045",
        unidad="códigos",
        cantidad=len(afectados),  # códigos afectados, no filas de detalle
        filas=filas,
    )


def referencias_con_espacios(df_admin: pd.DataFrame) -> Hallazgo:
    """Referencias con espacios sobrantes al inicio o al final.

    Rompen cualquier agrupación por referencia: `"PJ91 AVERIA "` y
    `"PJ91 AVERIA"` son dos claves distintas para el mismo producto.
    """
    df = df_admin[["referencia", "nombre_comercial"]].copy()
    df["referencia"] = df["referencia"].astype(str)
    sobrantes = df[df["referencia"] != df["referencia"].str.strip()]

    filas = (
        sobrantes.drop_duplicates("referencia")
        .assign(
            **{
                "Referencia (con espacios)": lambda d: d["referencia"].map(repr),
                "Debería ser": lambda d: d["referencia"].str.strip(),
            }
        )[["Referencia (con espacios)", "Debería ser", "nombre_comercial"]]
        .rename(columns={"nombre_comercial": "Nombre comercial"})
        .sort_values("Debería ser")
    )
    return Hallazgo(
        clave="referencias_con_espacios",
        titulo="Referencias con espacios sobrantes",
        explicacion=(
            "La referencia tiene espacios al inicio o al final. Agrupar por "
            "referencia parte el mismo producto en dos filas distintas."
        ),
        categoria="Códigos y referencias",
        prioridad="Media",
        origen="DEC-041",
        unidad="referencias",
        cantidad=len(filas),
        filas=filas,
    )


def especificacion_discrepante(df_admin: pd.DataFrame, con: sqlite3.Connection) -> Hallazgo:
    """Productos cuya especificación se escribe distinto en cada sistema.

    Solo cuenta pares que **existen en ambos** sistemas: si el texto
    difiere ahí, es una discrepancia real y no una ausencia. Es lo que hoy
    impide usar la especificación como llave de producto, que sería la más
    precisa (DEC-045).
    """
    admin = df_admin.copy()
    admin["ref"] = admin["referencia"].astype(str).str.strip()
    admin["cb"] = admin["codigo_barras"].astype(str).str.strip()
    admin["k"] = admin["especificacion"].map(_norm_especificacion)
    por_par = admin.groupby(["ref", "cb"]).agg(
        variantes=("k", set), especificacion_catalogo=("especificacion", "first")
    )

    pedidos = pd.read_sql(
        """SELECT DISTINCT TRIM(referencia) AS ref, TRIM(codigo_barras) AS cb, presentacion
           FROM lineas_pedido
           WHERE referencia IS NOT NULL AND referencia != ''""",
        con,
    )
    pedidos["k"] = pedidos["presentacion"].map(_norm_especificacion)

    cruce = pedidos.merge(por_par.reset_index(), on=["ref", "cb"], how="inner")
    discrepan = cruce[[k not in v for k, v in zip(cruce["k"], cruce["variantes"], strict=True)]]

    columnas = {
        "ref": "Referencia",
        "cb": "Código de barras",
        "presentacion": "Como figura en pedidos",
        "especificacion_catalogo": "Como figura en el catálogo",
    }
    # Sin coincidencias, el merge devuelve un DataFrame sin columnas y
    # seleccionarlas explota. Justamente el caso "ya está todo resuelto",
    # que es el que el módulo necesita reportar como cero, no como error.
    filas = (
        discrepan[list(columnas)].rename(columns=columnas)
        if len(discrepan)
        else pd.DataFrame(columns=list(columnas.values()))
    )
    return Hallazgo(
        clave="especificacion_discrepante",
        titulo="Especificación escrita distinto entre sistemas",
        explicacion=(
            "El mismo producto tiene una especificación distinta en los pedidos y "
            "en el catálogo (mayúsculas, comas, o datos extra como la fecha de "
            "vencimiento). Unificarla habilitaría la llave más precisa para "
            "relacionar producto y pedido."
        ),
        categoria="Nombres y descripciones",
        prioridad="Alta",
        origen="DEC-045",
        unidad="productos",
        cantidad=len(filas[["Referencia", "Código de barras"]].drop_duplicates()),
        filas=filas,
    )


def estados_sin_clasificar(con: sqlite3.Connection) -> Hallazgo:
    """Estados de subpedido que no están en ninguna lista de `comun/`.

    Un estado desconocido queda fuera de las reglas de negocio: no cuenta
    como activo ni como cerrado, así que distorsiona el inventario
    comprometido y los indicadores de antigüedad.
    """
    est = pd.read_sql(
        """SELECT estado, COUNT(*) AS n,
                  COUNT(DISTINCT id_pedido) AS pedidos
           FROM subpedidos
           WHERE estado IS NOT NULL AND estado != ''
           GROUP BY estado""",
        con,
    )
    desconocidos = est[~est["estado"].str.lower().isin(ESTADOS_CONOCIDOS)]

    filas = desconocidos.rename(
        columns={"estado": "Estado", "n": "Subpedidos", "pedidos": "Pedidos afectados"}
    ).sort_values("Subpedidos", ascending=False)
    return Hallazgo(
        clave="estados_sin_clasificar",
        titulo="Estados de subpedido sin clasificar",
        explicacion=(
            "El estado no está en ninguna lista de `comun/`, así que no cuenta ni "
            "como activo ni como cerrado. Distorsiona el inventario comprometido y "
            "los días abiertos. Requiere decidir a qué grupo pertenece."
        ),
        categoria="Estados",
        prioridad="Alta",
        origen="DEC-040",
        unidad="estados",
        cantidad=len(filas),
        filas=filas,
    )


def lineas_sin_id_producto(con: sqlite3.Connection) -> Hallazgo:
    """Combinaciones de pedido que no logran resolver un ID de producto.

    O el par referencia/código de barras no está en el catálogo vigente
    (código legado), o apunta a más de un ID y se excluyó a propósito para
    no duplicar líneas (DEC-045).
    """
    filas = pd.read_sql(
        """SELECT TRIM(l.referencia)    AS "Referencia",
                  TRIM(l.codigo_barras) AS "Código de barras",
                  l.nombre_producto     AS "Nombre en el pedido",
                  COUNT(*)              AS "Líneas afectadas"
           FROM lineas_pedido l
           LEFT JOIN catalogo_productos c
                  ON c.referencia = TRIM(l.referencia)
                 AND c.codigo_barras = TRIM(l.codigo_barras)
           WHERE c.id_producto IS NULL
             AND l.referencia IS NOT NULL AND l.referencia != ''
           GROUP BY 1, 2, 3
           ORDER BY 4 DESC""",
        con,
    )
    return Hallazgo(
        clave="lineas_sin_id_producto",
        titulo="Líneas de pedido sin ID de producto",
        explicacion=(
            "El par referencia/código de barras no resuelve a un ID: o no está en "
            "el catálogo vigente, o apunta a más de un producto. Estas líneas "
            "aparecen con el ID vacío en el consolidado de pedidos."
        ),
        categoria="Códigos y referencias",
        prioridad="Media",
        origen="DEC-045",
        unidad="combinaciones",
        cantidad=len(filas),
        filas=filas,
    )


# ─────────────────────────────────────────────
# Identidades de personal duplicadas (DEC-056)
# ─────────────────────────────────────────────

# Umbral de similitud entre nombres normalizados. Medido sobre los 79
# nombres reales: a 0,85 la regla separa limpiamente los duplicados
# (la más baja aceptada es 0,86) del resto (la más alta rechazada, 0,84).
_SIMILITUD_MINIMA = 0.85

# Un nombre de persona tiene al menos nombre y apellido. Exigir dos tokens
# descarta las cuentas genéricas de un solo token, que es donde la
# similitud se equivoca: `temporal5` y `temporal6` dan 0,89 y son cuentas
# distintas, no una errata.
_TOKENS_MINIMOS = 2


def _norm_persona(nombre: str) -> str:
    """Normaliza un nombre de persona para compararlo: sin tildes, mayúsculas."""
    texto = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return " ".join(texto.upper().split())


def _es_prefijo(tokens_a: list[str], tokens_b: list[str]) -> bool:
    """True si un nombre es el comienzo literal del otro (nombre truncado)."""
    corto, largo = sorted((tokens_a, tokens_b), key=len)
    return len(corto) >= _TOKENS_MINIMOS and largo[: len(corto)] == corto


def personal_duplicado(con: sqlite3.Connection) -> Hallazgo:
    """Personas que figuran con más de una grafía en el maestro de personal.

    Si la misma persona aparece escrita de dos formas, su carga de trabajo
    queda partida en dos identidades y el conteo de alistadores por día
    sale inflado — justo los indicadores de DEC-055.

    Dos nombres se agrupan cuando su similitud supera `_SIMILITUD_MINIMA`
    **o** cuando uno es el comienzo literal del otro (nombre truncado, que
    puede dar similitud baja: `PASTOR YESID` contra `PASTOR YESID
    RODRIGUEZ NIETO` da 0,60 y es obviamente la misma persona).

    Las variantes se unen en **grupos**, no en pares: si una persona
    aparece con tres grafías, es un caso, no tres.
    """
    apariciones: dict[str, int] = {}

    def sumar(nombre: object, n: int) -> None:
        texto = str(nombre or "").strip()
        if texto and texto != "-":
            apariciones[texto] = apariciones.get(texto, 0) + n

    # El alistador es multivaluado: un subpedido puede listar varias
    # personas separadas por coma (DEC-055: el picking se reparte por familia).
    for columna in ("alistador", "inspector"):
        for valor, n in con.execute(
            f"SELECT {columna}, COUNT(*) FROM subpedidos "  # noqa: S608 — nombre de columna literal
            f"WHERE {columna} IS NOT NULL GROUP BY 1"
        ):
            for parte in str(valor).split(","):
                sumar(parte, n)
    for valor, n in con.execute(
        "SELECT usuario, COUNT(*) FROM registro_operaciones "
        "WHERE tipo_usuario = 'staff' AND usuario IS NOT NULL GROUP BY 1"
    ):
        sumar(valor, n)

    candidatos = [
        (nombre, _norm_persona(nombre).split())
        for nombre in apariciones
        if len(_norm_persona(nombre).split()) >= _TOKENS_MINIMOS
    ]

    # Union-find sobre los nombres: agrupa transitivamente las variantes.
    padre = {nombre: nombre for nombre, _ in candidatos}

    def raiz(nombre: str) -> str:
        while padre[nombre] != nombre:
            padre[nombre] = padre[padre[nombre]]
            nombre = padre[nombre]
        return nombre

    similitudes: dict[str, float] = {}
    for (n_a, t_a), (n_b, t_b) in itertools.combinations(candidatos, 2):
        similitud = SequenceMatcher(None, " ".join(t_a), " ".join(t_b)).ratio()
        if similitud >= _SIMILITUD_MINIMA or _es_prefijo(t_a, t_b):
            padre[raiz(n_a)] = raiz(n_b)
            for nombre in (n_a, n_b):
                similitudes[nombre] = max(similitudes.get(nombre, 0.0), similitud)

    grupos: dict[str, list[str]] = {}
    for nombre in similitudes:
        grupos.setdefault(raiz(nombre), []).append(nombre)

    registros = [
        {
            "Grupo": indice,
            "Nombre como figura": nombre,
            "Apariciones": apariciones[nombre],
            "Similitud": round(similitudes[nombre], 2),
        }
        for indice, variantes in enumerate(
            sorted(grupos.values(), key=lambda v: -sum(apariciones[n] for n in v)), start=1
        )
        for nombre in sorted(variantes, key=lambda n: -apariciones[n])
    ]
    filas = pd.DataFrame(
        registros, columns=["Grupo", "Nombre como figura", "Apariciones", "Similitud"]
    )
    return Hallazgo(
        clave="personal_duplicado",
        titulo="Personas con más de una grafía en el maestro",
        explicacion=(
            "La misma persona figura escrita de varias formas (una letra de "
            "diferencia, una tilde, un apellido de más o de menos). Su carga de "
            "trabajo queda partida entre dos identidades y el conteo de "
            "alistadores por día sale inflado. Hay que unificar la grafía en el "
            "maestro de personal del sistema administrativo."
        ),
        categoria="Personal",
        prioridad="Media",
        origen="DEC-056",
        unidad="personas",
        cantidad=len(grupos),  # identidades reales, no variantes ni pares
        filas=filas,
    )


def productos_en_bodega_sin_precio(df_admin: pd.DataFrame, con: sqlite3.Connection) -> Hallazgo:
    """Producto ocupando una posición que el catálogo no sabe cuánto vale.

    No distorsiona la cola de conteo —esas líneas son casi todas de clase
    "Sin rotación", que ya va última por clase, medido: 1.568 de 1.576— pero
    sí deja un hueco real: hay decenas de miles de unidades en bodega sobre
    las que no se puede decir si conviene contarlas primero, ni cuánto
    capital representan.

    Solo cuenta lo que **está en una posición**: un ID sin precio y sin
    existencias es una ficha incompleta de algo que no está, y no compite
    por la atención de nadie.

    Cuenta **productos**, no filas de catálogo (DEC-064): el admin trae una
    fila por `(producto, almacén)` y son 12 almacenes, así que hay que
    colapsar por ID antes de contar. Sin eso, un producto de presencia
    nacional aportaba 12 al total y una sola alta movía la cifra en 12 —
    así fue como el detector pasó de reportar 9 a 49 sin que la realidad
    aumentara.
    """
    columnas = {
        "id_especificacion": "ID",
        "referencia": "Referencia",
        "nombre_comercial": "Nombre comercial",
        "posiciones": "Posiciones",
        "unidades": "Unidades",
    }
    if not _tabla_existe(con, "inventario_ubicaciones"):
        return Hallazgo(
            clave="productos_en_bodega_sin_precio",
            titulo="Producto en bodega sin precio en el catálogo",
            explicacion="",
            categoria="Montos y placeholders",
            prioridad="Media",
            origen="DEC-061",
            unidad="productos",
            cantidad=0,
            filas=pd.DataFrame(columns=list(columnas.values())),
        )

    catalogo = df_admin[["id_especificacion", "referencia", "nombre_comercial", "precio"]].copy()
    catalogo["id_especificacion"] = catalogo["id_especificacion"].astype(str)
    catalogo["precio"] = pd.to_numeric(catalogo["precio"], errors="coerce").fillna(0)

    # Una fila por producto, con el precio máximo entre sus almacenes: un
    # producto está sin precio solo si NINGUNA de sus filas tiene precio.
    # Hoy no hay IDs contradictorios (medido: 0), así que el max no cambia
    # ninguna clasificación — blinda el detector si mañana aparecen.
    por_producto = catalogo.groupby("id_especificacion", as_index=False).agg(
        referencia=("referencia", "first"),
        nombre_comercial=("nombre_comercial", "first"),
        precio=("precio", "max"),
    )
    sin_precio = por_producto[por_producto["precio"] <= 0]

    en_bodega = pd.read_sql(
        """SELECT id_especificacion,
                  COUNT(DISTINCT ubicacion) AS posiciones,
                  SUM(cantidad)             AS unidades
           FROM inventario_ubicaciones
           GROUP BY id_especificacion""",
        con,
    )
    en_bodega["id_especificacion"] = en_bodega["id_especificacion"].astype(str)

    afectados = sin_precio.merge(en_bodega, on="id_especificacion", how="inner")
    filas = (
        afectados[list(columnas)].rename(columns=columnas).sort_values("Unidades", ascending=False)
    )
    return Hallazgo(
        clave="productos_en_bodega_sin_precio",
        titulo="Producto en bodega sin precio en el catálogo",
        explicacion=(
            "Hay existencias de estos productos en posiciones del layout, pero el "
            "catálogo no les asigna precio. No se pueden valorar, así que no se sabe "
            "cuánto capital representan ni si conviene priorizarlos en el conteo. "
            "Requiere asignarles precio o confirmarlos como descontinuados."
        ),
        categoria="Montos y placeholders",
        prioridad="Media",
        origen="DEC-061",
        unidad="productos",
        cantidad=len(filas),
        filas=filas,
    )


def sku_en_bodega_fuera_de_catalogo(df_admin: pd.DataFrame, con: sqlite3.Connection) -> Hallazgo:
    """Producto en una posición cuyo ID el catálogo del admin no reconoce.

    Es la contracara del alcance que fijó DEC-072: el trabajo se concentra en
    los ID que publica `admin_inventario`, y lo que queda afuera **tiene que
    quedar contado en algún lado** o la exclusión se vuelve invisible.

    No se puede contar ni clasificar: sin ficha en el catálogo no hay
    referencia, ni clase ABC, ni precio. Pero ocupa posiciones reales del
    layout, así que tampoco es un error de datos que se pueda ignorar —
    alguien tiene que decidir si es otra línea de negocio que comparte
    bodega o producto que el admin dejó de reconocer.
    """
    columnas = {
        "id_especificacion": "ID",
        "referencia": "Referencia",
        "posiciones": "Posiciones",
        "unidades": "Unidades",
    }
    vacio = Hallazgo(
        clave="sku_en_bodega_fuera_de_catalogo",
        titulo="Producto en bodega con ID fuera del catálogo",
        explicacion="",
        categoria="Identidad del producto",
        prioridad="Alta",
        origen="DEC-072",
        unidad="ID",
        cantidad=0,
        filas=pd.DataFrame(columns=list(columnas.values())),
    )
    if not _tabla_existe(con, "inventario_ubicaciones"):
        return vacio

    en_bodega = pd.read_sql(
        """SELECT id_especificacion,
                  MAX(referencia)            AS referencia,
                  COUNT(DISTINCT ubicacion)  AS posiciones,
                  SUM(cantidad)              AS unidades
           FROM inventario_ubicaciones
           GROUP BY id_especificacion""",
        con,
    )
    if en_bodega.empty:
        return vacio
    en_bodega["id_especificacion"] = en_bodega["id_especificacion"].astype(str)
    conocidos = set(df_admin["id_especificacion"].astype(str))
    fuera = en_bodega[~en_bodega["id_especificacion"].isin(conocidos)]

    filas = fuera[list(columnas)].rename(columns=columnas).sort_values("Unidades", ascending=False)
    return Hallazgo(
        clave="sku_en_bodega_fuera_de_catalogo",
        titulo="Producto en bodega con ID fuera del catálogo",
        explicacion=(
            "Hay existencias en posiciones del layout bajo ID que el catálogo del "
            "sistema administrativo no reconoce. No se pueden identificar, clasificar "
            "ni valorar, así que quedan fuera del alcance de conteo (DEC-072) — pero "
            "ocupan espacio real. Requiere decidir si son otra línea de negocio que "
            "comparte bodega o producto que el admin dejó de publicar."
        ),
        categoria="Identidad del producto",
        prioridad="Alta",
        origen="DEC-072",
        unidad="ID",
        cantidad=len(filas),
        filas=filas,
    )


def vocabulario_nuevo_en_origen(con: sqlite3.Connection) -> Hallazgo:
    """Valores que la SPA empezó a emitir y el pipeline no conoce (DEC-081).

    **La vista del pedido cambia sola.** Medido sobre los 7 meses de
    histórico: entre enero y julio de 2026 aparecieron 18 valores nuevos en
    cuatro vocabularios, y ninguno lo detectó el pipeline. Se encontraron de
    casualidad, revisando el DOM a mano.

    Un valor nuevo puede ser inofensivo —una etiqueta renombrada— o significar
    que hay una sección nueva que nadie está extrayendo, que fue justo lo que
    pasó con la tarjeta "Operación de pago". Este detector no distingue entre
    las dos cosas: avisa, y alguien mira.

    Cubre tres vocabularios; el cuarto —los estados de subpedido— ya tiene su
    propio detector (`estados_sin_clasificar`), que además alimenta las listas
    de `comun/`.

    **Trae la fecha del primer avistamiento**, que es lo que convierte el
    aviso en algo accionable: un valor que apareció ayer es un cambio
    reciente; uno de hace cinco meses es una deuda que venía pasando
    inadvertida.

    Reconocer un valor es agregarlo a la lista de `comun/` — ahí el hallazgo
    desaparece solo, misma mecánica que el resto (DEC-047).
    """
    columnas = {
        "vocabulario": "Dónde",
        "valor": "Valor nuevo",
        "primera_vez": "Visto por primera vez",
        "veces": "Veces",
    }
    fuentes = (
        (
            "Paso del timeline",
            TIMELINE_CONOCIDO,
            """SELECT titulo AS valor, COUNT(*) AS veces, MIN(fecha_hora) AS primera_vez
               FROM timeline_pedido
               WHERE titulo IS NOT NULL AND titulo != ''
                 AND fecha_hora IS NOT NULL AND fecha_hora != ''
               GROUP BY titulo""",
        ),
        (
            "Acción del registro",
            ACCIONES_CONOCIDAS,
            """SELECT accion AS valor, COUNT(*) AS veces, MIN(momento) AS primera_vez
               FROM registro_operaciones
               WHERE accion IS NOT NULL AND accion != ''
                 AND momento IS NOT NULL AND momento != ''
               GROUP BY accion""",
        ),
        (
            "Concepto de monto",
            CONCEPTOS_MONTO_CONOCIDOS,
            """SELECT em.concepto_base AS valor, COUNT(*) AS veces, MIN(p.fecha) AS primera_vez
               FROM estadisticas_monto em
               JOIN pedidos p ON p.id_pedido = em.id_pedido
               WHERE em.concepto_base IS NOT NULL AND em.concepto_base != ''
                 AND p.fecha IS NOT NULL AND p.fecha != ''
               GROUP BY em.concepto_base""",
        ),
        # DEC-083: el veredicto de la auditoría vivía en `referencia` desde el
        # 2026-04-10 y este detector no lo veía porque solo miraba `accion`.
        # `CAST(... AS INTEGER) = 0` descarta los ID numéricos de comprobante,
        # que comparten la columna con el veredicto.
        (
            "Veredicto de auditoría",
            VEREDICTOS_AUDITORIA,
            """SELECT referencia AS valor, COUNT(*) AS veces, MIN(momento) AS primera_vez
               FROM registro_operaciones
               WHERE accion = 'Auditoría de pago'
                 AND referencia IS NOT NULL AND referencia != ''
                 AND CAST(referencia AS INTEGER) = 0
                 AND momento IS NOT NULL AND momento != ''
               GROUP BY referencia""",
        ),
    )

    filas: list[dict[str, object]] = []
    for etiqueta, conocidos, sql in fuentes:
        tabla = sql.split("FROM")[1].split()[0]
        if not _tabla_existe(con, tabla):
            continue
        df = pd.read_sql(sql, con)
        nuevos = df[~df["valor"].isin(conocidos)]
        for r in nuevos.itertuples(index=False):
            filas.append(
                {
                    "vocabulario": etiqueta,
                    "valor": r.valor,
                    "primera_vez": str(r.primera_vez)[:10],
                    "veces": int(r.veces),
                }
            )

    detalle = pd.DataFrame(filas, columns=list(columnas))
    if not detalle.empty:
        detalle = detalle.sort_values("primera_vez", ascending=False)
    detalle = detalle.rename(columns=columnas)

    return Hallazgo(
        clave="vocabulario_nuevo_en_origen",
        titulo="El sistema origen empezó a emitir valores desconocidos",
        explicacion=(
            "La vista del pedido en el sistema administrativo cambia con el tiempo: "
            "solo entre enero y julio de 2026 aparecieron 18 valores nuevos sin que "
            "nadie lo notara. Un valor desconocido puede ser una etiqueta renombrada "
            "o la punta de una sección nueva que el scraper no está extrayendo — que "
            "es lo que pasó con la tarjeta «Operación de pago». Revisar la vista en "
            "el origen y, si el valor es esperado, declararlo en `comun/`."
        ),
        categoria="Cambios del sistema origen",
        prioridad="Alta",
        origen="DEC-081",
        unidad="valores",
        cantidad=len(detalle),
        filas=detalle,
    )


# Campos que el origen debería seguir mostrando. La lista es explícita y no
# derivada del schema: una columna nueva y todavía sin poblar daría un falso
# positivo permanente, y una que legítimamente solo aplica a veces
# —`conductor`, que solo viene con método 'Ruta'— se compara contra sí misma
# mes a mes, no contra el 100%.
# Campos que solo tienen sentido en un pedido ENTREGADO. Medirlos sobre
# "cerrados" mezcla los cancelados —que nunca tuvieron tarjeta de entrega— y
# ensucia la base: sobre cerrados dan 79-83%, sobre entregados dan **100%**.
# Con una línea base limpia, cualquier caída es inequívoca.
_SOLO_ENTREGADOS: frozenset[str] = frozenset(
    {"despachador", "hora_entrega", "obs_entrega", "conductor", "vehiculo_entrega"}
)

_CAMPOS_VIGILADOS: tuple[tuple[str, str], ...] = (
    ("pedidos", "despachador"),
    ("pedidos", "hora_entrega"),
    ("pedidos", "obs_entrega"),
    ("pedidos", "conductor"),
    ("pedidos", "vehiculo_entrega"),
    ("pedidos", "alistador_pedido"),
    ("pedidos", "inspector_pedido"),
    ("pedidos", "nombre_empresa"),
    ("pedidos", "nit"),
    ("pedidos", "metodo_entrega"),
    ("pedidos", "forma_pago"),
    ("subpedidos", "estado"),
    ("subpedidos", "alistador"),
    ("subpedidos", "inspector"),
    ("lineas_pedido", "referencia"),
    ("lineas_pedido", "nombre_producto"),
    ("lineas_pedido", "precio_unitario"),
    ("estadisticas_monto", "concepto"),
    ("registro_operaciones", "accion"),
    ("registro_operaciones", "usuario"),
)

# Puntos porcentuales de caída que disparan el aviso. Por debajo de esto el
# ruido normal del mix de pedidos (más `Ruta` un mes que otro) produciría
# alertas constantes, que es la patología que DEC-070 y DEC-075 ya costaron.
_CAIDA_MINIMA_PP = 20.0

# DEC-065 dejó escrito lo que pasa al hardcodear estados que `comun/` posee:
# dos VIEWs discreparon del dashboard en silencio. Se deriva de la constante.
_CERRADOS_SQL = "(" + ", ".join(f"'{e}'" for e in sorted(ESTADOS_CERRADOS)) + ")"


def cobertura_de_campos_cayendo(con: sqlite3.Connection) -> Hallazgo:
    """Campos que el origen **dejó** de mostrar (DEC-091).

    `vocabulario_nuevo_en_origen` (DEC-081) vigila lo que aparece. Este vigila
    lo contrario, y hacía falta: la SPA dejó de renderizar la tarjeta de
    información de entrega para pedidos viejos y **nadie se enteró durante
    meses**, porque ninguna alerta miraba si una cifra bajaba.

    Peor: el `UPDATE` de persistencia escribía el vacío resultante encima del
    dato bueno, así que cada re-scrape destruía historia irrecuperable
    —`hora_entrega` de `Ruta` cayó de 10.924/10.924 a 8.306/11.750—. Eso ya
    está corregido; este detector cubre el caso que la corrección vuelve
    invisible.

    **Solo mira pedidos cerrados.** Uno abierto todavía se está llenando: un
    pedido de ayer sin `hora_entrega` es normal, no una pérdida, y compararlo
    daría un aviso todos los días.

    Returns:
        Un hallazgo con una fila por campo cuya cobertura cayó más de 20
        puntos porcentuales respecto del mes anterior.
    """
    columnas = {
        "campo": "Campo",
        "mes_anterior": "Mes anterior",
        "mes_actual": "Mes actual",
        "caida_pp": "Caída (pp)",
    }
    filas: list[dict[str, object]] = []

    # El conjunto de entregados se calcula UNA vez, no por campo y por mes:
    # como subconsulta correlacionada la función tardaba minutos (misma
    # trampa que DEC-067). Es una tabla temporal de la conexión, así que no
    # toca el esquema ni deja rastro.
    try:
        con.execute("DROP TABLE IF EXISTS _entregados_tmp")
        con.execute(
            "CREATE TEMP TABLE _entregados_tmp AS "
            "SELECT DISTINCT id_pedido FROM timeline_pedido "
            "WHERE titulo = 'Recibido y recibido'"
        )
        con.execute("CREATE INDEX _ix_entregados ON _entregados_tmp (id_pedido)")
        hay_entregados = True
    except sqlite3.Error:
        # Base recién creada: sin timeline no hay nada que restringir.
        hay_entregados = False

    # Lo mismo con los cerrados: el `NOT EXISTS` sobre subpedidos se evaluaba
    # fila por fila para cada uno de los 20 campos vigilados, y `lineas_pedido`
    # tiene 864k filas.
    try:
        con.execute("DROP TABLE IF EXISTS _cerrados_tmp")
        con.execute(
            f"""CREATE TEMP TABLE _cerrados_tmp AS
                SELECT id_pedido FROM subpedidos GROUP BY id_pedido
                 HAVING SUM(CASE WHEN LOWER(estado) NOT IN {_CERRADOS_SQL}
                            THEN 1 ELSE 0 END) = 0"""  # noqa: S608
        )
        con.execute("CREATE INDEX _ix_cerrados ON _cerrados_tmp (id_pedido)")
        hay_cerrados = True
    except sqlite3.Error:
        hay_cerrados = False
    if not hay_cerrados:
        return Hallazgo(
            clave="cobertura_de_campos_cayendo",
            titulo="El origen dejó de mostrar un campo",
            explicacion="",
            categoria="Cambios del sistema origen",
            prioridad="Alta",
            origen="pedidos.db",
            unidad="campos",
            cantidad=0,
            filas=pd.DataFrame(columns=list(columnas)).rename(columns=columnas),
        )

    for tabla, columna in _CAMPOS_VIGILADOS:
        join = "" if tabla == "pedidos" else "JOIN pedidos p ON p.id_pedido = t.id_pedido"
        fecha = "t.fecha" if tabla == "pedidos" else "p.fecha"
        # Los campos de la tarjeta de entrega solo aplican si el pedido se
        # entregó; el resto, sobre cualquier pedido cerrado.
        entregado = (
            " AND t.id_pedido IN (SELECT id_pedido FROM _entregados_tmp)"
            if columna in _SOLO_ENTREGADOS and hay_entregados
            else ""
        )
        sql = f"""
            SELECT STRFTIME('%Y-%m', {fecha}) AS mes,
                   COUNT(*) AS n,
                   SUM(CASE WHEN t.{columna} IS NOT NULL
                             AND TRIM(t.{columna}) NOT IN ('', '-', '--')
                            THEN 1 ELSE 0 END) AS con
              FROM {tabla} t {join}
             WHERE {fecha} >= DATE('now', '-4 months')
               AND t.id_pedido IN (SELECT id_pedido FROM _cerrados_tmp){entregado}
          GROUP BY 1 ORDER BY 1
        """  # noqa: S608
        try:
            datos = pd.read_sql(sql, con)
        except Exception:  # noqa: BLE001
            continue
        # El mes EN CURSO se excluye por nombre, no por posición: todavía se
        # está poblando y compararlo daría una caída falsa cada primero de
        # mes. Se comparan los dos últimos meses completos.
        mes_actual = datetime.now(timezone.utc).strftime("%Y-%m")
        datos = datos[(datos["mes"] != mes_actual) & (datos["n"] >= 50)]
        if len(datos) < 2:
            continue
        prev, act = datos.iloc[-2], datos.iloc[-1]
        pct_prev = prev["con"] / prev["n"] * 100
        pct_act = act["con"] / act["n"] * 100
        caida = pct_prev - pct_act
        if caida >= _CAIDA_MINIMA_PP:
            filas.append(
                {
                    "campo": f"{tabla}.{columna}",
                    "mes_anterior": f"{prev['mes']}: {pct_prev:.0f}%",
                    "mes_actual": f"{act['mes']}: {pct_act:.0f}%",
                    "caida_pp": round(caida),
                }
            )

    df = pd.DataFrame(filas, columns=list(columnas)).rename(columns=columnas)
    if len(df):
        df = df.sort_values("Caída (pp)", ascending=False)

    return Hallazgo(
        clave="cobertura_de_campos_cayendo",
        titulo="El origen dejó de mostrar un campo",
        explicacion=(
            "Un campo que el sistema origen venía poblando dejó de venir. No es "
            "un error del scraper: la SPA deja de renderizar secciones enteras "
            "para pedidos viejos, y lo que no se capture antes de que eso pase "
            "**no se puede recuperar**. Revisar si hay que adelantar la captura "
            "de ese periodo antes de que la ventana se cierre del todo."
        ),
        categoria="Cambios del sistema origen",
        prioridad="Alta",
        origen="pedidos.db",
        unidad="campos",
        cantidad=len(df),
        filas=df,
    )


def arena_referencias_no_reconocidas(df_admin: pd.DataFrame) -> Hallazgo:
    """Referencias de la categoría Arena que ningún grupo de modalidad reconoce (DEC-118).

    `comun.clasificar_modalidad_arena()` mapea las referencias de Arena
    contra listas verificadas contra el dato real (Unidades, Tonelada
    nacional, Corporativo por patrón, Respaldo, hub de Yumbo) más una lista
    de exclusión conocida (relleno de prueba, `PB008`/`PS0199` pendientes
    de investigar). Una referencia que no cae en ninguna de las dos cosas
    es una modalidad real sin mapear — `inventario.arena.calcular_arena()`
    la deja fuera de `arena_inventario` en silencio si nadie lo revisa acá.
    """
    from comun import ARENA_REFERENCIAS_EXCLUIDAS, clasificar_modalidad_arena

    arena = df_admin[df_admin["categoria"] == "Arena"].copy()
    arena["modalidad"] = arena["referencia"].map(clasificar_modalidad_arena)
    no_reconocidas = arena[
        arena["modalidad"].isna() & ~arena["referencia"].isin(ARENA_REFERENCIAS_EXCLUIDAS)
    ]

    filas = (
        no_reconocidas[["referencia", "nombre_comercial", "almacen", "inventario"]]
        .drop_duplicates(subset=["referencia"])
        .sort_values("referencia")
        .rename(
            columns={
                "referencia": "Referencia",
                "nombre_comercial": "Nombre comercial",
                "almacen": "Almacén (ejemplo)",
                "inventario": "Inventario (ejemplo)",
            }
        )
    )
    return Hallazgo(
        clave="arena_referencias_no_reconocidas",
        titulo="Referencias de Arena sin modalidad reconocida",
        explicacion=(
            "El módulo de Arena clasifica cada referencia en Unidades, Tonelada, "
            "Corporativo, Respaldo o hub de Yumbo. Estas no caen en ninguna — puede "
            "ser una modalidad o ciudad nueva que el clasificador todavía no conoce "
            "(`comun.clasificar_modalidad_arena`). Mientras no se revise, quedan "
            "fuera de `arena_inventario` sin aviso."
        ),
        categoria="Códigos y referencias",
        prioridad="Alta",
        origen="DEC-118",
        unidad="referencias",
        cantidad=filas["Referencia"].nunique(),
        filas=filas,
    )


def detectar_todos(df_admin: pd.DataFrame, con: sqlite3.Connection) -> list[Hallazgo]:
    """Corre todos los detectores y devuelve solo los que encontraron algo.

    Que un detector sin casos no se devuelva **es la mecánica por la que
    una tarea desaparece sola** cuando la inconsistencia se corrige en el
    sistema administrativo.

    Un detector que falle no tumba a los demás: se loguea y se sigue. Que
    una consulta quede desactualizada no debe dejar al módulo entero sin
    datos.

    Args:
        df_admin: Catálogo del admin, sin filtrar por alcance.
        con: Conexión de lectura a pedidos.db.

    Returns:
        Los hallazgos con al menos un caso.
    """
    detectores = [
        lambda: codigos_barras_multiples_ids(df_admin),
        lambda: referencias_con_espacios(df_admin),
        lambda: especificacion_discrepante(df_admin, con),
        lambda: estados_sin_clasificar(con),
        lambda: lineas_sin_id_producto(con),
        lambda: personal_duplicado(con),
        lambda: productos_en_bodega_sin_precio(df_admin, con),
        lambda: sku_en_bodega_fuera_de_catalogo(df_admin, con),
        lambda: vocabulario_nuevo_en_origen(con),
        lambda: cobertura_de_campos_cayendo(con),
        lambda: arena_referencias_no_reconocidas(df_admin),
    ]

    encontrados: list[Hallazgo] = []
    for detector in detectores:
        try:
            hallazgo = detector()
        except Exception:
            logger.exception("hallazgos: un detector falló; se continúa con el resto")
            continue
        if hallazgo.cantidad:
            encontrados.append(hallazgo)
        else:
            logger.info("hallazgos: '%s' sin casos — la tarea desaparece", hallazgo.clave)
    return encontrados
