"""
filtros.py — Filtros globales compartidos entre páginas (DEC-101).

`integral.md` pide desde el primer día "filtros por vendedor, forma de pago y
destino" y una herramienta única "con filtros que permiten a cada usuario acotar
la vista a su área de interés". La auditoría de DEC-094 midió el cumplimiento:
**cero usos de `st.session_state` en las 15 páginas**, siete sin filtro de fecha,
y cada una con su propio criterio. Cambiar de página perdía todo el contexto.

Este módulo lo resuelve con tres piezas:

- `barra_lateral()` dibuja los controles en el sidebar y **persiste la selección**
  entre páginas.
- `aplicar()` filtra un DataFrame por las dimensiones que ese DataFrame tenga.
- `aviso_alcance()` dice en pantalla qué está aplicado y qué no.

Desde DEC-165, además, la **bodega**: `selector_bodega()` la elige en la barra
lateral (la llama `app.py`, así vale para todas las páginas) y las páginas la
leen con `bodega_seleccionada()` para pasarla a cada consulta. `franja_historico()`
marca la operación histórica y `exigir_fuente()` reemplaza por un aviso lo que
en esa bodega no tiene datos.

**El alcance es explícito, y eso importa más que el filtro.** Un filtro global
que silenciosamente no aplica a la mitad de las páginas es peor que no tener
filtro: el usuario cree estar viendo un recorte y está viendo el total. Las
páginas de inventario (Bodega vs. sistema, Salud, Clasificación, Mapa, Plan de
conteo) trabajan sobre **la foto de la última corrida**, no sobre un histórico de
pedidos: ni la fecha ni el vendedor tienen sentido ahí, así que no lo consumen y
la barra lo dice.

**Por qué una clave espejo por control.** Streamlit descarta el estado de un
widget cuando el widget deja de dibujarse, y al navegar entre páginas eso pasa
todo el tiempo. Cada control guarda su valor en una clave propia (`_val`) que
sobrevive, y se reinicializa desde ella. Sin eso, los filtros se resetean solos
al cambiar de página — que es exactamente el problema que este módulo viene a
resolver.
"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd
import streamlit as st
from dotenv import dotenv_values

from comun import BODEGA_DEFECTO, BODEGAS, ruta_bodega

# Prefijo de las claves espejo. No colisiona con las claves de los widgets.
_P = "flt_"

# Columna del DataFrame por dimensión filtrable. Si la columna no está, la
# dimensión simplemente no se aplica — no es un error.
_COLUMNAS = {
    "vendedores": "vendedor",
    "clientes": "nombre_empresa",
    "canales": "metodo_entrega",
    "formas_pago": "forma_pago",
}


class Filtros(NamedTuple):
    """Selección vigente. Las tuplas vacías significan «todos»."""

    desde: str
    hasta: str
    vendedores: tuple[str, ...]
    clientes: tuple[str, ...]
    canales: tuple[str, ...]
    formas_pago: tuple[str, ...]

    @property
    def hay_dimensiones(self) -> bool:
        """True si alguna dimensión distinta de la fecha está acotada."""
        return bool(self.vendedores or self.clientes or self.canales or self.formas_pago)


def _recordar(clave: str, defecto):
    """Devuelve el valor guardado para `clave`, sembrándolo la primera vez."""
    espejo = _P + clave
    if espejo not in st.session_state:
        st.session_state[espejo] = defecto
    return st.session_state[espejo]


def _guardar(clave: str, valor) -> None:
    st.session_state[_P + clave] = valor


def barra_lateral(
    min_fecha: str,
    max_fecha: str,
    opciones: dict[str, list[str]] | None = None,
    dias_por_defecto: int | None = None,
) -> Filtros:
    """Dibuja los filtros en el sidebar y devuelve la selección vigente.

    Args:
        min_fecha: Primera fecha con pedidos (YYYY-MM-DD).
        max_fecha: Última fecha con pedidos (YYYY-MM-DD).
        opciones: Valores disponibles por dimensión. Una dimensión ausente o
            vacía no dibuja su control — no tiene sentido ofrecer un filtro de
            vendedor si la página no puede aplicarlo.
        dias_por_defecto: Ventana inicial, solo la primera vez de la sesión.
            `None` = todo el histórico, que es el default deliberado: casi todas
            las páginas que lo consumen muestran evolución mensual, y arrancar
            con 30 días dejaría los gráficos de tendencia con una sola barra.

    Returns:
        Los `Filtros` vigentes, ya persistidos.
    """
    opciones = opciones or {}
    # DEC-165: cada bodega recuerda sus filtros (espejo y widget con la bodega
    # en la clave). Compartidos, el rango de una se arrastraba a la otra: al
    # pasar a la histórica quedaba recortada a los días de la nueva.
    b = bodega_seleccionada()
    dias = pd.date_range(min_fecha, max_fecha, freq="D").strftime("%Y-%m-%d").tolist()
    inicio = dias[0] if dias_por_defecto is None else dias[max(0, len(dias) - dias_por_defecto)]

    with st.sidebar:
        st.markdown("### Filtros")

        guardado = _recordar(f"{b}:rango", (inicio, dias[-1]))
        # El rango guardado puede quedar fuera de la lista si llegaron pedidos
        # nuevos o si la base se acotó: se recorta en vez de reventar.
        desde = guardado[0] if guardado[0] in dias else dias[0]
        hasta = guardado[1] if guardado[1] in dias else dias[-1]

        rango = st.select_slider(
            "Periodo (fecha del pedido)",
            options=dias,
            value=(desde, hasta),
            key=f"w_{b}_rango",
            help="Se comparte con todas las páginas de pedidos, comercial y operación.",
        )
        _guardar(f"{b}:rango", rango)

        seleccion: dict[str, tuple[str, ...]] = {}
        etiquetas = {
            "vendedores": "Vendedor",
            "clientes": "Cliente",
            "canales": "Canal de entrega",
            "formas_pago": "Forma de pago",
        }
        for clave, etiqueta in etiquetas.items():
            disponibles = opciones.get(clave) or []
            if not disponibles:
                seleccion[clave] = ()
                continue
            previo = [v for v in _recordar(f"{b}:{clave}", []) if v in disponibles]
            elegido = st.multiselect(
                etiqueta,
                disponibles,
                default=previo,
                placeholder="Todos...",
                key=f"w_{b}_{clave}",
            )
            _guardar(f"{b}:{clave}", elegido)
            seleccion[clave] = tuple(elegido)

        if st.button("Limpiar filtros", width="stretch"):
            for clave in ("rango", *etiquetas):
                st.session_state.pop(f"{_P}{b}:{clave}", None)
                st.session_state.pop(f"w_{b}_{clave}", None)
            st.rerun()

    return Filtros(
        desde=rango[0],
        hasta=rango[1],
        vendedores=seleccion["vendedores"],
        clientes=seleccion["clientes"],
        canales=seleccion["canales"],
        formas_pago=seleccion["formas_pago"],
    )


def recortar(f: Filtros, dias_max: int) -> tuple[Filtros, bool]:
    """Acota el rango a los últimos `dias_max` días si el global es más ancho.

    **Existe por una medición, no por prudencia.** El consolidado de pedidos
    agrega sobre `lineas_pedido` (875.059 filas): con 7 días tarda 4,4 s y con
    el histórico completo **42,7 s**. Un filtro global cuyo default es todo el
    histórico no puede imponerle eso a la única página que trabaja a nivel de
    línea.

    El recorte **no toca el estado compartido**: la selección del usuario sigue
    intacta para el resto de las páginas. Solo esta consulta usa menos.

    Returns:
        `(filtros_recortados, se_recorto)`. El segundo valor existe para que la
        página lo diga en pantalla — un recorte silencioso sería peor que el
        problema que resuelve.
    """
    desde = pd.Timestamp(f.desde)
    hasta = pd.Timestamp(f.hasta)
    if (hasta - desde).days <= dias_max:
        return f, False
    nuevo = (hasta - pd.Timedelta(days=dias_max)).strftime("%Y-%m-%d")
    return f._replace(desde=nuevo), True


def aplicar(df: pd.DataFrame, f: Filtros) -> pd.DataFrame:
    """Filtra por las dimensiones que el DataFrame realmente tenga.

    La fecha **no** se aplica acá: ya viaja como parámetro a SQL, y volver a
    filtrarla en pandas sería redundante y una segunda fuente de verdad.

    Una dimensión cuya columna no existe se ignora en silencio, a propósito: es
    lo que permite usar la misma función sobre consultas que devuelven distintos
    juegos de columnas sin envolver cada llamada en condicionales.
    """
    if df.empty:
        return df
    vista = df
    for clave, columna in _COLUMNAS.items():
        valores = getattr(f, clave)
        if valores and columna in vista.columns:
            vista = vista[vista[columna].isin(valores)]
    return vista


def aviso_alcance(f: Filtros, aplica_dimensiones: bool = True) -> None:
    """Muestra en la página qué recorte está vigente.

    Se llama aunque no haya filtros puestos: que la barra diga "todos" y la
    página no diga nada deja al lector adivinando si el filtro llegó o no.
    """
    partes = [f"**{f.desde}** → **{f.hasta}**"]
    if aplica_dimensiones:
        for clave, etiqueta in (
            ("vendedores", "vendedor"),
            ("clientes", "cliente"),
            ("canales", "canal"),
            ("formas_pago", "forma de pago"),
        ):
            valores = getattr(f, clave)
            if valores:
                detalle = ", ".join(valores[:3]) + ("…" if len(valores) > 3 else "")
                partes.append(f"{etiqueta}: {detalle}")
    elif f.hay_dimensiones:
        partes.append("_(los filtros de vendedor/cliente/canal no aplican a esta página)_")

    st.caption("🔎 Filtros vigentes — " + " · ".join(partes))


# ─────────────────────────────────────────────
# Bodega (DEC-165)
# ─────────────────────────────────────────────

# Lo que se ve si el archivo de la bodega no define BODEGA_ETIQUETA. El nombre
# real de cada operación es confidencial: vive solo en su `.env`.
_ETIQUETA_DEFECTO = {BODEGA_DEFECTO: "Miempresa", "bodega2": "Bodega 2"}
# La bodega que abre el dashboard si su base existe (respuesta 6 del Arquitecto).
_BODEGA_INICIAL = "bodega2"
_CLAVE_BODEGA = _P + "bodega"


@st.cache_data(ttl=600, show_spinner=False)
def etiqueta_bodega(bodega: str) -> str:
    """Nombre visible de la bodega: `BODEGA_ETIQUETA` de su archivo.

    Se lee con `dotenv_values`, sin cargar el archivo al entorno: el dashboard
    no debe heredar credenciales de ninguna bodega por mostrar su nombre.
    """
    archivo = ruta_bodega("env", bodega)
    valores = dotenv_values(archivo) if archivo.exists() else {}
    return (valores.get("BODEGA_ETIQUETA") or "").strip() or _ETIQUETA_DEFECTO[bodega]


def ruta_visible(ruta) -> str:
    """Ruta relativa a la raíz del proyecto, para nombrarla en pantalla."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    try:
        return Path(ruta).resolve().relative_to(raiz).as_posix()
    except ValueError:
        return str(ruta)


