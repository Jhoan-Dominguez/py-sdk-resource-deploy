"""Service / environment pickers built from the service registry."""

from __future__ import annotations

import streamlit as st

from webui import backend

ALL = "(todos)"


def service(key: str, allow_all: bool = False) -> str | None:
    options = ([ALL] if allow_all else []) + backend.services()
    choice = st.selectbox("Servicio", options, key=f"{key}:service")
    return None if choice == ALL else choice


def environments(key: str, service_name: str | None, default: list[str] | None = None) -> list[str]:
    """Multi-select of the environments the service has definitions for (all services if None)."""
    names = [service_name] if service_name else backend.services()
    options = sorted({e for s in names for e in backend.environments(s)})
    return st.multiselect("Entornos", options, default=default or [], key=f"{key}:envs")


def environment(key: str, service_name: str | None, allow_all: bool = True) -> str | None:
    names = [service_name] if service_name else backend.services()
    options = sorted({e for s in names for e in backend.environments(s)})
    choice = st.selectbox("Entorno", ([ALL] if allow_all else []) + options, key=f"{key}:env")
    return None if choice == ALL else choice
