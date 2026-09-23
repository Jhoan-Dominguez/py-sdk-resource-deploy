"""CLI to deploy this project's AWS resources via boto3 (not Terraform).

Usage (from the repo root, via the src/main.py entry point):
    python3 src/main.py deploy iam --env dev               # dry-run: no write APIs are called
    python3 src/main.py deploy iam --env dev --apply        # applies the changes to AWS
    python3 src/main.py deploy iam --env dev,mock,stg,prod  # several environments in one run
    python3 src/main.py inventory init [--apply]            # check/create the inventory table
    python3 src/main.py inventory list [--env dev] [--service iam] [--status active]
    python3 src/main.py inventory import iam --env dev --origin created [--apply]
    python3 src/main.py inventory keep|unkeep <name|arn|deployment-id>... [--reason TEXT]
    python3 src/main.py undeploy iam --env dev [--resource NAME]... [--apply]
    python3 src/main.py undeploy --deployment-id <id> [--apply]
    python3 src/main.py undeploy --expired [--apply]
    python3 src/main.py inventory expire <target>... --in 7d | --at 2026-10-01 | --clear
    python3 src/main.py inventory expired
    python3 src/main.py inventory audit [--service iam] [--env dev] [--apply]

Every applied deploy records what it created/adopted in the inventory table
(INVENTORY_TABLE); see docs/ for the full guide.

To add a new service (s3, dynamodb, ...): create `services/<name>.py` with a
class implementing `ServiceDeployer` (see services/base.py and the example in
services/iam.py) and register it in SERVICES below.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import boto3

from . import config
from .config import MissingEnvVarError
from .inventory import STATUS_ACTIVE, InventoryError, InventoryStore
from .resources import (
    ACTION_AUDIT,
    ACTION_DEPLOY,
    ACTION_IMPORT,
    ACTION_UNDEPLOY,
    FINDING_MISSING,
    FINDING_UNDEFINED,
    FINDING_UNTRACKED,
    ORIGIN_CREATED,
    ORIGINS,
    DeployRun,
    ExpiryError,
    ImportOptions,
    ResourceRecord,
    format_timestamp,
    parse_expiry,
)
from .services.base import ServiceDeployer
from .services.iam import IamService
from .undeploy import plan_undeploy

SERVICES: dict[str, type[ServiceDeployer]] = {
    "iam": IamService,
}


def _new_deployment_id() -> str:
    # Sortable by time, unique across concurrent runs.
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


class AccountMismatchError(RuntimeError):
    pass


def _verified_caller_arn(session: boto3.Session, aws_account_id: str) -> str:
    # Fail fast if AWS_ACCOUNT_ID doesn't match who we're actually authenticated as,
    # instead of silently creating resources (or inventory entries) in the wrong account.
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != aws_account_id:
        raise AccountMismatchError(
            f"AWS_ACCOUNT_ID={aws_account_id} does not match the authenticated AWS "
            f"account ({identity['Account']}); refusing to continue to avoid touching the "
            "wrong account."
        )
    return identity["Arn"]


def _inventory(session: boto3.Session) -> InventoryStore:
    return InventoryStore(
        session.client("dynamodb"),
        table_name=config.inventory_table(),
        mode=config.inventory_table_mode(),
        project_name=config.project_name(),
    )


def _runs(args: argparse.Namespace, session: boto3.Session, caller_arn: str, action: str):
    """Yield (deployer, run) per environment in --env, all sharing one deployment id.

    The inventory table is validated (or, in create mode with --apply, created) first, so
    a missing/invalid table stops the command before any service is touched.
    """
    # Parsed once so every environment of the run gets the same expiry.
    expires_at = parse_expiry(args.ttl) if args.ttl else None
    inventory = _inventory(session)
    print(f"inventory table {inventory.table_name}: {inventory.ensure_table(args.apply)}")
    if expires_at:
        print(f"recorded resources will expire at {expires_at}")

    def record(resource: ResourceRecord) -> None:
        # `run` is the current environment's DeployRun, bound in the loop below
        # before the service can call this.
        if args.apply:
            inventory.put(run, resource)

    deployer = SERVICES[args.service](session)
    deployment_id = _new_deployment_id()
    mode = "apply" if args.apply else "dry-run"
    for environment in (e.strip() for e in args.env.split(",")):
        print(
            f"\n=== {action} {args.service} / {environment} "
            f"({mode}, deployment {deployment_id}) ==="
        )
        run = DeployRun(
            service=args.service,
            environment=environment,
            project_name=config.project_name(),
            aws_account_id=config.aws_account_id(),
            apply=args.apply,
            deployment_id=deployment_id,
            caller_arn=caller_arn,
            record=record,
            action=action,
            expires_at=expires_at,
        )
        yield deployer, run


def _cmd_deploy(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    for deployer, run in _runs(args, session, caller_arn, ACTION_DEPLOY):
        deployer.deploy(run)
    return 0


def _cmd_inventory_import(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    options = ImportOptions(
        origin=args.origin,
        reclassify=args.reclassify,
        only=frozenset(args.resource) if args.resource else None,
    )
    if options.origin == ORIGIN_CREATED:
        print(
            "note: resources classified as `created` can be deleted by a future undeploy; "
            "only use it for resources this tool deployed."
        )
    for deployer, run in _runs(args, session, caller_arn, ACTION_IMPORT):
        deployer.import_existing(run, options)
    return 0


def _cmd_inventory_init(args: argparse.Namespace, session: boto3.Session, _: str) -> int:
    inventory = _inventory(session)
    print(f"inventory table {inventory.table_name}: {inventory.ensure_table(args.apply)}")
    return 0


_LIST_COLUMNS = (
    ("environment", "ENV"),
    ("service", "SERVICE"),
    ("resource_type", "TYPE"),
    ("resource_name", "NAME"),
    ("origin", "ORIGIN"),
    ("status", "STATUS"),
    ("keep", "KEEP"),
    ("expires_at", "EXPIRES"),
    ("updated_at", "UPDATED"),
    ("last_deployment_id", "LAST DEPLOYMENT"),
)


def _cmd_inventory_list(args: argparse.Namespace, session: boto3.Session, _: str) -> int:
    inventory = _inventory(session)
    status = inventory.ensure_table(apply=False)
    if not inventory.ready:
        print(f"inventory table {inventory.table_name}: {status}; nothing deployed yet.")
        return 0

    items = inventory.list(
        environment=args.env,
        service=args.service,
        status=args.status,
        deployment_id=args.deployment_id,
    )
    if not items:
        print("No inventory entries match.")
        return 0
    _print_items(items)
    return 0


def _print_items(items: list[dict[str, Any]]) -> None:
    rows = [[str(i.get(attr, "")) for attr, _ in _LIST_COLUMNS] for i in items]
    headers = [h for _, h in _LIST_COLUMNS]
    widths = [max(len(c) for c in col) for col in zip(headers, *rows, strict=False)]
    for row in [headers, *rows]:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    print(f"\n{len(items)} resource(s).")
    for i in items:
        if i.get("keep_reason"):
            print(f"  keep {i['resource_name']}: {i['keep_reason']}")
        if i.get("last_error"):
            print(f"  ! {i['resource_name']}: last undeploy failed: {i['last_error']}")


def _ready_inventory(session: boto3.Session) -> InventoryStore | None:
    inventory = _inventory(session)
    status = inventory.ensure_table(apply=False)
    if not inventory.ready:
        print(f"inventory table {inventory.table_name}: {status}; nothing recorded yet.")
        return None
    return inventory


def _cmd_inventory_keep(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    inventory = _ready_inventory(session)
    if inventory is None:
        return 1
    keep = args.inventory_command == "keep"
    matches, missing = _match_targets(inventory, args)
    for item in matches:
        inventory.set_keep(item, keep, caller_arn, args.reason if keep else None)
        action = "kept" if keep else "no longer kept"
        print(f"  {item['environment']} {item['resource_type']} {item['resource_name']}: {action}")
    return 1 if missing else 0


def _match_targets(
    inventory: InventoryStore, args: argparse.Namespace
) -> tuple[list[dict[str, Any]], bool]:
    """Active items matching any of args.targets (name, resource id or creating deployment).

    Returns (matches, some_target_matched_nothing).
    """
    items = inventory.list(environment=args.env, service=args.service, status=STATUS_ACTIVE)
    matches: dict[str, dict[str, Any]] = {}
    missing = False
    for target in args.targets:
        found = [
            i
            for i in items
            if target in (i["resource_name"], i["resource_id"], i.get("created_deployment_id"))
        ]
        if not found:
            print(f"warning: no active inventory entry matches '{target}'")
            missing = True
        matches |= {i["resource_id"]: i for i in found}
    return list(matches.values()), missing


def _cmd_inventory_expire(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    inventory = _ready_inventory(session)
    if inventory is None:
        return 1
    when = None if args.clear else parse_expiry(args.expire_in or args.at)
    matches, missing = _match_targets(inventory, args)
    for item in matches:
        inventory.set_expiry(item, when, caller_arn)
        action = f"expires at {when}" if when else "expiry cleared"
        print(f"  {item['environment']} {item['resource_type']} {item['resource_name']}: {action}")
    return 1 if missing else 0


def _expired_items(inventory: InventoryStore, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Active items whose expiry passed.

    A dependent (e.g. an attachment) doesn't expire on its own while something `created` it
    depends on is still unexpired: extending a role/policy must not leave its attachment
    to be removed. It still goes when that dependency expires (undeploy pulls dependents).
    """
    now = format_timestamp(datetime.now(UTC))
    items = inventory.list(environment=args.env, service=args.service, status=STATUS_ACTIVE)
    by_id = {i["resource_id"]: i for i in items}

    def expired(item: dict[str, Any]) -> bool:
        # Same fixed-width UTC format on both sides, so string order is time order.
        return bool(item.get("expires_at")) and item["expires_at"] <= now

    def held_by_live_dependency(item: dict[str, Any]) -> bool:
        return any(
            (dep := by_id.get(d)) is not None
            and dep.get("origin") == ORIGIN_CREATED
            and not expired(dep)
            for d in item.get("depends_on") or []
        )

    return [i for i in items if expired(i) and not held_by_live_dependency(i)]


