"""Resource records and ownership tags shared by every service deployer.

Every resource the tool creates or adopts is tagged in AWS with the TAG_* keys below
(so AWS itself says who owns it) and recorded in the inventory table (see inventory.py).
Tag keys are namespaced so they never collide with tags Terraform's `default_tags`
puts on the same resources (`ManagedBy`, `Project`, `Environment`, ...).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

TOOL_NAME = "tf-resource-deploy"

TAG_PREFIX = "resource-deploy:"
TAG_MANAGED_BY = f"{TAG_PREFIX}managed-by"
TAG_PROJECT = f"{TAG_PREFIX}project"
TAG_ENVIRONMENT = f"{TAG_PREFIX}environment"
TAG_SERVICE = f"{TAG_PREFIX}service"
TAG_DEPLOYMENT_ID = f"{TAG_PREFIX}deployment-id"
TAG_ORIGIN = f"{TAG_PREFIX}origin"

# created: the tool created the resource, so a future undeploy may delete it.
# adopted: it already existed when the tool first saw it; undeploy must never delete it.
ORIGIN_CREATED = "created"
ORIGIN_ADOPTED = "adopted"
ORIGINS = (ORIGIN_CREATED, ORIGIN_ADOPTED)

# What the current CLI run is doing; stored in the inventory as `last_action`.
ACTION_DEPLOY = "deploy"
ACTION_IMPORT = "import"
ACTION_UNDEPLOY = "undeploy"
ACTION_AUDIT = "audit"


@dataclass(frozen=True)
class ResourceRecord:
    """One deployed resource, as stored in the inventory."""

    # ARN, or a synthetic id for things without one (e.g. a role/policy attachment).
    resource_id: str
    resource_type: str
    name: str
    origin: str
    depends_on: tuple[str, ...] = ()
    # Real creation time from AWS when known (imports); otherwise the inventory uses "now".
    created_at: str | None = None


@dataclass(frozen=True)
class DeployRun:
    """Everything a service needs to know about the current CLI run for one environment."""

    service: str
    environment: str
    project_name: str
    aws_account_id: str
    apply: bool
    deployment_id: str
    caller_arn: str
    # Persists a record in the inventory; a no-op on dry-runs.
    record: Callable[[ResourceRecord], None]
    action: str = ACTION_DEPLOY
    # When set, every recorded resource gets this `expires_at` (deploy/import --ttl).
    expires_at: str | None = None
    # Undeploy only: report the outcome for one inventory item (error None = deleted or
    # already gone). Only persisted on applied runs.
    record_removal: Callable[[dict[str, Any], str | None], None] = field(
        default=lambda item, error: None
    )

    def tags(self, origin: str) -> list[dict[str, str]]:
        """Ownership tags in the Key/Value list shape IAM, DynamoDB, etc. expect."""
        values = {
            TAG_MANAGED_BY: TOOL_NAME,
            TAG_PROJECT: self.project_name,
            TAG_ENVIRONMENT: self.environment,
            TAG_SERVICE: self.service,
            TAG_DEPLOYMENT_ID: self.deployment_id,
            TAG_ORIGIN: origin,
        }
        return [{"Key": k, "Value": v} for k, v in values.items()]


@dataclass(frozen=True)
class ImportOptions:
    """How `inventory import` should classify the existing resources it finds."""

    # Origin for resources that have no origin tag yet.
    origin: str
    # Also overwrite the origin of resources already tagged with a different one.
    reclassify: bool
    # Only these role/policy names (None = everything the service defines).
    only: frozenset[str] | None

    def selects(self, name: str) -> bool:
        return self.only is None or name in self.only


@dataclass(frozen=True)
class Finding:
    """One difference `inventory audit` found between the inventory, AWS and the source."""

    kind: str
    resource_type: str
    name: str
    detail: str
    # Inventory item involved, if any.
    item: dict[str, Any] | None = None
    # What the inventory should hold instead (orphans, origin mismatches), if fixable.
    record: ResourceRecord | None = None


@dataclass(frozen=True)
class Definition:
    """One resource a service's source defines for an environment (what `deploy` would
    manage), as listed by the web UI's catalog. Nothing here reflects AWS state."""

    resource_type: str
    name: str
    # Where it comes from (e.g. the JSON file name).
    source: str
    detail: str = ""


# Finding kinds. MISSING/ORPHAN/ORIGIN_MISMATCH can be fixed in the inventory by
# `audit --apply`; UNDEFINED/UNTRACKED need a human decision (undeploy / import).
FINDING_MISSING = "missing"
FINDING_ORPHAN = "orphan"
FINDING_ORIGIN_MISMATCH = "origin-mismatch"
FINDING_UNDEFINED = "undefined"
FINDING_UNTRACKED = "untracked"


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_DURATION = re.compile(r"^(\d+)([hdw])$")
_DURATION_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


class ExpiryError(ValueError):
    pass


def parse_expiry(text: str, now: datetime | None = None) -> str:
    """`12h` / `7d` / `2w` from now, or an absolute `YYYY-MM-DD[THH:MM[:SS]][Z]` (UTC)."""
    now = now or datetime.now(UTC)
    match = _DURATION.match(text.strip())
    if match:
        amount, unit = int(match.group(1)), _DURATION_UNITS[match.group(2)]
        return format_timestamp(now + timedelta(**{unit: amount}))
    try:
        moment = datetime.fromisoformat(text.strip().removesuffix("Z"))
    except ValueError:
        raise ExpiryError(
            f"Invalid expiry '{text}': use a duration (12h, 7d, 2w) or a date "
            "(2026-10-01, 2026-10-01T18:00)."
        ) from None
    return format_timestamp(moment.replace(tzinfo=moment.tzinfo or UTC))


def tags_to_dict(tags: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in tags or []}
