"""The web UI's only bridge to src/deploy: reads call the library, writes run the CLI.

Every write goes through `deploy.cli.main` with the same arguments you would type in a
terminal, so the CLI's safety rules (dry-run unless --apply, account check, the undeploy
planner, --confirm) apply unchanged. The UI never reimplements them.
"""

from __future__ import annotations

import contextlib
import io
import threading
import traceback
from dataclasses import asdict, dataclass
from typing import Any

import boto3
import streamlit as st

from deploy import cli, config

# redirect_stdout is process-wide and Streamlit serves each browser session from its own
# thread: serialize CLI runs so their outputs don't mix and two AWS writes never overlap.
_CLI_LOCK = threading.Lock()

# Inventory reads are cached this long; every CLI run clears the cache.
_CACHE_SECONDS = 30


@dataclass(frozen=True)
class CliResult:
    argv: tuple[str, ...]
    exit_code: int
    output: str

    @property
    def command(self) -> str:
        return "python3 src/main.py " + " ".join(self.argv)


def run_cli(argv: list[str]) -> CliResult:
    """Run one CLI command and capture everything it prints."""
    out = io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(argv)
        except SystemExit as e:  # argparse usage errors
            code = e.code if isinstance(e.code, int) else 2
        except Exception:  # AWS errors the CLI lets through: show them instead of crashing
            traceback.print_exc()
            code = 1
    st.cache_data.clear()
    return CliResult(tuple(argv), code, out.getvalue())


@st.cache_resource
def session() -> boto3.Session:
    return boto3.Session(region_name=config.aws_region())


@st.cache_data(ttl=300, show_spinner=False)
def caller_arn() -> str:
    """Raises if the credentials don't belong to AWS_ACCOUNT_ID (same check as the CLI)."""
    return cli.verified_caller_arn(session(), config.aws_account_id())


def settings() -> dict[str, str]:
    """The tool's configuration; raises MissingEnvVarError like the CLI does."""
    return {
        "AWS_ACCOUNT_ID": config.aws_account_id(),
        "AWS_REGION": config.aws_region(),
        "PROJECT_NAME": config.project_name(),
        "TRUSTED_PRINCIPAL_USER": config.trusted_principal_user(),
        "INVENTORY_TABLE": config.inventory_table(),
        "INVENTORY_TABLE_MODE": config.inventory_table_mode(),
    }


def _store():
    store = cli.inventory_store(session())
    status = store.ensure_table(apply=False)
    return store, status


@st.cache_data(ttl=_CACHE_SECONDS, show_spinner=False)
def table_status() -> tuple[bool, str]:
    """(ready, status line) of the inventory table, without creating it."""
    store, status = _store()
    return store.ready, status


@st.cache_data(ttl=_CACHE_SECONDS, show_spinner="Leyendo el inventario…")
def inventory(
    env: str | None = None,
    service: str | None = None,
    status: str | None = None,
    deployment_id: str | None = None,
) -> list[dict[str, Any]]:
    store, _ = _store()
    return store.list(env, service, status, deployment_id) if store.ready else []


@st.cache_data(ttl=_CACHE_SECONDS, show_spinner=False)
def expired(env: str | None = None, service: str | None = None) -> list[dict[str, Any]]:
    store, _ = _store()
    return cli.expired_items(store, env, service) if store.ready else []


@st.cache_data(ttl=_CACHE_SECONDS, show_spinner="Calculando el plan…")
def undeploy_preview(
    service: str | None = None,
    env: str | None = None,
    resources: tuple[str, ...] | None = None,
    deployment_id: str | None = None,
    expired: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]], list[str]]:
    """(to delete, skipped with reason, unmatched resource names), from the CLI's planner."""
    store, _ = _store()
    if not store.ready:
        return [], [], []
    plan, unmatched = cli.undeploy_plan(
        store,
        service=service,
        env=env,
        resources=list(resources) if resources else None,
        deployment_id=deployment_id,
        expired=expired,
    )
    return plan.delete, plan.skipped, unmatched


def services() -> list[str]:
    return sorted(cli.SERVICES)


@st.cache_data(show_spinner=False)
def environments(service: str) -> list[str]:
    return cli.SERVICES[service](session()).environments()


@st.cache_data(ttl=_CACHE_SECONDS, show_spinner=False)
def definitions(service: str, env: str) -> list[dict[str, str]]:
    return [asdict(d) for d in cli.SERVICES[service](session()).definitions(env)]