def _cmd_inventory_expired(args: argparse.Namespace, session: boto3.Session, _: str) -> int:
    inventory = _ready_inventory(session)
    if inventory is None:
        return 0
    items = _expired_items(inventory, args)
    if not items:
        print("Nothing has expired.")
        return 0
    _print_items(items)
    kept = sum(1 for i in items if i.get("keep"))
    if kept:
        print(f"\n{kept} of them are kept: undeploy will skip them until you unkeep them.")
    print("Remove them with: undeploy --expired (dry-run first, then --apply).")
    return 0


_FINDING_HINTS = {
    FINDING_UNDEFINED: "remove with: undeploy {service} --env {env} --resource '{name}'",
    FINDING_UNTRACKED: (
        "record with: inventory import {service} --env {env} --origin created|adopted "
        "--resource '{name}'"
    ),
}


def _cmd_inventory_audit(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    inventory = _ready_inventory(session)
    if inventory is None:
        return 0
    aws_account_id = config.aws_account_id()
    items = [i for i in inventory.list() if i.get("account_id") == aws_account_id]
    deployment_id = _new_deployment_id()
    mode = "apply (inventory only)" if args.apply else "report only"
    totals: Counter[str] = Counter()
    fixed = 0
    for service in [args.service] if args.service else sorted(SERVICES):
        deployer = SERVICES[service](session)
        environments = (
            [e.strip() for e in args.env.split(",")]
            if args.env
            else sorted(
                {i["environment"] for i in items if i["service"] == service}
                | set(deployer.environments())
            )
        )
        for environment in environments:
            print(f"\n=== audit {service} / {environment} ({mode}, deployment {deployment_id}) ===")
            run = DeployRun(
                service=service,
                environment=environment,
                project_name=config.project_name(),
                aws_account_id=aws_account_id,
                apply=args.apply,
                deployment_id=deployment_id,
                caller_arn=caller_arn,
                record=lambda _: None,
                action=ACTION_AUDIT,
            )
            scoped = [
                i for i in items if i["service"] == service and i["environment"] == environment
            ]
            findings = deployer.audit(run, scoped)
            if not findings:
                print("  in sync")
            for f in findings:
                totals[f.kind] += 1
                fixable = f.kind == FINDING_MISSING or f.record is not None
                suffix = ""
                if fixable and args.apply:
                    if f.kind == FINDING_MISSING:
                        inventory.mark_deleted(run, f.item, note="not found in AWS by audit")
                    else:
                        inventory.put(run, f.record)
                    fixed += 1
                    suffix = "  -> inventory fixed"
                elif fixable:
                    suffix = "  (fixable with --apply)"
                elif f.kind in _FINDING_HINTS:
                    hint = _FINDING_HINTS[f.kind].format(
                        service=service, env=environment, name=f.name
                    )
                    suffix = f"  ({hint})"
                print(f"  {f.kind:<15} {f.resource_type} {f.name}: {f.detail}{suffix}")

    summary = ", ".join(f"{n} {kind}" for kind, n in sorted(totals.items())) or "no findings"
    print(f"\nAudit: {summary}." + (f" {fixed} fixed in the inventory." if fixed else ""))
    return 1 if totals else 0


def _confirmed(args: argparse.Namespace, environments: set[str]) -> bool:
    expected = ",".join(sorted(environments))
    if args.confirm is not None:
        if args.confirm == expected:
            return True
        print(f"error: --confirm must be exactly '{expected}'.", file=sys.stderr)
        return False
    if not sys.stdin.isatty():
        print(f"error: no terminal to confirm; pass --confirm {expected}", file=sys.stderr)
        return False
    answer = input(f"\nThis deletes resources in AWS. Type '{expected}' to confirm: ")
    return answer.strip() == expected


def _cmd_undeploy(args: argparse.Namespace, session: boto3.Session, caller_arn: str) -> int:
    by_scope = not (args.expired or args.deployment_id)
    if args.expired:
        valid = not (args.deployment_id or args.resource)
    elif args.deployment_id:
        valid = not (args.service or args.env or args.resource)
    else:
        valid = bool(args.service and args.env)
    if not valid:
        print(
            "error: use exactly one of `undeploy <service> --env ... [--resource ...]`, "
            "`undeploy --deployment-id <id>` or `undeploy --expired [<service>] [--env ...]`.",
            file=sys.stderr,
        )
        return 2
    inventory = _ready_inventory(session)
    if inventory is None:
        return 0

    if args.deployment_id:
        targets = inventory.list(deployment_id=args.deployment_id, status=STATUS_ACTIVE)
        partitions = {(i["service"], i["environment"]) for i in targets}
    elif args.expired:
        targets = _expired_items(inventory, args)
        partitions = {(i["service"], i["environment"]) for i in targets}
    else:
        partitions = {(args.service, e.strip()) for e in args.env.split(",")}
    everything = [i for service, env in sorted(partitions) for i in inventory.list(env, service)]
    if by_scope:
        targets = [i for i in everything if i.get("status") == STATUS_ACTIVE]
        if args.resource:
            for name in sorted(set(args.resource) - {i["resource_name"] for i in targets}):
                print(f"warning: --resource {name} is not an active inventory entry, ignored")
            targets = [i for i in targets if i["resource_name"] in args.resource]

    plan = plan_undeploy(everything, {i["resource_id"] for i in targets}, config.aws_account_id())
    unknown = [i for i in plan.delete if i["service"] not in SERVICES]
    plan.skipped += [(i, f"service {i['service']} is not registered") for i in unknown]
    plan.delete = [i for i in plan.delete if i["service"] in SERVICES]

    for item, reason in plan.skipped:
        print(
            f"  skip {item['environment']} {item['resource_type']} "
            f"{item['resource_name']}: {reason}"
        )
    if not plan.delete:
        print("Nothing to delete.")
        return 0
    if args.apply and not _confirmed(args, {i["environment"] for i in plan.delete}):
        print("Aborted, nothing was deleted.")
        return 1

    results: Counter[str] = Counter()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in plan.delete:
        groups[(item["service"], item["environment"])].append(item)
    deployment_id = _new_deployment_id()
    mode = "apply" if args.apply else "dry-run"
    for (service, environment), items in sorted(groups.items()):
        print(f"\n=== undeploy {service} / {environment} ({mode}, deployment {deployment_id}) ===")
        run = DeployRun(
            service=service,
            environment=environment,
            project_name=config.project_name(),
            aws_account_id=config.aws_account_id(),
            apply=args.apply,
            deployment_id=deployment_id,
            caller_arn=caller_arn,
            record=lambda _: None,
            action=ACTION_UNDEPLOY,
        )

        def removal(item: dict[str, Any], error: str | None, run: DeployRun = run) -> None:
            results["blocked" if error else "removed"] += 1
            if not args.apply:
                return
            if error:
                inventory.mark_error(run, item, error)
            else:
                inventory.mark_deleted(run, item)

        SERVICES[service](session).undeploy(replace(run, record_removal=removal), items)

    verb = "removed" if args.apply else "would be removed"
    print(
        f"\n{results['removed']} {verb}, {results['blocked']} blocked, "
        f"{len(plan.skipped)} skipped."
    )
    return 1 if results["blocked"] else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    deploy = commands.add_parser("deploy", help="Create/update a service's resources")
    deploy.add_argument("service", choices=sorted(SERVICES), help="Which service to deploy")
    deploy.add_argument(
        "--env", required=True, help="Comma-separated environment(s): dev, mock, stg, prod"
    )
    deploy.add_argument(
        "--apply", action="store_true", help="Without this flag only the plan is shown (dry-run)"
    )
    deploy.add_argument(
        "--ttl",
        metavar="WHEN",
        help="Set an expiry on every recorded resource: 12h, 7d, 2w or a date (2026-10-01)",
    )
    deploy.set_defaults(handler=_cmd_deploy)

    inventory = commands.add_parser("inventory", help="Inspect the deployment inventory")
    inventory_commands = inventory.add_subparsers(dest="inventory_command", required=True)

    init = inventory_commands.add_parser(
        "init", help="Validate the inventory table, or create it (INVENTORY_TABLE_MODE=create)"
    )
    init.add_argument("--apply", action="store_true", help="Actually create the table if missing")
    init.set_defaults(handler=_cmd_inventory_init)

    listing = inventory_commands.add_parser("list", help="List recorded resources")
    listing.add_argument("--env", help="Only this environment")
    listing.add_argument("--service", choices=sorted(SERVICES), help="Only this service")
    listing.add_argument("--status", help="Only this status (active, deleted)")
    listing.add_argument("--deployment-id", help="Only resources created by this deployment")
    listing.set_defaults(handler=_cmd_inventory_list)

    importing = inventory_commands.add_parser(
        "import",
        help="Record resources that already exist in AWS (tags + inventory, content untouched)",
    )
    importing.add_argument("service", choices=sorted(SERVICES), help="Which service to import")
    importing.add_argument(
        "--env", required=True, help="Comma-separated environment(s): dev, mock, stg, prod"
    )
    importing.add_argument(
        "--origin",
        required=True,
        choices=ORIGINS,
        help="Origin for untagged resources: created (undeploy may delete them) or adopted",
    )
    importing.add_argument(
        "--reclassify",
        action="store_true",
        help="Also change the origin of resources already tagged with a different one",
    )
    importing.add_argument(
        "--resource",
        action="append",
        metavar="NAME",
        help="Only this role/policy name (repeatable); default: everything the service defines",
    )
    importing.add_argument(
        "--apply", action="store_true", help="Without this flag only the plan is shown (dry-run)"
    )
    importing.add_argument(
        "--ttl",
        metavar="WHEN",
        help="Set an expiry on every recorded resource: 12h, 7d, 2w or a date (2026-10-01)",
    )
    importing.set_defaults(handler=_cmd_inventory_import)

    expire = inventory_commands.add_parser("expire", help="Set or clear when resources expire")
    expire.add_argument(
        "targets",
        nargs="+",
        metavar="TARGET",
        help="Resource name, resource id (ARN) or the deployment id that created it",
    )
    expire.add_argument("--env", help="Only match in this environment")
    expire.add_argument("--service", choices=sorted(SERVICES), help="Only this service")
    when = expire.add_mutually_exclusive_group(required=True)
    when.add_argument("--in", dest="expire_in", metavar="DURATION", help="From now: 12h, 7d, 2w")
    when.add_argument("--at", metavar="DATE", help="UTC date/time: 2026-10-01[T18:00]")
    when.add_argument("--clear", action="store_true", help="Remove the expiry")
    expire.set_defaults(handler=_cmd_inventory_expire)

    expired = inventory_commands.add_parser(
        "expired", help="List active resources whose expiry has passed"
    )
    expired.add_argument("--env", help="Only this environment")
    expired.add_argument("--service", choices=sorted(SERVICES), help="Only this service")
    expired.set_defaults(handler=_cmd_inventory_expired)

    audit = inventory_commands.add_parser(
        "audit", help="Compare the inventory with AWS and the service definitions"
    )
    audit.add_argument("--service", choices=sorted(SERVICES), help="Only this service")
    audit.add_argument(
        "--env", help="Comma-separated environment(s); default: all known for the service"
    )
    audit.add_argument(
        "--apply",
        action="store_true",
        help="Fix the inventory (never AWS) for missing, orphan and origin-mismatch findings",
    )
    audit.set_defaults(handler=_cmd_inventory_audit)

    for name, text in (
        ("keep", "Protect resources from undeploy"),
        ("unkeep", "Remove the undeploy protection"),
    ):
        keeping = inventory_commands.add_parser(name, help=text)
        keeping.add_argument(
            "targets",
            nargs="+",
            metavar="TARGET",
            help="Resource name, resource id (ARN) or the deployment id that created it",
        )
        keeping.add_argument("--env", help="Only match in this environment")
        keeping.add_argument("--service", choices=sorted(SERVICES), help="Only this service")
        if name == "keep":
            keeping.add_argument("--reason", help="Why it's kept (shown by inventory list)")
        keeping.set_defaults(handler=_cmd_inventory_keep, reason=None)

    undeploy = commands.add_parser(
        "undeploy", help="Delete resources this tool created (per the inventory)"
    )
    undeploy.add_argument(
        "service", nargs="?", choices=sorted(SERVICES), help="Which service to undeploy"
    )
    undeploy.add_argument("--env", help="Comma-separated environment(s)")
    undeploy.add_argument(
        "--resource",
        action="append",
        metavar="NAME",
        help="Only this resource name (repeatable); its dependents are included",
    )
    undeploy.add_argument(
        "--deployment-id", help="Instead of service/env: what this deployment created"
    )
    undeploy.add_argument(
        "--apply", action="store_true", help="Without this flag only the plan is shown (dry-run)"
    )
    undeploy.add_argument(
        "--confirm",
        metavar="ENVS",
        help="Non-interactive confirmation: the affected environments, comma-separated, sorted",
    )
    undeploy.add_argument(
        "--expired",
        action="store_true",
        help="Instead of --resource/--deployment-id: everything whose expiry has passed "
        "(service/--env act as filters)",
    )
    undeploy.set_defaults(handler=_cmd_undeploy)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        session = boto3.Session(region_name=config.aws_region())
        caller_arn = _verified_caller_arn(session, config.aws_account_id())
        return args.handler(args, session, caller_arn)
    except (MissingEnvVarError, AccountMismatchError, InventoryError, ExpiryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
