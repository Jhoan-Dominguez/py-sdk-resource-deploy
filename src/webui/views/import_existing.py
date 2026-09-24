"""inventory import <service>: tag + record resources that already exist, content untouched."""

import streamlit as st

from webui import backend
from webui.components import filters, run_flow

st.title("Importar recursos existentes")
st.caption(
    "Registra en el inventario recursos que ya existen en AWS (por ejemplo, desplegados antes "
    "de que existiera el inventario). Solo añade etiquetas: nunca cambia su contenido."
)

service = filters.service("import")
envs = filters.environments("import", service)
origin = st.radio(
    "Origen para los recursos sin etiqueta",
    ["created", "adopted"],
    horizontal=True,
    key="import:origin",
    captions=["un undeploy futuro podrá borrarlos", "undeploy nunca los borrará"],
)
if origin == "created":
    st.warning("Usa `created` solo para recursos que desplegó esta herramienta.")
reclassify = st.checkbox(
    "Reclasificar también los que ya tienen otro origen (--reclassify)", key="import:reclassify"
)
names = sorted(
    {
        d["name"]
        for e in envs
        for d in backend.definitions(service, e)
        if d["resource_type"] != "skipped"
    }
)
only = st.multiselect(
    "Solo estos recursos (vacío = todo lo que define el servicio)", names, key="import:only"
)
ttl = st.text_input("Caducidad opcional (--ttl)", key="import:ttl", placeholder="vacío")

if not envs:
    st.info("Elige al menos un entorno.")
    st.stop()

argv = ["inventory", "import", service, "--env", ",".join(envs), "--origin", origin]
argv += ["--reclassify"] if reclassify else []
argv += [a for name in only for a in ("--resource", name)]
argv += ["--ttl", ttl.strip()] if ttl.strip() else []
run_flow.dry_run_then_apply("import", argv)