def bodegas_disponibles() -> list[str]:
    """Las bodegas que ya tienen base, en el orden de `comun.BODEGAS`."""
    # Imports de `db` dentro de las funciones: Streamlit corre con dashboard/ en
    # sys.path (`db` es módulo de primer nivel, como en las páginas), pero las
    # pruebas importan `dashboard.filtros` como paquete.
    from db import base_existe

    return [b for b in BODEGAS if base_existe(bodega=b)]


def selector_bodega() -> str:
    """Dibuja el selector en la barra lateral y devuelve la bodega elegida.

    Por defecto `bodega2` si su base existe (hasta su carga inicial no existe y
    el dashboard se ve como siempre); la elección se recuerda entre páginas. Con
    una sola bodega disponible no hay nada que elegir y no se dibuja control.
    """
    disponibles = bodegas_disponibles() or [BODEGA_DEFECTO]
    inicial = _BODEGA_INICIAL if _BODEGA_INICIAL in disponibles else disponibles[0]
    guardada = _recordar("bodega", inicial)
    if guardada not in disponibles:
        guardada = inicial
    if len(disponibles) > 1:
        with st.sidebar:
            elegida = st.radio(
                "Bodega",
                disponibles,
                index=disponibles.index(guardada),
                format_func=etiqueta_bodega,
                key="w_bodega",
                horizontal=True,
            )
    else:
        elegida = guardada
    _guardar("bodega", elegida)
    return elegida


