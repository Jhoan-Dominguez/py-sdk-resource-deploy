# Notes, decisions and known limitations

Operational notes collected while building the tool: what has and hasn't been verified,
what to do before using it for real, the design decisions (and where they differ from the
original plan), breaking changes, and known limitations. Keep this file up to date with
every change.

## Verification status

- **Every phase has been tested only against mocked AWS ([moto](https://github.com/getmoto/moto)),
  never against the real account.** The IAM, STS and DynamoDB behavior the tool relies on
  matches the AWS API documentation, but the first real runs should always be dry-runs
  first.
- There's no automated test suite in the repo yet (`pyproject.toml` points pytest at
  `python_tests/unit`, which doesn't exist). The mocked scenarios were run by calling
  `deploy.cli.main([...])` inside `moto.mock_aws()` from a throwaway virtualenv with
  `moto[iam,dynamodb,sts]` installed. moto is **not** in `requirements.txt`.
- Scenarios covered: deploy dry-run/apply/re-apply, adoption of untagged resources,
  existing tables with other key names, missing table in `existing` mode, import (dry-run,
  selection, reclassification, content untouched), undeploy (keep and its propagation,
  adopted, policy used by a foreign role, AWS tag mismatch, confirmation, `--resource`,
  `--deployment-id` across environments, idempotency, redeploy after delete), audit (every
  finding kind, `--apply`), and expiry (`--ttl`, `expire`, `expired`, `undeploy --expired`
  with keep).

## Before the first real use

1. Add `INVENTORY_TABLE` to `.env` (and `INVENTORY_TABLE_MODE=existing` if you'll use an
   existing table). See `.env.template`.
2. Grant the identity that runs the tool the [permissions](#permissions) below.
3. If the old tool (before the inventory existed) already deployed resources, **run
   `inventory import` first**. Until you do, `undeploy` doesn't know about them and
   won't touch them, and the next `deploy` would mark them `adopted`.
   ```
   python3 src/main.py inventory import iam --env dev --origin created          # review
   python3 src/main.py inventory import iam --env dev --origin created --apply
   ```
   Leave out (`--resource`) or later reclassify as `adopted` anything this tool didn't
   create, e.g. a role made by hand by an administrator.
4. Run `inventory audit` to confirm the inventory matches AWS.
5. **Always review the dry-run before `--apply`**, especially for `undeploy`.

## Design decisions

- **Two sources of truth.** Ownership tags on each AWS resource, plus a DynamoDB
  inventory. For origin, the **AWS tag wins**: `audit --apply` copies it into the
  inventory, and undeploy refuses to delete when the tag isn't `created`.
- **Namespaced tag keys** (`resource-deploy:*`) instead of the `ManagedBy` / `Project`
  / `Environment` keys from the original proposal. Terraform's `default_tags` already use
  those keys, and adopting a Terraform-managed resource would have overwritten them.
- **Adopted resources are still updated by `deploy`**: policy content, trust policy. The
  only thing `adopted` guarantees is that undeploy never deletes them. If they should be
  left completely untouched, that's a possible change.
- **Policies used outside the undeploy are blocked, never detached.** The original plan
  said "detach from every role that uses it". It was changed so the tool never removes
  permissions from roles, users or groups it doesn't own. The reason is stored as
  `last_error` and shown by `inventory list`.
- **Deleting one of our roles removes whatever is still on it** (other attached policies,
  inline policies, instance profile membership). The role is ours and is going away.
  Instance profiles themselves aren't deleted.
- **`keep` propagates** through dependencies (a kept role protects its attachments and the
  policies they use, and the other way round).
- **Dependents pulled into an undeploy** (e.g. attachments) are removed only if something
  they depend on is actually removed.
- **Attachments don't expire on their own** while a `created` role/policy they depend on
  hasn't expired.
- **Confirmation applies to every environment.** No environment name (such as `prod`) is
  hardcoded as special.
- **`keep`, `unkeep` and `expire` change only the inventory** and apply immediately,
  without `--apply`. Everything that changes AWS is a dry-run by default.
- **Deleted entries stay in the inventory** (`status = deleted`) as history. Recording the
  same resource again starts a fresh lifecycle.
- **Who created a resource is unknown for imports and audit orphans** (`created_by =
  unknown`). The importer is stored in `imported_by`.
- **The inventory table is created with deletion protection and point-in-time recovery.**
  To delete it, turn off deletion protection manually first.
- **All code and docs are in English**, per the project's language convention.

## Breaking changes

| When | Change |
|---|---|
| Phase 1 | CLI: `python3 src/main.py iam --env dev` became `python3 src/main.py deploy iam --env dev`. `make run-deploy` was updated. |
| Phase 1 | `INVENTORY_TABLE` is a new **required** environment variable. |
| Phase 1 | Errors about configuration, the account or the inventory print `error: ...` and exit with 1 instead of a traceback. |
| Phase 3 | `undeploy` exits with 1 when something was blocked; `inventory keep` / `unkeep` exit with 1 when a target matches nothing. |
| Phase 4 | `inventory audit` exits with 1 when there are findings (intended for CI drift checks); `inventory expire` exits with 1 when a target matches nothing. |

## Known limitations and pre-existing issues

- **Audit cost:** it reads the tags of every role and customer-managed policy in the
  account (one call each). Slow in large accounts.
- **Name matching** in `keep` / `unkeep` / `expire` changes every active entry with that
  name. Use the ARN, `--service` or `--env` to narrow it.
- **Constants in code:** the tool's own name (`tf-resource-deploy`, the value of the
  `managed-by` tag) and the tag prefix `resource-deploy:` are constants. They identify the
  tool, not an account or project, and changing them would orphan every existing tag.
  Everything account- or project-specific comes from environment variables.
- **Boundary file names (pre-existing):** `src/utils/data/iam/iam-<env>/sf-onboarding-<env>-boundary.json`
  embeds the project name in the file name, so that policy's name doesn't follow
  `PROJECT_NAME`.
- **Docker `~/.aws` mount (pre-existing):** the Makefile mounts `~/.aws` read-write, while
  the `Dockerfile` comment says read-only (`:ro`). Read-write may be needed for the
  `aws login` token cache; decide which is intended.
- **Docker and confirmation:** containers run without a terminal, so `make run-undeploy
  APPLY=1` needs `CONFIRM=<envs>`.
- **Leftover Terraform tooling** (`.tflint.hcl`, `.terraform-docs.yml`, `terraform_*`
  pre-commit hooks and Makefile targets) refers to Terraform code that isn't in this repo.

## Permissions

The identity running the tool needs, per command:

| Command | Permissions |
|---|---|
| every command | `sts:GetCallerIdentity`; `dynamodb:DescribeTable` on the inventory table |
| inventory reads (`list`, `expired`, and the lookups in `keep` / `expire` / `undeploy` / `audit`) | `dynamodb:Query`, `dynamodb:Scan` |
| every inventory write | `dynamodb:UpdateItem` |
| table creation (`create` mode) | `dynamodb:CreateTable`, `dynamodb:UpdateContinuousBackups`, `dynamodb:TagResource` |
| `deploy iam` | `iam:GetPolicy`, `GetPolicyVersion`, `CreatePolicy`, `CreatePolicyVersion`, `ListPolicyVersions`, `DeletePolicyVersion`, `TagPolicy`, `GetRole`, `CreateRole`, `UpdateAssumeRolePolicy`, `TagRole`, `ListAttachedRolePolicies`, `AttachRolePolicy` |
| `inventory import iam` | `iam:GetPolicy`, `GetRole`, `TagPolicy`, `TagRole`, `ListAttachedRolePolicies` |
| `undeploy iam` | `iam:GetRole`, `GetPolicy`, `DetachRolePolicy`, `DeleteRole`, `DeleteRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `ListInstanceProfilesForRole`, `RemoveRoleFromInstanceProfile`, `ListEntitiesForPolicy`, `ListPolicyVersions`, `DeletePolicyVersion`, `DeletePolicy` |
| `inventory audit` (iam) | `iam:ListRoles`, `ListRoleTags`, `ListPolicies`, `ListPolicyTags`, `GetRole`, `GetPolicy`, `ListAttachedRolePolicies` |
