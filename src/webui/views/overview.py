"""Summary: inventory table state and what is active per environment and service."""

import pandas as pd
import streamlit as st

from webui import backend
from webui.components import run_flow

st.title("Resumen")

ready, status = backend.table_status()
if not ready:
    st.warning(f"La tabla de inventario {status}. Todavía no hay nada registrado.")
    st.write("Crea la tabla (solo en `INVENTORY_TABLE_MODE=create`):")
    run_flow.dry_run_then_apply("init", ["inventory", "init"], apply_label="Crear tabla")
    st.stop()

items = backend.inventory()
active = [i for i in items if i.get("status") == "active"]
expired = backend.expired()

cols = st.columns(4)
cols[0].metric("Activos", len(active))
cols[1].metric("Eliminados (historial)", len(items) - len(active))
cols[2].metric("Protegidos (keep)", sum(1 for i in active if i.get("keep")))
cols[3].metric("Expirados", len(expired))

if expired:
    st.warning(f"{len(expired)} recurso(s) expirados: revísalos en **Eliminar → Expirados**.")
failed = [i for i in active if i.get("last_error")]
if failed:
    st.error(f"{len(failed)} recurso(s) con un undeploy fallido: revisa **Inventario**.")

if active:
    st.subheader("Recursos activos por entorno")
    df = pd.DataFrame(active)
    pivot = df.pivot_table(
        index=["environment", "service"],
        columns="resource_type",
        values="resource_id",
        aggfunc="count",
        fill_value=0,
    )
    st.dataframe(pivot, width="stretch")

    st.subheader("Por origen")
    st.dataframe(
        df.groupby(["environment", "origin"]).size().unstack(fill_value=0),
        width="stretch",
    )
else:
    st.info("No hay recursos activos. Empieza en **Desplegar** o **Importar**.")
