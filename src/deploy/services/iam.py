"""Build IAM policies and roles from src/utils/data/iam/iam-<environment>/*.json.

Grouping convention (one iam-<environment>/ folder per environment):

- `tf-<environment>-N-*.json`  -> all attached to the same deploy role,
                                   `<PROJECT_NAME>-<environment>-deploy`.
- `*-boundary.json`            -> created/updated as a managed policy but
                                   NEVER attached to a role: it's the
                                   permissions boundary that terraform.tfvars
                                   references by ARN for module.iam (created by
                                   an administrator outside that state, see
                                   terraform/environment/dev/terraform.tfvars).
- Any other standalone file (front-publish-site.json, keycloak-admin-exec.json)
  -> its own role with just that one policy attached.
- `*-no-boundary.json`         -> alternate variant of another policy; skipped
                                   (not deployed automatically).

Policy/role names: a file's stem is used as-is if it already identifies the
environment (e.g. `tf-dev-1-state-s3`, `sf-onboarding-dev-boundary`); if not
(`front-publish-site`), the `-<environment>` suffix is added, because IAM
policy/role names are unique per account, not per folder, and that file name
repeats identically across iam-mock/ and iam-dev/.

AWS account id: the JSON files never hardcode it. They use the placeholder
`${AWS_ACCOUNT_ID}`, substituted at load time from the required AWS_ACCOUNT_ID
environment variable (see src/deploy/config.py).

Ownership: roles and policies the tool creates are tagged with origin=created; existing
ones it finds untagged are tagged origin=adopted (never deleted by a future undeploy).
Attachments can't be tagged: one counts as created if this run attached it, or if both
its role and policy were created by the tool. Everything is recorded in the inventory
(see src/deploy/inventory.py and docs/inventory.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .. import config
from ..resources import (
    FINDING_MISSING,
    FINDING_ORIGIN_MISMATCH,
    FINDING_ORPHAN,
    FINDING_UNDEFINED,
    FINDING_UNTRACKED,
    ORIGIN_ADOPTED,
    ORIGIN_CREATED,
    TAG_ENVIRONMENT,
    TAG_MANAGED_BY,
    TAG_ORIGIN,
    TAG_PROJECT,
    TAG_SERVICE,
    TOOL_NAME,
    DeployRun,
    Finding,
    ImportOptions,
    ResourceRecord,
    format_timestamp,
    tags_to_dict,
)
from .base import ServiceDeployer

DATA_ROOT = Path(__file__).resolve().parents[2] / "utils" / "data" / "iam"


def _trust_policy(aws_account_id: str, trusted_principal_user: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowConfiguredPrincipalToAssume",
                "Effect": "Allow",
                "Principal": {
                    "AWS": f"arn:aws:iam::{aws_account_id}:user/{trusted_principal_user}"
                },
                "Action": "sts:AssumeRole",
            }
        ],
    }


def _load_policy_document(path: Path, aws_account_id: str) -> dict:
    return json.loads(path.read_text().replace("${AWS_ACCOUNT_ID}", aws_account_id))


def _unique_name(stem: str, environment: str) -> str:
    tokens = stem.split("-")
    if environment in tokens:
        return stem
    return f"{stem}-{environment}"


@dataclass(frozen=True)
class PolicyFile:
    path: Path
    stem: str
    is_boundary: bool


@dataclass(frozen=True)
class RoleGroup:
    role_name: str
    policies: list[PolicyFile]


def _discover(environment: str) -> tuple[list[PolicyFile], list[Path]]:
    env_dir = DATA_ROOT / f"iam-{environment}"
    if not env_dir.is_dir():
        raise FileNotFoundError(f"No policy folder for environment '{environment}': {env_dir}")

    kept: list[PolicyFile] = []
    skipped: list[Path] = []
    for path in sorted(env_dir.glob("*.json")):
        stem = path.stem
        if stem.endswith("-no-boundary"):
            skipped.append(path)
            continue
        kept.append(PolicyFile(path=path, stem=stem, is_boundary=stem.endswith("-boundary")))
    return kept, skipped


def _group_roles(files: list[PolicyFile], environment: str, project_name: str) -> list[RoleGroup]:
    deploy_prefix = f"tf-{environment}-"
    deploy_files = [f for f in files if f.stem.startswith(deploy_prefix)]
    standalone_files = [
        f for f in files if not f.stem.startswith(deploy_prefix) and not f.is_boundary
    ]

    groups = []
    if deploy_files:
        groups.append(
            RoleGroup(role_name=f"{project_name}-{environment}-deploy", policies=deploy_files)
        )
    for f in standalone_files:
        groups.append(RoleGroup(role_name=_unique_name(f.stem, environment), policies=[f]))
    return groups


def _policy_arn(aws_account_id: str, name: str) -> str:
    return f"arn:aws:iam::{aws_account_id}:policy/{name}"


def _role_arn(aws_account_id: str, name: str) -> str:
    return f"arn:aws:iam::{aws_account_id}:role/{name}"


def _prune_oldest_version_if_needed(iam, policy_arn: str) -> None:
    # A managed policy only keeps 5 versions; free one up before creating a sixth.
    versions = iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]
    if len(versions) < 5:
        return
    oldest = min((v for v in versions if not v["IsDefaultVersion"]), key=lambda v: v["CreateDate"])
    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])


def _claim_ownership(current_tags, run: DeployRun, tag) -> tuple[str, str]:
    """Return (origin, note) for an existing resource, adopting it if it has no origin tag."""
    origin = tags_to_dict(current_tags).get(TAG_ORIGIN)
    if origin:
        return origin, ""
    if not run.apply:
        return ORIGIN_ADOPTED, "untagged, would be adopted; "
    tag(run.tags(ORIGIN_ADOPTED))
    return ORIGIN_ADOPTED, "adopted (ownership tags added); "


def _ensure_policy(
    iam, run: DeployRun, name: str, document: dict, description: str
) -> tuple[str, str, str]:
    """Return (arn, origin, status)."""
    arn = _policy_arn(run.aws_account_id, name)
    try:
        current = iam.get_policy(PolicyArn=arn)["Policy"]
    except iam.exceptions.NoSuchEntityException:
        if not run.apply:
            return arn, ORIGIN_CREATED, "does not exist, would be created"
        created = iam.create_policy(
            PolicyName=name,
            PolicyDocument=json.dumps(document),
            Description=description,
            Tags=run.tags(ORIGIN_CREATED),
        )["Policy"]
        return created["Arn"], ORIGIN_CREATED, "created"

    origin, note = _claim_ownership(
        current.get("Tags"), run, lambda tags: iam.tag_policy(PolicyArn=arn, Tags=tags)
    )
    current_doc = iam.get_policy_version(PolicyArn=arn, VersionId=current["DefaultVersionId"])[
        "PolicyVersion"
    ]["Document"]
    if current_doc == document:
        return arn, origin, f"{note}unchanged"
    if not run.apply:
        return arn, origin, f"{note}exists, content differs: would be updated"

    _prune_oldest_version_if_needed(iam, arn)
    iam.create_policy_version(PolicyArn=arn, PolicyDocument=json.dumps(document), SetAsDefault=True)
    return arn, origin, f"{note}updated"


def _ensure_role(
    iam, run: DeployRun, name: str, trust_document: dict, description: str
) -> tuple[str, str, str]:
    """Return (arn, origin, status)."""
    arn = _role_arn(run.aws_account_id, name)
    try:
        current = iam.get_role(RoleName=name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        if not run.apply:
            return arn, ORIGIN_CREATED, "does not exist, would be created"
        created = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_document),
            Description=description,
            Tags=run.tags(ORIGIN_CREATED),
        )["Role"]
        return created["Arn"], ORIGIN_CREATED, "created"

    origin, note = _claim_ownership(
        current.get("Tags"), run, lambda tags: iam.tag_role(RoleName=name, Tags=tags)
    )
    if current["AssumeRolePolicyDocument"] == trust_document:
        return current["Arn"], origin, f"{note}unchanged"
    if not run.apply:
        return current["Arn"], origin, f"{note}exists, trust policy differs: would be updated"

    iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust_document))
    return current["Arn"], origin, f"{note}trust policy updated"


def _ensure_attached(
    iam, run: DeployRun, role_name: str, policy_arn: str, parents_created: bool
) -> tuple[str, str]:
    """Return (origin, status)."""
    try:
        attached = {
            p["PolicyArn"]
            for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        }
    except iam.exceptions.NoSuchEntityException:
        attached = set()

    if policy_arn in attached:
        return (ORIGIN_CREATED if parents_created else ORIGIN_ADOPTED), "already attached"
    if not run.apply:
        return ORIGIN_CREATED, "would be attached"
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return ORIGIN_CREATED, "attached"


def _classify_existing(
    current_tags, run: DeployRun, options: ImportOptions, tag
) -> tuple[str, str]:
    """Return (origin, note) for an import, re-tagging only as `options` allows."""
    current = tags_to_dict(current_tags).get(TAG_ORIGIN)
    verb = "tagged" if run.apply else "would be tagged"
    if current is None:
        if run.apply:
            tag(run.tags(options.origin))
        return options.origin, f"untagged, {verb} origin={options.origin}"
    if current == options.origin:
        return current, f"already origin={current}"
    if not options.reclassify:
        return current, f"keeps origin={current} (pass --reclassify to set {options.origin})"
    if run.apply:
        # Only the origin changes; the other ownership tags keep their original values.
        tag([{"Key": TAG_ORIGIN, "Value": options.origin}])
    verb = "reclassified" if run.apply else "would be reclassified"
    return options.origin, f"{verb} {current} -> {options.origin}"


class _Blocked(Exception):
    """Undeploy must not delete this resource; the message says why."""


_TYPE_ATTACHMENT = "iam_role_policy_attachment"
_TYPE_ROLE = "iam_role"
_TYPE_POLICY = "iam_policy"
# Attachments first; roles before policies, so deleting a role (which detaches everything
# left on it) frees the policies it used.
_UNDEPLOY_ORDER = {_TYPE_ATTACHMENT: 0, _TYPE_ROLE: 1, _TYPE_POLICY: 2}


def _name_from_arn(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def _require_created_tag(tags, kind: str) -> None:
    origin = tags_to_dict(tags).get(TAG_ORIGIN)
    if origin != ORIGIN_CREATED:
        raise _Blocked(f"AWS origin tag on the {kind} is {origin or 'missing'}, not created")


class IamService(ServiceDeployer):
    name = "iam"

    def __init__(self, session: boto3.Session) -> None:
        self._trusted_principal_user = config.trusted_principal_user()
        self._iam = session.client("iam")

    def deploy(self, run: DeployRun) -> None:
        environment, project_name = run.environment, run.project_name
        files, skipped = _discover(environment)
        for path in skipped:
            print(f"  (skipped, alternate variant not deployed automatically: {path.name})")

        for f in (f for f in files if f.is_boundary):
            document = _load_policy_document(f.path, run.aws_account_id)
            description = (
                f"{project_name} {environment}: permissions boundary ({f.path.name}); "
                "not attached to any role"
            )
            arn, origin, status = _ensure_policy(self._iam, run, f.stem, document, description)
            print(f"  policy {f.stem}: {status}  [{origin}; boundary, not attached to a role]")
            run.record(ResourceRecord(arn, "iam_policy", f.stem, origin))

        trust_document = _trust_policy(run.aws_account_id, self._trusted_principal_user)
        for group in _group_roles(files, environment, project_name):
            role_description = (
                f"{project_name} {environment}: generated from "
                f"src/utils/data/iam/iam-{environment}/"
            )
            role_arn, role_origin, role_status = _ensure_role(
                self._iam, run, group.role_name, trust_document, role_description
            )
            print(f"  role {group.role_name}: {role_status}  [{role_origin}]")
            run.record(ResourceRecord(role_arn, "iam_role", group.role_name, role_origin))

            for f in group.policies:
                document = _load_policy_document(f.path, run.aws_account_id)
                policy_name = _unique_name(f.stem, environment)
                policy_description = (
                    f"{project_name} {environment}: {f.path.name}, attached to {group.role_name}"
                )
                policy_arn, policy_origin, policy_status = _ensure_policy(
                    self._iam, run, policy_name, document, policy_description
                )
                print(f"    policy {policy_name}: {policy_status}  [{policy_origin}]")
                run.record(ResourceRecord(policy_arn, "iam_policy", policy_name, policy_origin))

                parents_created = role_origin == policy_origin == ORIGIN_CREATED
                attach_origin, attach_status = _ensure_attached(
                    self._iam, run, group.role_name, policy_arn, parents_created
                )
                print(f"    attach -> {group.role_name}: {attach_status}  [{attach_origin}]")
                run.record(
                    ResourceRecord(
                        resource_id=f"attachment:{role_arn}|{policy_arn}",
                        resource_type="iam_role_policy_attachment",
                        name=f"{group.role_name} <- {policy_name}",
                        origin=attach_origin,
                        depends_on=(role_arn, policy_arn),
                    )
                )

    def _import_policy(
        self, run: DeployRun, options: ImportOptions, name: str, indent: str = "  "
    ) -> tuple[str, bool, str | None]:
        """Return (arn, exists, origin). Unselected policies are only read, never touched."""
        arn = _policy_arn(run.aws_account_id, name)
        try:
            current = self._iam.get_policy(PolicyArn=arn)["Policy"]
        except self._iam.exceptions.NoSuchEntityException:
            if options.selects(name):
                print(f"{indent}policy {name}: not deployed, skipped")
            return arn, False, None
        origin = tags_to_dict(current.get("Tags")).get(TAG_ORIGIN)
        if not options.selects(name):
            return arn, True, origin

        origin, note = _classify_existing(
            current.get("Tags"),
            run,
            options,
            lambda tags: self._iam.tag_policy(PolicyArn=arn, Tags=tags),
        )
        print(f"{indent}policy {name}: {note}")
        run.record(
            ResourceRecord(
                arn, "iam_policy", name, origin, created_at=format_timestamp(current["CreateDate"])
            )
        )
        return arn, True, origin

    def _import_role(
        self, run: DeployRun, options: ImportOptions, name: str
    ) -> tuple[str, bool, str | None]:
        """Return (arn, exists, origin). Unselected roles are only read, never touched."""
        arn = _role_arn(run.aws_account_id, name)
        try:
            current = self._iam.get_role(RoleName=name)["Role"]
        except self._iam.exceptions.NoSuchEntityException:
            if options.selects(name):
                print(f"  role {name}: not deployed, skipped")
            return arn, False, None
        origin = tags_to_dict(current.get("Tags")).get(TAG_ORIGIN)
        if not options.selects(name):
            return current["Arn"], True, origin

        origin, note = _classify_existing(
            current.get("Tags"),
            run,
            options,
            lambda tags: self._iam.tag_role(RoleName=name, Tags=tags),
        )
        print(f"  role {name}: {note}")
        run.record(
            ResourceRecord(
                current["Arn"],
                "iam_role",
                name,
                origin,
                created_at=format_timestamp(current["CreateDate"]),
            )
        )
        return current["Arn"], True, origin

    def import_existing(self, run: DeployRun, options: ImportOptions) -> None:
        environment = run.environment
        files, _ = _discover(environment)
        groups = _group_roles(files, environment, run.project_name)

        known = {f.stem for f in files if f.is_boundary}
        for group in groups:
            known.add(group.role_name)
            known.update(_unique_name(f.stem, environment) for f in group.policies)
        for name in sorted((options.only or set()) - known):
            print(f"  warning: --resource {name} is not defined for {environment}, ignored")

        for f in (f for f in files if f.is_boundary):
            self._import_policy(run, options, f.stem)

        for group in groups:
            role_arn, role_exists, role_origin = self._import_role(run, options, group.role_name)
            attached: set[str] = set()
            if role_exists:
                attached = {
                    p["PolicyArn"]
                    for p in self._iam.list_attached_role_policies(RoleName=group.role_name)[
                        "AttachedPolicies"
                    ]
                }

            for f in group.policies:
                policy_name = _unique_name(f.stem, environment)
                policy_arn, _, policy_origin = self._import_policy(
                    run, options, policy_name, indent="    "
                )
                selected = options.selects(group.role_name) or options.selects(policy_name)
                if policy_arn not in attached or not selected:
                    continue
                # Same rule as deploy: an attachment is `created` only if both ends are.
                attach_origin = (
                    ORIGIN_CREATED
                    if role_origin == policy_origin == ORIGIN_CREATED
                    else ORIGIN_ADOPTED
                )
                status = "recorded" if run.apply else "would be recorded"
                print(f"    attach -> {group.role_name}: {status}  [{attach_origin}]")
                run.record(
                    ResourceRecord(
                        resource_id=f"attachment:{role_arn}|{policy_arn}",
                        resource_type="iam_role_policy_attachment",
                        name=f"{group.role_name} <- {policy_name}",
                        origin=attach_origin,
                        depends_on=(role_arn, policy_arn),
                    )
                )

    def undeploy(self, run: DeployRun, items: list[dict[str, Any]]) -> None:
        planned_roles = {i["resource_name"] for i in items if i["resource_type"] == _TYPE_ROLE}
        planned_attachments = {
            (_name_from_arn(i["depends_on"][0]), i["depends_on"][1])
            for i in items
            if i["resource_type"] == _TYPE_ATTACHMENT
        }
        for item in sorted(items, key=lambda i: _UNDEPLOY_ORDER.get(i["resource_type"], 99)):
            kind = item["resource_type"]
            try:
                if kind == _TYPE_ATTACHMENT:
                    status = self._remove_attachment(run, item)
                elif kind == _TYPE_ROLE:
                    status = self._remove_role(run, item, planned_attachments)
                elif kind == _TYPE_POLICY:
                    status = self._remove_policy(run, item, planned_roles, planned_attachments)
                else:
                    raise _Blocked(f"unknown resource type {kind}")
            except (_Blocked, ClientError) as e:
                print(f"  {kind} {item['resource_name']}: BLOCKED - {e}")
                run.record_removal(item, str(e))
                continue
            print(f"  {kind} {item['resource_name']}: {status}")
            run.record_removal(item, None)

    def _remove_attachment(self, run: DeployRun, item: dict[str, Any]) -> str:
        role_arn, policy_arn = item["depends_on"]
        if not run.apply:
            return "would be detached"
        try:
            self._iam.detach_role_policy(RoleName=_name_from_arn(role_arn), PolicyArn=policy_arn)
        except self._iam.exceptions.NoSuchEntityException:
            return "already detached"
        return "detached"

    def _remove_role(
        self, run: DeployRun, item: dict[str, Any], planned_attachments: set[tuple[str, str]]
    ) -> str:
        name = item["resource_name"]
        try:
            role = self._iam.get_role(RoleName=name)["Role"]
        except self._iam.exceptions.NoSuchEntityException:
            return "already gone"
        _require_created_tag(role.get("Tags"), "role")

        # The role is ours and is going away, so whatever is still on it goes too.
        attached = [
            p["PolicyArn"]
            for page in self._iam.get_paginator("list_attached_role_policies").paginate(
                RoleName=name
            )
            for p in page["AttachedPolicies"]
        ]
        inline = [
            p
            for page in self._iam.get_paginator("list_role_policies").paginate(RoleName=name)
            for p in page["PolicyNames"]
        ]
        profiles = [
            p["InstanceProfileName"]
            for page in self._iam.get_paginator("list_instance_profiles_for_role").paginate(
                RoleName=name
            )
            for p in page["InstanceProfiles"]
        ]
        if not run.apply:
            extra = [a for a in attached if (name, a) not in planned_attachments]
            notes = [f"detach {_name_from_arn(a)}" for a in extra]
            notes += [f"delete inline {p}" for p in inline]
            notes += [f"leave instance profile {p}" for p in profiles]
            return "would be deleted" + (f" (also: {', '.join(notes)})" if notes else "")

        for policy_arn in attached:
            self._iam.detach_role_policy(RoleName=name, PolicyArn=policy_arn)
        for policy_name in inline:
            self._iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        for profile in profiles:
            self._iam.remove_role_from_instance_profile(InstanceProfileName=profile, RoleName=name)
        self._iam.delete_role(RoleName=name)
        return "deleted"

    def _remove_policy(
        self,
        run: DeployRun,
        item: dict[str, Any],
        planned_roles: set[str],
        planned_attachments: set[tuple[str, str]],
    ) -> str:
        arn = item["resource_id"]
        try:
            policy = self._iam.get_policy(PolicyArn=arn)["Policy"]
        except self._iam.exceptions.NoSuchEntityException:
            return "already gone"
        _require_created_tag(policy.get("Tags"), "policy")

        # No PolicyUsageFilter: covers permissions policies and permissions boundaries.
        users, groups, roles = [], [], []
        for page in self._iam.get_paginator("list_entities_for_policy").paginate(PolicyArn=arn):
            users += [u["UserName"] for u in page["PolicyUsers"]]
            groups += [g["GroupName"] for g in page["PolicyGroups"]]
            roles += [r["RoleName"] for r in page["PolicyRoles"]]
        if not run.apply:
            # Nothing has been removed yet: ignore what earlier steps of this plan remove.
            roles = [
                r for r in roles if r not in planned_roles and (r, arn) not in planned_attachments
            ]
        users_in_use = [f"user {u}" for u in users] + [f"group {g}" for g in groups]
        in_use = [f"role {r}" for r in roles] + users_in_use
        if in_use:
            raise _Blocked(
                f"still used by {', '.join(in_use)} (outside this undeploy); "
                "detach it there first or keep it"
            )

        if not run.apply:
            return "would be deleted"
        for version in self._iam.list_policy_versions(PolicyArn=arn)["Versions"]:
            if not version["IsDefaultVersion"]:
                self._iam.delete_policy_version(PolicyArn=arn, VersionId=version["VersionId"])
        self._iam.delete_policy(PolicyArn=arn)
        return "deleted"

    def environments(self) -> list[str]:
        return sorted(p.name.removeprefix("iam-") for p in DATA_ROOT.glob("iam-*") if p.is_dir())

    def _owned_by_run(self, tags, run: DeployRun) -> bool:
        values = tags_to_dict(tags)
        return (
            values.get(TAG_MANAGED_BY) == TOOL_NAME
            and values.get(TAG_PROJECT) == run.project_name
            and values.get(TAG_ENVIRONMENT) == run.environment
            and values.get(TAG_SERVICE) == run.service
        )

    def _tagged_resources(self, run: DeployRun) -> tuple[dict, dict]:
        """Roles and customer-managed policies tagged for this project/env, by ARN.

        IAM's list calls don't return tags, so this costs one extra call per role and per
        policy in the account.
        """
        roles, policies = {}, {}
        for page in self._iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                tags = self._iam.list_role_tags(RoleName=role["RoleName"])["Tags"]
                if self._owned_by_run(tags, run):
                    roles[role["Arn"]] = role | {"Tags": tags}
        for page in self._iam.get_paginator("list_policies").paginate(Scope="Local"):
            for policy in page["Policies"]:
                tags = self._iam.list_policy_tags(PolicyArn=policy["Arn"])["Tags"]
                if self._owned_by_run(tags, run):
                    policies[policy["Arn"]] = policy | {"Tags": tags}
        return roles, policies

    def _get(self, kind: str, name_or_arn: str) -> dict | None:
        try:
            if kind == _TYPE_ROLE:
                return self._iam.get_role(RoleName=name_or_arn)["Role"]
            return self._iam.get_policy(PolicyArn=name_or_arn)["Policy"]
        except self._iam.exceptions.NoSuchEntityException:
            return None

    def audit(self, run: DeployRun, items: list[dict[str, Any]]) -> list[Finding]:
        environment = run.environment
        try:
            files, _ = _discover(environment)
        except FileNotFoundError:
            files = []
        groups = _group_roles(files, environment, run.project_name)
        defined_roles = {g.role_name for g in groups}
        defined_policies = {f.stem for f in files if f.is_boundary} | {
            _unique_name(f.stem, environment) for g in groups for f in g.policies
        }
        defined_attachments = {
            (g.role_name, _unique_name(f.stem, environment)) for g in groups for f in g.policies
        }

        active = {i["resource_id"]: i for i in items if i.get("status") == "active"}
        tagged_roles, tagged_policies = self._tagged_resources(run)
        findings: list[Finding] = []

        attached_cache: dict[str, set[str] | None] = {}

        def attached(role_name: str) -> set[str] | None:
            if role_name not in attached_cache:
                try:
                    attached_cache[role_name] = {
                        p["PolicyArn"]
                        for page in self._iam.get_paginator("list_attached_role_policies").paginate(
                            RoleName=role_name
                        )
                        for p in page["AttachedPolicies"]
                    }
                except self._iam.exceptions.NoSuchEntityException:
                    attached_cache[role_name] = None
            return attached_cache[role_name]

        # Inventory -> AWS / source.
        live: dict[str, dict] = {}
        for rid, item in active.items():
            kind, name = item["resource_type"], item["resource_name"]
            if kind == _TYPE_ATTACHMENT:
                role_arn, policy_arn = item["depends_on"]
                current = attached(_name_from_arn(role_arn))
                if current is None or policy_arn not in current:
                    findings.append(
                        Finding(FINDING_MISSING, kind, name, "not attached in AWS", item)
                    )
                elif (
                    _name_from_arn(role_arn),
                    _name_from_arn(policy_arn),
                ) not in defined_attachments:
                    findings.append(
                        Finding(
                            FINDING_UNDEFINED, kind, name, "no longer defined in the JSON", item
                        )
                    )
                continue
            if kind not in (_TYPE_ROLE, _TYPE_POLICY):
                continue
            resource = self._get(kind, name if kind == _TYPE_ROLE else rid)
            if resource is None:
                findings.append(Finding(FINDING_MISSING, kind, name, "not found in AWS", item))
                continue
            live[rid] = resource
            tag_origin = tags_to_dict(resource.get("Tags")).get(TAG_ORIGIN)
            if tag_origin is None:
                findings.append(
                    Finding(
                        FINDING_ORIGIN_MISMATCH,
                        kind,
                        name,
                        f"inventory says {item['origin']} but the AWS resource has no origin tag "
                        f"(undeploy will block it); fix with: inventory import iam --env "
                        f"{environment} --origin {item['origin']} --resource '{name}'",
                        item,
                    )
                )
            elif tag_origin != item["origin"]:
                findings.append(
                    Finding(
                        FINDING_ORIGIN_MISMATCH,
                        kind,
                        name,
                        f"inventory says {item['origin']}, AWS tag says {tag_origin}",
                        item,
                        ResourceRecord(
                            rid,
                            kind,
                            name,
                            tag_origin,
                            created_at=format_timestamp(resource["CreateDate"]),
                        ),
                    )
                )
            defined = defined_roles if kind == _TYPE_ROLE else defined_policies
            if name not in defined:
                findings.append(
                    Finding(FINDING_UNDEFINED, kind, name, "no longer defined in the JSON", item)
                )

        # AWS -> inventory: tagged but not active in the inventory.
        origins: dict[str, str | None] = {
            arn: tags_to_dict(r["Tags"]).get(TAG_ORIGIN)
            for arn, r in (tagged_roles | tagged_policies).items()
        }
        origins |= {rid: i["origin"] for rid, i in active.items() if rid not in origins}
        for kind, tagged in ((_TYPE_ROLE, tagged_roles), (_TYPE_POLICY, tagged_policies)):
            for arn, resource in tagged.items():
                if arn in active:
                    continue
                name = resource["RoleName"] if kind == _TYPE_ROLE else resource["PolicyName"]
                origin = origins[arn] or ORIGIN_ADOPTED
                findings.append(
                    Finding(
                        FINDING_ORPHAN,
                        kind,
                        name,
                        f"tagged origin={origin} but not in the inventory",
                        record=ResourceRecord(
                            arn,
                            kind,
                            name,
                            origin,
                            created_at=format_timestamp(resource["CreateDate"]),
                        ),
                    )
                )

        our_policies = set(tagged_policies) | {
            rid for rid, i in active.items() if i["resource_type"] == _TYPE_POLICY
        }
        our_roles = {arn: r["RoleName"] for arn, r in tagged_roles.items()} | {
            rid: i["resource_name"]
            for rid, i in active.items()
            if i["resource_type"] == _TYPE_ROLE and rid in live
        }
        for role_arn, role_name in sorted(our_roles.items()):
            for policy_arn in sorted((attached(role_name) or set()) & our_policies):
                attachment_id = f"attachment:{role_arn}|{policy_arn}"
                if attachment_id in active:
                    continue
                origin = (
                    ORIGIN_CREATED
                    if origins.get(role_arn) == origins.get(policy_arn) == ORIGIN_CREATED
                    else ORIGIN_ADOPTED
                )
                name = f"{role_name} <- {_name_from_arn(policy_arn)}"
                findings.append(
                    Finding(
                        FINDING_ORPHAN,
                        _TYPE_ATTACHMENT,
                        name,
                        "attached in AWS but not in the inventory",
                        record=ResourceRecord(
                            attachment_id,
                            _TYPE_ATTACHMENT,
                            name,
                            origin,
                            depends_on=(role_arn, policy_arn),
                        ),
                    )
                )

        # Source -> AWS: defined, deployed, but neither tagged nor recorded.
        known = set(active) | set(tagged_roles) | set(tagged_policies)
        for kind, names in ((_TYPE_ROLE, defined_roles), (_TYPE_POLICY, defined_policies)):
            for name in sorted(names):
                arn = (_role_arn if kind == _TYPE_ROLE else _policy_arn)(run.aws_account_id, name)
                if arn in known:
                    continue
                resource = self._get(kind, name if kind == _TYPE_ROLE else arn)
                if resource is not None and not tags_to_dict(resource.get("Tags")).get(TAG_ORIGIN):
                    findings.append(
                        Finding(
                            FINDING_UNTRACKED,
                            kind,
                            name,
                            "exists in AWS, untagged and not in the inventory",
                        )
                    )
        return findings
