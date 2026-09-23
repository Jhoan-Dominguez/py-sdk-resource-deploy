"""Decide which inventory items an undeploy may delete. Service-agnostic.

Rules (see docs/undeploy.md):
- Only active items with origin=created in the current account are ever deleted.
- Selecting a resource also selects everything that depends on it (e.g. a role's
  attachments), because it can't be deleted while they exist. A dependent pulled in this
  way is only deleted if something it depends on is actually deleted too; if its parent
  ends up skipped, it stays.
- `keep` protects an item and spreads through dependencies: an item that depends on a kept
  item is protected (a kept role's attachments), and so is everything a protected
  dependent relies on (the policies those attachments use). Otherwise deleting them would
  change the resource the user asked to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .inventory import STATUS_ACTIVE
from .resources import ORIGIN_CREATED

Item = dict[str, Any]


@dataclass
class UndeployPlan:
    delete: list[Item] = field(default_factory=list)
    skipped: list[tuple[Item, str]] = field(default_factory=list)


def _deps(item: Item) -> list[str]:
    return list(item.get("depends_on") or [])


def plan_undeploy(
    inventory: list[Item], selected_ids: set[str], aws_account_id: str
) -> UndeployPlan:
    """`inventory` is every item of the affected (service, environment) partitions."""
    active = {i["resource_id"]: i for i in inventory if i.get("status") == STATUS_ACTIVE}

    # Close the selection over dependents until nothing new is added.
    direct = {rid for rid in selected_ids if rid in active}
    selected = set(direct)
    while True:
        dependents = {
            rid for rid, i in active.items() if rid not in selected and selected & set(_deps(i))
        }
        if not dependents:
            break
        selected |= dependents

    kept = {rid for rid, i in active.items() if i.get("keep")}
    protected_dependents = {
        rid for rid, i in active.items() if _deps(i) and (rid in kept or kept & set(_deps(i)))
    }
    protected = kept | protected_dependents
    protected |= {d for rid in protected_dependents for d in _deps(active[rid])}

    def blocker(rid: str) -> str | None:
        item = active[rid]
        if item.get("account_id") != aws_account_id:
            return f"belongs to account {item.get('account_id')}"
        if item.get("origin") != ORIGIN_CREATED:
            return f"origin={item.get('origin')}: never deleted by undeploy"
        if rid in kept:
            reason = item.get("keep_reason")
            return f"kept ({reason})" if reason else "kept"
        if rid in protected:
            return "linked to a kept resource"
        return None

    reasons = {rid: blocker(rid) for rid in selected}
    deleting = {rid for rid in direct if reasons[rid] is None}
    pulled = selected - direct
    while True:
        added = {
            rid
            for rid in pulled - deleting
            if reasons[rid] is None and deleting & set(_deps(active[rid]))
        }
        if not added:
            break
        deleting |= added

    plan = UndeployPlan()
    for rid in sorted(
        selected, key=lambda r: (active[r]["environment"], active[r]["resource_name"])
    ):
        if rid in deleting:
            plan.delete.append(active[rid])
        else:
            plan.skipped.append(
                (active[rid], reasons[rid] or "what it depends on is not being removed")
            )
    return plan
