"""Common interface for deploying one AWS service via boto3.

Each service (iam now; s3, dynamodb, etc. later) implements this interface
and gets registered in `deploy.cli.SERVICES` to become selectable on the CLI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import boto3

from ..resources import Definition, DeployRun, Finding, ImportOptions


class ServiceDeployer(ABC):
    name: str

    @abstractmethod
    def __init__(self, session: boto3.Session) -> None:
        """Create the boto3 clients the service needs from `session`."""

    @abstractmethod
    def deploy(self, run: DeployRun) -> None:
        """Print what would change in `run.environment`; if `run.apply`, also apply it.

        Must tag every resource it creates with `run.tags(ORIGIN_CREATED)`, tag untagged
        pre-existing resources it takes over with `run.tags(ORIGIN_ADOPTED)`, and call
        `run.record(...)` right after each resource is created/verified, so the inventory
        reflects reality even if a later step fails.
        """

    @abstractmethod
    def import_existing(self, run: DeployRun, options: ImportOptions) -> None:
        """Record resources this service defines that already exist in AWS.

        Never changes their content: only adds/updates ownership tags (when `run.apply`) and
        calls `run.record(...)`. Resources that don't exist are reported and skipped. An
        untagged resource gets `options.origin`; an already-tagged one keeps its origin
        unless `options.reclassify` is set.
        """

    @abstractmethod
    def undeploy(self, run: DeployRun, items: list[dict[str, Any]]) -> None:
        """Delete `items` (inventory items already cleared by deploy.undeploy.plan_undeploy).

        Must delete in dependency order, print one status line per item, never delete a
        resource whose AWS origin tag isn't `created`, and call `run.record_removal(item,
        error)` for each one (error None = deleted or already gone). Only call write APIs
        when `run.apply`; a failure on one item must not stop the others.
        """

    @abstractmethod
    def environments(self) -> list[str]:
        """Environments this service has definitions for (audit checks them by default)."""

    @abstractmethod
    def audit(self, run: DeployRun, items: list[dict[str, Any]]) -> list[Finding]:
        """Compare `items` (this service/environment's inventory, any status) with AWS and
        with the service's own definitions. Read-only: never changes AWS or the inventory.

        Report FINDING_MISSING (active in the inventory, gone from AWS), FINDING_ORPHAN
        (carries this project/environment's ownership tags but isn't active in the
        inventory; include the `record` it should have), FINDING_ORIGIN_MISMATCH (AWS
        origin tag differs from the inventory; `record` with the tag's origin),
        FINDING_UNDEFINED (active but no longer defined in the source) and
        FINDING_UNTRACKED (defined, exists in AWS, but untagged and not in the inventory).
        """

    def definitions(self, environment: str) -> list[Definition]:
        """What `deploy` would manage in `environment`, from the service's own source, without
        calling AWS. Optional: used only by the web UI's catalog (empty = nothing to show).
        """
        return []
