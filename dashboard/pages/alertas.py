"""
Centro de alertas — qué requiere acción hoy (DEC-059, B.2).

Las demás páginas responden preguntas; esta responde una sola: **¿hay algo
que atender?** Recorre el estado que el scheduler acaba de calcular y
muestra únicamente lo que está fuera de lo normal, ordenado por severidad.

No duplica los módulos: los referencia. Cada alerta dice de dónde sale y el
detalle se trabaja allá. Lo que aporta es no tener que entrar a seis
páginas a buscar si pasó algo.

**La antigüedad es real, no estimada.** Cada alerta guarda cuándo apareció
por primera vez, así que "esto lleva 12 días abierto" sale del dato. Y una
alerta que deja de emitirse no se borra: queda marcada como resuelta con la
fecha en que se vio por última vez, que es de donde sale el tiempo de
resolución.
"""

import sqlite3

import pandas as pd
import streamlit as st

from db import get_alertas

st.markdown('<p class="dp-breadcrumb">Dashboard / Principal</p>', unsafe_allow_html=True)
st.title("🚨 Centro de alertas")

try:
    df = get_alertas()
except sqlite3.OperationalError as e:
    st.error(f"La base de datos está ocupada momentáneamente ({e}). Recarga en unos segundos.")
    st.stop()

if df.empty:
    st.success("No hay alertas registradas. Se generan en cada ciclo del scheduler.")
    st.stop()

ORDEN = ["Crítica", "Alta", "Media"]
ICONO = {"Crítica": "🔴", "Alta": "🟠", "Media": "🟡"}

ahora = pd.Timestamp.now(tz="UTC")
df["primera_vez_dt"] = pd.to_datetime(df["primera_vez"], errors="coerce", utc=True)
df["visto_dt"] = pd.to_datetime(df["visto_en"], errors="coerce", utc=True)
df["dias_abierta"] = (ahora - df["primera_vez_dt"]).dt.total_seconds() / 86400
df["dias_hasta_cierre"] = (df["visto_dt"] - df["primera_vez_dt"]).dt.total_seconds() / 86400

activas = df[df["activa"] == 1].copy()
resueltas = df[df["activa"] == 0].copy()

c1, c2, c3, c4 = st.columns(4)
for columna, sev in zip((c1, c2, c3), ORDEN, strict=True):
    columna.metric(f"{ICONO[sev]} {sev}", int((activas["severidad"] == sev).sum()))
c4.metric("Resueltas", len(resueltas))

if activas.empty:
    st.success("Ninguna alerta activa en la última corrida.")
else:
    st.divider()

    f1, f2 = st.columns(2)
    sev_sel = f1.multiselect("Severidad", ORDEN, default=ORDEN)
    modulos = sorted(activas["modulo"].dropna().unique())
    mod_sel = f2.multiselect("Módulo de origen", modulos, default=modulos)

    vista = activas[activas["severidad"].isin(sev_sel) & activas["modulo"].isin(mod_sel)].copy()
    vista["_o"] = vista["severidad"].map({s: i for i, s in enumerate(ORDEN)})
    vista = vista.sort_values(["_o", "valor"], ascending=[True, False])
    vista["Sev."] = vista["severidad"].map(ICONO)

    st.dataframe(
        vista[["Sev.", "tipo", "entidad", "detalle", "dias_abierta", "modulo"]],
        hide_index=True,
        width="stretch",
        height=460,
        column_config={
            "Sev.": st.column_config.TextColumn("Sev.", width="small"),
            "tipo": st.column_config.TextColumn("Tipo", width="medium"),
            "entidad": st.column_config.TextColumn("Qué", width="medium"),
            "detalle": st.column_config.TextColumn("Detalle", width="large"),
            "dias_abierta": st.column_config.NumberColumn("Días abierta", format="%.0f"),
            "modulo": st.column_config.TextColumn("Ver en", width="small"),
        },
    )
    st.download_button(
        "⬇️ Descargar en CSV",
        vista[["severidad", "tipo", "entidad", "detalle", "dias_abierta", "modulo"]]
        .to_csv(index=False)
        .encode("utf-8-sig"),
        file_name="alertas.csv",
        mime="text/csv",
    )

    reincidentes = activas[activas["dias_abierta"] > 7]
    if len(reincidentes):
        st.warning(
            f"**{len(reincidentes)} alertas llevan más de una semana abiertas.** "
            "Una alerta que persiste dejó de ser un evento y pasó a ser una condición: "
            "o se resuelve la causa, o el umbral que la dispara no refleja la operación real."
        )

if len(resueltas):
    with st.expander(f"Resueltas ({len(resueltas)})"):
        st.markdown(
            "Dejaron de emitirse, así que la condición que las originó desapareció. "
            "El tiempo de cierre es la diferencia entre su primera y su última aparición."
        )
        mediana = resueltas["dias_hasta_cierre"].median()
        if pd.notna(mediana):
            st.metric("Tiempo mediano de resolución", f"{mediana:.1f} días")
        st.dataframe(
            resueltas[["severidad", "tipo", "entidad", "detalle", "dias_hasta_cierre"]]
            .sort_values("dias_hasta_cierre", ascending=False)
            .rename(
                columns={
                    "severidad": "Severidad",
                    "tipo": "Tipo",
                    "entidad": "Qué",
                    "detalle": "Detalle",
                    "dias_hasta_cierre": "Días hasta cierre",
                }
            ),
            hide_index=True,
            width="stretch",
        )

st.caption(
    "Las alertas se recalculan en cada ciclo del scheduler. Una que se resuelve en el "
    "origen desaparece sola de la lista activa — no hay que cerrarla a mano."
)
