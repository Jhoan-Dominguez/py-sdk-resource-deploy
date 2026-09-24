"""What each service can deploy per environment, next to what the inventory says is active."""

import pandas as pd
import streamlit as st

from webui import backend

st.title("Catálogo")
st.caption(
    "Lo que `deploy` gestionaría según las definiciones del repo (sin llamar a AWS), "
    "cruzado con el inventario. `no desplegado` = no hay entrada activa con ese nombre."
)

for service in backend.services():
    st.header(service)
    envs = backend.environments(service)
    if not envs:
        st.info("Este servicio no tiene definiciones.")
        continue
    for tab, env in zip(st.tabs(envs), envs, strict=True):
        with tab:
            defs = backend.definitions(service, env)
            if not defs:
                st.info("El servicio no expone su catálogo (`definitions()`).")
                continue
            active = {
                (i["resource_type"], i["resource_name"]): i
                for i in backend.inventory(env, service, "active")
            }
            rows = []
            for d in defs:
                item = active.get((d["resource_type"], d["name"]))
                rows.append(
                    {
                        "TYPE": d["resource_type"],
                        "NAME": d["name"],
                        "SOURCE": d["source"],
                        "DETAIL": d["detail"],
                        "INVENTORY": (
                            f"{item['status']} · {item['origin']}"
                            + (" · keep" if item.get("keep") else "")
                            if item
                            else ("—" if d["resource_type"] == "skipped" else "no desplegado")
                        ),
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            defined = {(d["resource_type"], d["name"]) for d in defs}
            undefined = [
                i
                for key, i in active.items()
                if key not in defined and i["resource_type"] in {d[0] for d in defined}
            ]
            if undefined:
                names = ", ".join(i["resource_name"] for i in undefined)
                st.warning(
                    f"Activos en el inventario pero ya no definidos en el repo: {names}. "
                    "Revísalos en **Auditoría** o elimínalos en **Eliminar**."
                )
