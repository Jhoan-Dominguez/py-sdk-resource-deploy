"""Everything recorded in the inventory, plus keep/unkeep and expiry on selected rows."""

import streamlit as st

from webui import backend
from webui.components import filters, inventory_table, run_flow

st.title("Inventario")

cols = st.columns(3)
with cols[0]:
    service = filters.service("inv", allow_all=True)
with cols[1]:
    env = filters.environment("inv", service)
with cols[2]:
    status = st.selectbox("Estado", ["active", "deleted", "(todos)"], key="inv:status")

items = backend.inventory(env, service, None if status == "(todos)" else status)
st.caption(f"{len(items)} recurso(s). Selecciona filas para protegerlas o darles caducidad.")
selected = inventory_table.show(items, "inv:table", selectable=True)

if selected:
    active = [i for i in selected if i.get("status") == "active"]
    st.subheader(f"Acciones sobre {len(active)} recurso(s) activo(s) seleccionados")
    st.caption("Solo cambian el inventario y se aplican al momento (como en el CLI).")
    # The resource id (ARN or attachment id) is the one target that matches a single entry.
    targets = [i["resource_id"] for i in active]
    keep_tab, expire_tab = st.tabs(["Keep / unkeep", "Caducidad"])
    with keep_tab:
        reason = st.text_input("Motivo (opcional)", key="inv:reason")
        argv = ["inventory", "keep", *targets] + (["--reason", reason] if reason else [])
        run_flow.run_once("inv:keep", argv, "Proteger (keep)", disabled=not targets)
        run_flow.run_once(
            "inv:unkeep",
            ["inventory", "unkeep", *targets],
            "Quitar protección (unkeep)",
            disabled=not targets,
        )
    with expire_tab:
        mode = st.radio(
            "Caduca", ["dentro de", "en la fecha", "sin caducidad"], horizontal=True, key="inv:mode"
        )
        if mode == "dentro de":
            when = st.text_input("Duración (12h, 7d, 2w)", value="7d", key="inv:in")
            option = ["--in", when]
        elif mode == "en la fecha":
            when = st.text_input("Fecha UTC (2026-10-01 o 2026-10-01T18:00)", key="inv:at")
            option = ["--at", when]
        else:
            option = ["--clear"]
        run_flow.run_once(
            "inv:expire",
            ["inventory", "expire", *targets, *option],
            "Guardar caducidad",
            disabled=not targets or option[-1] == "",
        )

st.divider()
st.subheader("Expirados")
st.caption("Activos con la caducidad vencida; se eliminan desde **Eliminar → Expirados**.")
inventory_table.show(backend.expired(env, service), "inv:expired")