def es_historica(bodega: str) -> bool:
    """True si `bodega` es la operación histórica (DEC-165).

    Para el contenido que describe mediciones de esa operación —un hallazgo,
    un ejemplo con referencias concretas—: mostrado en la otra bodega se leería
    como un dato de ella. No para decidir qué se muestra según los datos: eso
    es `exigir_fuente()`.
    """
    return bodega == BODEGA_DEFECTO


def bodega_seleccionada() -> str:
    """La bodega que eligió `selector_bodega()` en esta sesión."""
    return st.session_state.get(_CLAVE_BODEGA) or BODEGA_DEFECTO


def franja_historico(bodega: str) -> None:
    """Marca la operación histórica cuando conviven las dos (DEC-165).

    Con una sola bodega disponible no se dibuja: no hay otra con qué
    confundirla, y el dashboard se ve como antes de DEC-163.
    """
    if bodega != BODEGA_DEFECTO or len(bodegas_disponibles()) < 2:
        return
    from tz import a_hora_colombia

    from db import get_ultima_actualizacion

    ultima = get_ultima_actualizacion(bodega=bodega)
    ultima = a_hora_colombia(ultima) if ultima else None
    st.warning(
        f"**Histórico — {etiqueta_bodega(bodega)}.** La operación terminó; sus "
        f"pedidos abiertos se siguen actualizando hasta el cierre. "
        f"Última actualización: {ultima or 'sin dato'}.",
        icon=":material/history:",
    )


def exigir_fuente(
    bodega: str,
    fuentes: list[str] | tuple[str, ...],
    que: str,
    *,
    seccion: bool = False,
    motivo: str = "depende del layout de bodega, que esta operación todavía no tiene",
) -> bool:
    """True si todas las `fuentes` tienen datos en la bodega; si no, lo dice.

    «No disponible» se decide por la fuente, no por el nombre de la bodega: el
    día que la fuente tenga filas, la página o la sección aparece sola.

    Args:
        bodega: La bodega seleccionada.
        fuentes: Tablas o VIEWs que la página o sección necesita.
        que: Qué se está ocultando, para el aviso.
        seccion: False detiene la página (`st.stop()`); True solo avisa y
            devuelve False para que la página salte esa sección.
        motivo: Por qué no hay datos.
    """
    from db import fuente_disponible

    if all(fuente_disponible(f, bodega=bodega) for f in fuentes):
        return True
    st.info(
        f"**{que}** no está disponible para {etiqueta_bodega(bodega)}: {motivo}.",
        icon=":material/block:",
    )
    if not seccion:
        st.stop()
    return False
