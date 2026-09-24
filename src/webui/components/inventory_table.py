"""Inventory items as a table, optionally with row selection."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

# Same columns as `inventory list`, plus the resource id (the unique target for keep/expire).
COLUMNS = {
    "environment": "ENV",
    "service": "SERVICE",
    "resource_type": "TYPE",
    "resource_name": "NAME",
    "origin": "ORIGIN",
    "status": "STATUS",
    "keep": "KEEP",
    "expires_at": "EXPIRES",
    "updated_at": "UPDATED",
    "last_deployment_id": "LAST DEPLOYMENT",
    "keep_reason": "KEEP REASON",
    "last_error": "LAST ERROR",
    "resource_id": "RESOURCE ID",
}


def frame(items: list[dict[str, Any]], extra: dict[str, list[Any]] | None = None) -> pd.DataFrame:
    rows = [{h: str(i.get(attr) or "") for attr, h in COLUMNS.items()} for i in items]
    df = pd.DataFrame(rows, columns=list(COLUMNS.values()))
    for column, values in (extra or {}).items():
        df.insert(0, column, values)
    # Hide columns that are empty for every row (e.g. no errors, nothing kept).
    return df.loc[:, (df != "").any(axis=0)] if len(df) else df


def show(
    items: list[dict[str, Any]],
    key: str,
    selectable: bool = False,
    extra: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Render `items`; returns the selected ones (always [] when not `selectable`)."""
    if not items:
        st.info("No hay entradas.")
        return []
    df = frame(items, extra)
    if not selectable:
        st.dataframe(df, hide_index=True, width="stretch", key=key)
        return []
    event = st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        key=key,
        on_select="rerun",
        selection_mode="multi-row",
    )
    return [items[r] for r in event.selection.rows]
