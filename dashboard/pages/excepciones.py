"""
Excepciones del proceso — lo que sale mal, y quién lo autoriza (DEC-099).

`registro_operaciones` emite 19 acciones distintas y el dashboard leía cuatro.
Las que faltaban no son ruido: describen **el circuito de excepción** —cuándo la
bodega no puede completar un pedido, quién autoriza despacharlo corto, cuándo
falla una entrega y cuándo alguien ajusta cantidades a mano—. Nada de eso tenía
pantalla.

Esta página **no repite el fill rate**, que ya vive en Operación → Cumplimiento
de despacho y mide *cuánto* faltó. Acá se mide **el proceso de decisión** sobre
ese faltante: cuánto tarda, quién lo aprueba, y qué pasa cuando no se aprueba.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st
from filtros import aplicar, aviso_alcance, barra_lateral

from db import get_excepciones, get_opciones_comerciales, get_rango_fechas
from theme import STATUS_CRITICAL

st.markdown('<p class="dp-breadcrumb">Dashboard / Operación</p>', unsafe_allow_html=True)
st.title("⚠️ Excepciones del proceso")

try:
    _min_fecha, _max_fecha = get_rango_fechas()
except (FileNotFoundError, sqlite3.OperationalError) as e:
    st.error(f"No se pudo leer la base ({e}).")
    st.stop()

if not _min_fecha:
    st.info("Todavía no hay pedidos en la base de datos.")
    st.stop()

_f = barra_lateral(_min_fecha, _max_fecha, get_opciones_comerciales())
aviso_alcance(_f)

try:
    df = aplicar(get_excepciones(_f.desde, _f.hasta), _f)
except sqlite3.OperationalError as e:
    st.error(f"La base está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.info("Sin datos de excepciones en el periodo. ¿Corrió el ETL?")
    st.stop()

for col in ("faltante_en", "aprobado_en", "rechazado_en", "entrega_cancelada_en"):
    df[col + "_dt"] = pd.to_datetime(df[col], errors="coerce")
df["entregado_dt"] = pd.to_datetime(df["entregado_en"], errors="coerce")
df["ultima_entrega_dt"] = pd.to_datetime(df["ultima_entrega_en"], errors="coerce")

con_faltante = df[df["faltante_en"].notna()]
rechazados = df[df["rechazado_en"].notna()]

# ── 1. El circuito de aprobación del faltante ──────────────────────────────────
st.subheader("Cuando la bodega no puede completar el pedido")

if con_faltante.empty:
    st.success("Ningún pedido del periodo tuvo faltantes de alistamiento.")
else:
    # Un rechazo seguido de aprobación es retrabajo resuelto; sin aprobación
    # posterior, es una decisión que quedó en pie.
    sin_resolver = rechazados[
        rechazados["aprobado_en_dt"].isna()
        | (rechazados["aprobado_en_dt"] < rechazados["rechazado_en_dt"])
    ]
    decision_h = (
        (
            con_faltante[["aprobado_en_dt", "rechazado_en_dt"]].min(axis=1)
            - con_faltante["faltante_en_dt"]
        ).dt.total_seconds()
        / 3600
    ).dropna()
    decision_h = decision_h[decision_h >= 0]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Pedidos con faltante", f"{len(con_faltante):,}".replace(",", "."))
    k2.metric(
        "Tasa de no aprobación",
        f"{100 * len(rechazados) / len(con_faltante):.1f}%",
        help=f"{len(rechazados)} pedidos con al menos un «Faltantes no aprobados».",
    )
    k3.metric(
        "Decisión mediana",
        f"{decision_h.median():.1f} h" if len(decision_h) else "—",
        help=f"p90: {decision_h.quantile(0.9):.1f} h" if len(decision_h) else "",
    )
    k4.metric(
        "Rechazos que siguen en pie",
        f"{len(sin_resolver):,}".replace(",", "."),
        help="Sin un «Faltantes aprobados» posterior que los levante.",
    )

    # ¿El monto explica el rechazo? La respuesta medida es que no.
    con_monto = con_faltante[con_faltante["monto_diferencia"].notna()]
    if not con_monto.empty:
        rech_monto = con_monto[con_monto["rechazado_en"].notna()]["monto_diferencia"]
        apr_monto = con_monto[con_monto["rechazado_en"].isna()]["monto_diferencia"]
        if len(rech_monto) and len(apr_monto):
            st.markdown(
                f"**El monto no explica el rechazo.** El faltante mediano de los pedidos "
                f"rechazados es de **${rech_monto.median():,.0f}** contra "
                f"**${apr_monto.median():,.0f}** de los aprobados — si acaso, se rechaza "
                "lo más chico. Sea cual sea el criterio de quien autoriza, **no es el "
                "valor de lo que falta**, que es lo que uno esperaría.".replace(",", ".")
            )

    # Lo accionable: rechazado, no levantado y nunca entregado.
    trabados = sin_resolver[sin_resolver["entregado_dt"].isna()]
    if not trabados.empty:
        st.markdown(
            f"<div style='border-left:3px solid {STATUS_CRITICAL};padding-left:12px'>"
            f"<b>{len(trabados)} pedidos tienen el faltante rechazado, sin aprobación "
            "posterior y sin entrega registrada.</b> Es el único grupo de esta sección "
            "donde hay algo que hacer: el resto ya se resolvió de una forma u otra."
            "</div>",
            unsafe_allow_html=True,
        )
        vista = trabados.sort_values("faltante_en", ascending=False)
        st.dataframe(
            vista[
                [
                    "id_pedido",
                    "fecha",
                    "nombre_empresa",
                    "metodo_entrega",
                    "faltante_en",
                    "rechazado_en",
                    "productos_con_faltante",
                    "monto_diferencia",
                ]
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "id_pedido": st.column_config.TextColumn("Pedido"),
                "fecha": st.column_config.TextColumn("Fecha"),
                "nombre_empresa": st.column_config.TextColumn("Cliente", width="medium"),
                "metodo_entrega": st.column_config.TextColumn("Canal"),
                "faltante_en": st.column_config.TextColumn("Faltante detectado"),
                "rechazado_en": st.column_config.TextColumn("Rechazado"),
                "productos_con_faltante": st.column_config.NumberColumn("Productos", format="%d"),
                "monto_diferencia": st.column_config.NumberColumn("Monto", format="$%.0f"),
            },
        )
        st.download_button(
            "⬇️ Descargar faltantes trabados (CSV)",
            vista.to_csv(index=False).encode("utf-8-sig"),
            file_name="faltantes_trabados.csv",
            mime="text/csv",
        )
    else:
        st.success("Ningún faltante rechazado quedó sin resolver y sin entregar.")

# ── 2. Entregas que fallaron ───────────────────────────────────────────────────
st.divider()
st.subheader("Entregas canceladas")

canceladas = df[df["entrega_cancelada_en"].notna()]
if canceladas.empty:
    st.success("Ninguna entrega cancelada en el periodo.")
else:
    # Una entrega cancelada y reintentada después no es una entrega perdida.
    sin_reintento = canceladas[
        canceladas["ultima_entrega_dt"].isna()
        | (canceladas["ultima_entrega_dt"] < canceladas["entrega_cancelada_en_dt"])
    ]
    c1, c2, c3 = st.columns(3)
    c1.metric("Entregas canceladas", f"{len(canceladas):,}".replace(",", "."))
    c2.metric(
        "Reintentadas con éxito",
        f"{len(canceladas) - len(sin_reintento):,}".replace(",", "."),
        help="Tienen un evento de entrega posterior a la cancelación.",
    )
    c3.metric(
        "Sin entrega posterior",
        f"{len(sin_reintento):,}".replace(",", "."),
        delta="Requieren revisión" if len(sin_reintento) else "Ninguna",
        delta_color="inverse" if len(sin_reintento) else "normal",
    )
    st.caption(
        f"El {100 * (len(canceladas) - len(sin_reintento)) / len(canceladas):.0f}% de las "
        "entregas canceladas se reintenta y llega. **Contarlas todas como entregas "
        "fallidas sería un error de lectura**: la cancelación es un evento del proceso, "
        "no necesariamente un fracaso. Lo que sí requiere revisión son las que no "
        "volvieron a intentarse."
    )
    if not sin_reintento.empty:
        with st.expander(f"Ver las {len(sin_reintento)} sin entrega posterior"):
            st.dataframe(
                sin_reintento[
                    [
                        "id_pedido",
                        "fecha",
                        "nombre_empresa",
                        "metodo_entrega",
                        "entrega_cancelada_en",
                    ]
                ].sort_values("entrega_cancelada_en", ascending=False),
                hide_index=True,
                width="stretch",
            )

# ── 3. Control interno ─────────────────────────────────────────────────────────
st.divider()
st.subheader("Ajustes manuales de cantidad")

ajustes = df[df["ajustes_manuales"] > 0]
st.markdown(
    "`Modificar cantidad de entrega manualmente` es la **única acción del origen que "
    "altera cantidades por fuera del proceso** de alistamiento e inspección. El volumen "
    "es ínfimo y la relevancia no: es exactamente el evento que un auditor busca."
)
if ajustes.empty:
    st.success("Ningún ajuste manual de cantidades en el periodo.")
else:
    a1, a2 = st.columns(2)
    a1.metric("Pedidos ajustados", len(ajustes))
    a2.metric("Personas que ajustaron", int(ajustes["ajustado_por"].nunique()))
    st.dataframe(
        ajustes[
            ["id_pedido", "fecha", "nombre_empresa", "ajustes_manuales", "ajustado_por"]
        ].sort_values("fecha", ascending=False),
        hide_index=True,
        width="stretch",
        column_config={
            "id_pedido": st.column_config.TextColumn("Pedido"),
            "fecha": st.column_config.TextColumn("Fecha"),
            "nombre_empresa": st.column_config.TextColumn("Cliente", width="medium"),
            "ajustes_manuales": st.column_config.NumberColumn("Ajustes", format="%d"),
            "ajustado_por": st.column_config.TextColumn("Quién"),
        },
    )

# ── 4. Autogestión del cliente ─────────────────────────────────────────────────
st.divider()
st.subheader("Quién crea los pedidos")

autog = int(df["autogestion"].sum())
total = len(df)
g1, g2 = st.columns([1, 2])
g1.metric(
    "Pedidos creados por el cliente",
    f"{100 * autog / total:.1f}%",
    help=f"{autog:,} de {total:,} pedidos.".replace(",", "."),
)
g2.markdown(
    "El origen marca con `tipo_usuario = 'member'` los pedidos que registra el propio "
    "cliente, y son la **abrumadora mayoría**. Es un hecho estructural del canal que no "
    "estaba en ninguna pantalla y que cambia la lectura de otras cifras: las "
    "cancelaciones por falta de pago, los datos de entrega incompletos y las "
    "reprogramaciones no son necesariamente errores del equipo interno — buena parte "
    "del dato lo captura alguien de afuera."
)
st.caption(
    f"Los {total - autog:,} pedidos restantes no tienen el evento con marca `member`: ".replace(
        ",", "."
    )
    + "o los creó personal interno, o son anteriores al 2026-01-23, que es cuando el "
    "origen empezó a emitir el registro de operaciones."
)
