# Undeploy

`undeploy` deletes resources the tool created, working from the inventory. Like every
command that changes AWS, it's a **dry-run unless you pass `--apply`**, and with
`--apply` it also asks for confirmation.

```
python3 src/main.py undeploy iam --env dev                                 # plan for one service/env
python3 src/main.py undeploy iam --env dev --resource front-publish-site-dev   # only that resource
python3 src/main.py undeploy --deployment-id 20260922T101500Z-1a2b3c4d     # what one run created
python3 src/main.py undeploy iam --env dev --apply                         # asks you to type "dev"
python3 src/main.py undeploy iam --env dev --apply --confirm dev           # non-interactive
python3 src/main.py undeploy --expired [iam] [--env dev]                   # whatever has expired
```

## Picking what to remove

| Form | Selects |
|---|---|
| `undeploy <service> --env a,b` | every active inventory entry of that service in those environments |
| `... --resource NAME` (repeatable) | only those resource names (unknown names print a warning) |
| `undeploy --deployment-id ID` | every active entry **created** by that deployment (the id printed by `deploy` / `import`, or shown by `inventory list --deployment-id`), across services and environments |
| `undeploy --expired [service] [--env a,b]` | every active entry whose expiry has passed ([expiry.md](expiry.md)); service and `--env` are optional filters |

Selecting a resource **also selects everything that depends on it**. For example, a
policy brings its attachments along, because it can't be deleted while it's attached.
A dependent pulled in this way is removed only if something it depends on is actually
removed. If its role/policy ends up skipped (e.g. kept), it stays and is reported as
`what it depends on is not being removed`.

## What is never deleted

Each selected entry is checked in this order. The first match skips it and prints why:

1. **Another account**: the entry's `account_id` isn't the current `AWS_ACCOUNT_ID`.
2. **`origin=adopted`**: the tool didn't create it (see [inventory.md](inventory.md)).
3. **Kept**: protected with `inventory keep`.
4. **Linked to a kept resource**: protection spreads through dependencies. The
   attachments of a kept role, and the policies those attachments use, are protected too,
   because deleting them would change the role you asked to keep. The same applies in the
   other direction for a kept policy.

The service then double-checks AWS itself before deleting: a role or policy whose
`resource-deploy:origin` **tag** isn't `created` is blocked, even if the inventory says
otherwise.

## Keeping resources

```
python3 src/main.py inventory keep sf-onboarding-dev-deploy --reason "used by CI"
python3 src/main.py inventory keep 20260922T101500Z-1a2b3c4d            # everything that run created
python3 src/main.py inventory unkeep sf-onboarding-dev-deploy
```

A target can be a resource name, a resource id (ARN), or the deployment id that created
it. `--env` / `--service` narrow the match. Keep only changes the inventory (`keep`,
`kept_at`, `kept_by`, `keep_reason`), so it applies immediately, without `--apply`.
`inventory list` shows a `KEEP` column and the reasons under the table. A target that
matches nothing prints a warning and exits with code 1.

## How `iam` deletes

Order: **attachments → roles → policies**.

| Resource | What happens |
|---|---|
| Attachment | `DetachRolePolicy`. If it's already detached, it counts as done. |
| Role | The role is ours and is going away, so everything still on it goes too: other attached policies are detached, inline policies deleted, and it's removed from instance profiles. Then `DeleteRole`. The dry-run lists these extras. |
| Policy | **Blocked** if anything outside this undeploy still uses it: another role, a user, a group, or a role using it as a permissions boundary. It's never detached from things the tool doesn't own. Otherwise its non-default versions are deleted, then `DeletePolicy`. |

A resource that no longer exists in AWS counts as removed (`already gone`). One failure
doesn't stop the rest. At the end you get `N removed, N blocked, N skipped`, and the exit
code is 1 if anything was blocked.

## What the inventory records

| Outcome | Inventory change |
|---|---|
| Deleted / already gone | `status = deleted`, `deleted_at`, `deleted_by`, `deleted_deployment_id`, `last_action = undeploy`; keep fields removed |
| Blocked / failed | stays `active`; `last_error`, `last_error_at` (shown by `inventory list`) |

Deleted entries stay in the table as history; see them with
`inventory list --status deleted`. If you deploy (or import) the same resource again, its
entry starts over: `status = active`, new `created_*` values, and the `deleted_*` /
`last_error` fields removed.

## Confirmation

With `--apply`, if anything would be deleted, you must type the affected environments,
sorted and comma-separated (e.g. `mock,stg`). Without a terminal (Docker without `-it`,
CI), pass the same value with `--confirm`. A wrong value aborts before anything is
deleted.

With Docker: `make run-undeploy [SERVICE=iam] [ENV=dev] [ARGS="--resource X"] [APPLY=1 CONFIRM=dev]`.

## Permissions

Besides the deploy permissions, the identity needs: `iam:DetachRolePolicy`,
`DeleteRole`, `DeleteRolePolicy`, `ListRolePolicies`, `ListInstanceProfilesForRole`,
`RemoveRoleFromInstanceProfile`, `ListEntitiesForPolicy`, `ListPolicyVersions`,
`DeletePolicyVersion` and `DeletePolicy`.

The full per-command list is in [notes.md](notes.md#permissions).
