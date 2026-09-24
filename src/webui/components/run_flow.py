"""The dry-run → apply flow every AWS-changing view goes through."""

from __future__ import annotations

import streamlit as st

from webui import backend
from webui.backend import CliResult


def show_result(result: CliResult, title: str) -> None:
    ok = result.exit_code == 0
    (st.success if ok else st.warning)(f"{title} — código de salida {result.exit_code}")
    st.caption(f"`{result.command}`")
    st.code(result.output or "(sin salida)", language="text")


def dry_run_then_apply(
    key: str,
    argv: list[str],
    *,
    confirm_envs: str | None = None,
    dry_label: str = "Simular (dry-run)",
    apply_label: str = "Aplicar",
) -> None:
    """A dry-run button, then an apply button that only unlocks for the exact same `argv`.

    `confirm_envs` (undeploy): the affected environments, sorted and comma-separated. The
    user has to type them, and they're passed as --confirm because the web server has no
    terminal for the CLI to ask on.
    """
    state = st.session_state.setdefault(f"flow:{key}", {})
    dry_col, apply_col = st.columns(2)
    if dry_col.button(dry_label, key=f"{key}:dry", width="stretch"):
        state.clear()
        state["dry_argv"] = argv
        state["dry"] = backend.run_cli(argv)

    ready = state.get("dry_argv") == argv
    if state.get("dry_argv") and not ready:
        st.info("Los parámetros cambiaron desde la última simulación: simula de nuevo.")
    typed_ok = True
    if ready and confirm_envs:
        typed = st.text_input(
            f"Esto borra recursos en AWS. Escribe `{confirm_envs}` para confirmar",
            key=f"{key}:confirm",
        )
        typed_ok = typed.strip() == confirm_envs

    if apply_col.button(
        apply_label,
        key=f"{key}:apply",
        type="primary",
        disabled=not (ready and typed_ok),
        width="stretch",
    ):
        apply_argv = [*argv, "--apply"] + (["--confirm", confirm_envs] if confirm_envs else [])
        state["applied"] = backend.run_cli(apply_argv)
        # Applying again needs a fresh dry-run: AWS and the inventory just changed.
        state.pop("dry_argv", None)
        state.pop("dry", None)

    if "dry" in state:
        show_result(state["dry"], "Simulación")
    if "applied" in state:
        show_result(state["applied"], "Aplicado")


def run_once(key: str, argv: list[str], label: str, *, disabled: bool = False) -> None:
    """For commands that only touch the inventory and apply immediately (keep, expire)."""
    if st.button(label, key=f"{key}:run", disabled=disabled):
        st.session_state[f"once:{key}"] = backend.run_cli(argv)
    if f"once:{key}" in st.session_state:
        show_result(st.session_state[f"once:{key}"], label)
