# `iam` service

`deploy iam --env <env>` turns the JSON policy documents in
`src/utils/data/iam/iam-<env>/` into IAM managed policies and roles.

## What each file becomes

The file name decides what gets created:

| File name | Result |
|---|---|
| `tf-<env>-N-*.json` | One managed policy each, all attached to a single role, `<PROJECT_NAME>-<env>-deploy`. `N` groups them by area: 1 state S3, 2 network, 3 IAM roles, 4 Lambda/Step Functions/logs, 5 data stores, 6 containers, 7 edge, 8 static site. |
| `*-boundary.json` | A managed policy that is **never attached** to a role. Terraform uses it as a permissions boundary, by ARN. |
| `*-no-boundary.json` | Alternate version of another file; skipped. |
| anything else (`front-publish-site.json`, `keycloak-admin-exec.json`) | Its own role with only that policy attached. |

**Names:** if the file name already contains the environment (`tf-dev-1-state-s3`), it's
used as-is. Otherwise `-<env>` is added (`front-publish-site-dev`), because IAM names are
unique per account and the same file name appears in every environment folder.

**Trust policy:** every role can only be assumed by
`arn:aws:iam::<AWS_ACCOUNT_ID>:user/<TRUSTED_PRINCIPAL_USER>`.

**Account id:** the JSON files never contain it. They use `${AWS_ACCOUNT_ID}`, which is
replaced when the file is loaded.

## Environment folders

`dev`, `stg` and `prod` have the same 12 files, with the environment name swapped. `mock`
has 9: no boundary, state-s3 or no-boundary files, because the mock environment uses local
Terraform state and no permissions boundary. When you change a policy, make the same change
in every folder.

## Change behavior

| Resource | Missing | Exists and different | Exists and equal |
|---|---|---|---|
| Policy | created, tagged `origin=created` | new default version (the oldest non-default version is deleted when the limit of 5 is reached) | unchanged |
| Role | created, tagged `origin=created` | trust policy replaced | unchanged |
| Attachment | attached | — | unchanged |

An existing role or policy without the `resource-deploy:origin` tag is tagged
`origin=adopted` first (see [inventory.md](inventory.md)). Every role, policy and
attachment is recorded in the inventory as `iam_role`, `iam_policy` or
`iam_role_policy_attachment`. Each attachment's `depends_on` lists its role and policy.

`inventory import iam` checks the same names, but only tags and records what already
exists (see [inventory.md](inventory.md#importing-existing-resources)).

`undeploy iam` removes attachments, then roles, then policies. A policy still used by
anything outside the undeploy is blocked, never detached (see [undeploy.md](undeploy.md)).

`inventory audit` lists every role and customer-managed policy in the account to find
orphans, which costs one tag lookup per resource (see [audit.md](audit.md)).
