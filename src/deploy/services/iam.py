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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import boto3

from .. import config
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


def _prune_oldest_version_if_needed(iam, policy_arn: str) -> None:
    # A managed policy only keeps 5 versions; free one up before creating a sixth.
    versions = iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]
    if len(versions) < 5:
        return
    oldest = min((v for v in versions if not v["IsDefaultVersion"]), key=lambda v: v["CreateDate"])
    iam.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])


def _ensure_policy(
    iam, aws_account_id: str, name: str, document: dict, description: str, apply: bool
) -> tuple[str, str]:
    arn = _policy_arn(aws_account_id, name)
    try:
        current = iam.get_policy(PolicyArn=arn)["Policy"]
    except iam.exceptions.NoSuchEntityException:
        if not apply:
            return arn, "does not exist, would be created"
        created = iam.create_policy(
            PolicyName=name,
            PolicyDocument=json.dumps(document),
            Description=description,
        )["Policy"]
        return created["Arn"], "created"

    current_doc = iam.get_policy_version(PolicyArn=arn, VersionId=current["DefaultVersionId"])[
        "PolicyVersion"
    ]["Document"]
    if current_doc == document:
        return arn, "unchanged"
    if not apply:
        return arn, "exists, content differs: would be updated"

    _prune_oldest_version_if_needed(iam, arn)
    iam.create_policy_version(PolicyArn=arn, PolicyDocument=json.dumps(document), SetAsDefault=True)
    return arn, "updated"


def _ensure_role(iam, name: str, trust_document: dict, description: str, apply: bool) -> str:
    try:
        current = iam.get_role(RoleName=name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        if not apply:
            return "does not exist, would be created"
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_document),
            Description=description,
        )
        return "created"

    if current["AssumeRolePolicyDocument"] == trust_document:
        return "unchanged"
    if not apply:
        return "exists, trust policy differs: would be updated"

    iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust_document))
    return "trust policy updated"


def _ensure_attached(iam, role_name: str, policy_arn: str, apply: bool) -> str:
    try:
        attached = {
            p["PolicyArn"]
            for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        }
    except iam.exceptions.NoSuchEntityException:
        attached = set()

    if policy_arn in attached:
        return "already attached"
    if not apply:
        return "would be attached"
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return "attached"


class IamService(ServiceDeployer):
    name = "iam"

    def __init__(self, iam_client=None, sts_client=None):
        self._aws_account_id = config.aws_account_id()
        self._aws_region = config.aws_region()
        self._project_name = config.project_name()
        self._trusted_principal_user = config.trusted_principal_user()
        self._iam = iam_client or boto3.client("iam", region_name=self._aws_region)
        self._sts = sts_client or boto3.client("sts", region_name=self._aws_region)

    def _verified_aws_account_id(self) -> str:
        # Fail fast if AWS_ACCOUNT_ID doesn't match who we're actually authenticated as,
        # instead of silently creating account-011221923990-shaped ARNs in the wrong account.
        caller_account = self._sts.get_caller_identity()["Account"]
        if caller_account != self._aws_account_id:
            raise RuntimeError(
                f"AWS_ACCOUNT_ID={self._aws_account_id} does not match the authenticated AWS "
                f"account ({caller_account}); refusing to deploy to avoid touching the "
                "wrong account."
            )
        return self._aws_account_id

    def deploy(self, environment: str, apply: bool) -> None:
        aws_account_id = self._verified_aws_account_id()
        project_name = self._project_name
        files, skipped = _discover(environment)
        for path in skipped:
            print(f"  (skipped, alternate variant not deployed automatically: {path.name})")

        for f in (f for f in files if f.is_boundary):
            document = _load_policy_document(f.path, aws_account_id)
            description = (
                f"{project_name} {environment}: permissions boundary ({f.path.name}); "
                "not attached to any role"
            )
            _, status = _ensure_policy(
                self._iam, aws_account_id, f.stem, document, description, apply
            )
            print(f"  policy {f.stem}: {status}  [boundary, not attached to a role]")

        for group in _group_roles(files, environment, project_name):
            role_description = (
                f"{project_name} {environment}: generated from "
                f"src/utils/data/iam/iam-{environment}/"
            )
            role_status = _ensure_role(
                self._iam,
                group.role_name,
                _trust_policy(aws_account_id, self._trusted_principal_user),
                role_description,
                apply,
            )
            print(f"  role {group.role_name}: {role_status}")

            for f in group.policies:
                document = _load_policy_document(f.path, aws_account_id)
                policy_name = _unique_name(f.stem, environment)
                policy_description = (
                    f"{project_name} {environment}: {f.path.name}, attached to {group.role_name}"
                )
                policy_arn, policy_status = _ensure_policy(
                    self._iam, aws_account_id, policy_name, document, policy_description, apply
                )
                print(f"    policy {policy_name}: {policy_status}")

                attach_status = _ensure_attached(self._iam, group.role_name, policy_arn, apply)
                print(f"    attach -> {group.role_name}: {attach_status}")
