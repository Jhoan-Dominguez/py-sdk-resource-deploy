"""deploy <service> --env ... [--ttl ...]: dry-run first, then --apply."""

import streamlit as st

from webui.components import filters, run_flow

st.title("Desplegar")
st.caption(
    "Crea o actualiza los recursos definidos en el repo y los registra en el inventario. "
    "Todos los entornos elegidos comparten un deployment id."
)

service = filters.service("deploy")
envs = filters.environments("deploy", service)
ttl = st.text_input(
    "Caducidad opcional (--ttl): 12h, 7d, 2w o una fecha", key="deploy:ttl", placeholder="vacío"
)

if not envs:
    st.info("Elige al menos un entorno.")
    st.stop()

argv = ["deploy", service, "--env", ",".join(envs)] + (
    ["--ttl", ttl.strip()] if ttl.strip() else []
)
run_flow.dry_run_then_apply("deploy", argv)
