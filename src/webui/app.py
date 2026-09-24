"""Web UI for the deploy tool. Run from the repo root with the .env variables exported:

    streamlit run src/webui/app.py --server.address 127.0.0.1     (or: make run-ui)

It has no login and acts with your AWS credentials: keep it on localhost.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:  # same trick as src/main.py; Streamlit reruns this script often
    sys.path.insert(0, _SRC)

import streamlit as st  # noqa: E402

from deploy.cli import AccountMismatchError  # noqa: E402
from deploy.config import MissingEnvVarError  # noqa: E402
from deploy.inventory import InventoryError  # noqa: E402
from webui import backend  # noqa: E402

st.set_page_config(page_title="tf-resource-deploy", page_icon=":material/cloud:", layout="wide")

_VIEWS = Path(__file__).parent / "views"
navigation = st.navigation(
    {
        "Consultar": [
            st.Page(_VIEWS / "overview.py", title="Resumen", icon=":material/home:", default=True),
            st.Page(_VIEWS / "catalog.py", title="Catálogo", icon=":material/menu_book:"),
            st.Page(_VIEWS / "inventory.py", title="Inventario", icon=":material/inventory_2:"),
        ],
        "Operar": [
            st.Page(_VIEWS / "deploy.py", title="Desplegar", icon=":material/rocket_launch:"),
            st.Page(_VIEWS / "import_existing.py", title="Importar", icon=":material/download:"),
            st.Page(_VIEWS / "undeploy.py", title="Eliminar", icon=":material/delete:"),
            st.Page(_VIEWS / "audit.py", title="Auditoría", icon=":material/fact_check:"),
        ],
    }
)

# Same preconditions as the CLI, checked once per rerun so every view can assume them.
with st.sidebar:
    try:
        settings = backend.settings()
        caller = backend.caller_arn()
        ready, table = backend.table_status()
    except (MissingEnvVarError, AccountMismatchError, InventoryError) as e:
        st.error(str(e))
        st.stop()
    except Exception as e:  # e.g. no/expired AWS credentials
        st.error(f"No se pudo conectar con AWS: {type(e).__name__}: {e}")
        st.stop()
    st.markdown(f"**Proyecto:** `{settings['PROJECT_NAME']}`")
    st.markdown(f"**Cuenta:** `{settings['AWS_ACCOUNT_ID']}` · `{settings['AWS_REGION']}`")
    st.caption(f"Identidad: {caller}")
    st.caption(f"Tabla `{settings['INVENTORY_TABLE']}`: {table}")
    if st.button("Recargar datos", icon=":material/refresh:"):
        st.cache_data.clear()
        st.rerun()

navigation.run()
