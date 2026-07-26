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
from difflib import SequenceMatcher

import pandas as pd

from comun import ESTADOS_CONOCIDOS

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
