"""inventory audit: compare inventory, AWS and definitions; --apply fixes the inventory only."""

import streamlit as st

from webui.components import filters, run_flow

st.title("Auditoría")
st.caption(
    "Compara el inventario con AWS y con las definiciones del repo. El informe no cambia nada; "
    "**Corregir inventario** arregla solo el inventario (missing, orphan, origin-mismatch), "
    "nunca AWS. Los `undefined` se eliminan en **Eliminar** y los `untracked` se registran en "
    "**Importar**. El código de salida es 1 cuando hay hallazgos."
)

service = filters.service("audit", allow_all=True)
envs = filters.environments("audit", service)

argv = ["inventory", "audit"]
argv += ["--service", service] if service else []
argv += ["--env", ",".join(envs)] if envs else []
run_flow.dry_run_then_apply(
    "audit", argv, dry_label="Auditar (solo informe)", apply_label="Corregir inventario"
)
