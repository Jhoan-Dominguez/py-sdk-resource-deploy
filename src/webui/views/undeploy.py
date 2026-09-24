"""undeploy: preview the planner's decision, dry-run, then apply with typed confirmation."""

import streamlit as st

from webui import backend
from webui.components import filters, inventory_table, run_flow

st.title("Eliminar recursos")
st.caption(
    "Solo se borran recursos activos con origen `created` de esta cuenta. Los `adopted`, los "
    "protegidos con keep y lo que depende de ellos se omiten siempre."
)

mode = st.radio(
    "Qué eliminar",
    ["Por servicio y entorno", "Por deployment id", "Expirados"],
    horizontal=True,
    key="undeploy:mode",
)

if mode == "Por servicio y entorno":
    service = filters.service("undeploy")
    envs = filters.environments("undeploy", service)
    names = sorted(
        {i["resource_name"] for e in envs for i in backend.inventory(e, service, "active")}
    )
    only = st.multiselect(
        "Solo estos recursos (vacío = todo el servicio en esos entornos); se incluyen sus "
        "dependientes",
        names,
        key="undeploy:only",
    )
    if not envs:
        st.info("Elige al menos un entorno.")
        st.stop()
    env = ",".join(envs)
    delete, skipped, _ = backend.undeploy_preview(service, env, tuple(only) or None)
    argv = ["undeploy", service, "--env", env]
    argv += [a for name in only for a in ("--resource", name)]
elif mode == "Por deployment id":
    ids = sorted(
        {
            i["created_deployment_id"]
            for i in backend.inventory(status="active")
            if i.get("created_deployment_id")
        },
        reverse=True,
    )
    if not ids:
        st.info("No hay deployments con recursos activos.")
        st.stop()
    deployment_id = st.selectbox("Deployment id (el más reciente primero)", ids)
    delete, skipped, _ = backend.undeploy_preview(deployment_id=deployment_id)
    argv = ["undeploy", "--deployment-id", deployment_id]
else:
    cols = st.columns(2)
    with cols[0]:
        service = filters.service("undeploy-exp", allow_all=True)
    with cols[1]:
        # The CLI filters --expired by a single environment.
        env = filters.environment("undeploy-exp", service)
    delete, skipped, _ = backend.undeploy_preview(service, env, expired=True)
    argv = ["undeploy", *([service] if service else []), "--expired"]
    argv += ["--env", env] if env else []

st.subheader(f"Se eliminarán ({len(delete)})")
inventory_table.show(delete, "undeploy:delete")
if skipped:
    st.subheader(f"Se omitirán ({len(skipped)})")
    inventory_table.show(
        [i for i, _ in skipped], "undeploy:skipped", extra={"MOTIVO": [r for _, r in skipped]}
    )

if not delete:
    st.info("No hay nada que eliminar con esta selección.")
    st.stop()

run_flow.dry_run_then_apply(
    f"undeploy:{mode}",
    argv,
    confirm_envs=",".join(sorted({i["environment"] for i in delete})),
    apply_label="Eliminar",
)
