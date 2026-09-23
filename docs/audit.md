# Audit

`inventory audit` compares three things: the **inventory**, **AWS**, and the service's
**definitions** (for `iam`, the JSON files). It reports where they disagree.

```
python3 src/main.py inventory audit                          # every service, every known environment
python3 src/main.py inventory audit --service iam --env dev   # narrower
python3 src/main.py inventory audit --apply                  # also fix the inventory (never AWS)
```

Without `--env`, each service is audited in every environment it has definitions for
(the `iam-*` folders), plus any environment that appears in the inventory.

The exit code is **1 if there is any finding**, so it can run in CI or on a schedule as a
drift check.

## Findings

| Kind | Meaning | `--apply` does | What you do |
|---|---|---|---|
| `missing` | Active in the inventory, but gone from AWS (deleted outside the tool), or an attachment that is no longer attached | marks it `deleted` with `deleted_note = not found in AWS by audit` | nothing, or `deploy` again if it should exist |
| `orphan` | In AWS with this project/environment's ownership tags (or an attachment between two of our resources), but not active in the inventory | records it, with the origin from its tag | review it; `undeploy` it if it shouldn't exist |
| `origin-mismatch` | The AWS origin tag and the inventory disagree | copies the **tag's** origin into the inventory (the tag is the source of truth) | review it |
| `origin-mismatch`, no tag | The inventory has it, but the AWS resource has no origin tag, so undeploy would block it | nothing (it would need a change in AWS) | `inventory import ... --origin <x> --resource <name> --apply` (the hint prints the full command) |
| `undefined` | Active and deployed, but no longer in the definitions (e.g. its JSON file was removed) | nothing | `undeploy <service> --env <env> --resource <name>` if it should go |
| `untracked` | Defined and present in AWS, but untagged and not in the inventory | nothing | `inventory import <service> --env <env> --origin created\|adopted --resource <name>` |

`--apply` **only writes to the inventory**. The audit never creates, changes, tags or
deletes anything in AWS. Entries it records or marks deleted get `last_action = audit`.
Orphans get `created_by = unknown`.

## How `iam` is audited

- For every active inventory entry: `GetRole` / `GetPolicy`, or the role's attached
  policies for an attachment. It also compares the origin tag with the inventory and
  checks that the name is still defined.
- To find orphans, it lists **every role and every customer-managed policy in the
  account** and reads their tags. IAM's list calls don't return tags, so this costs one
  `ListRoleTags` / `ListPolicyTags` call per role and per policy. In accounts with many
  roles, the audit takes a while and may be throttled.
- Only resources whose `managed-by`, `project`, `environment` and `service` tags all match
  count as orphans of that environment.
- Orphan attachments: policies attached to our roles that are also ours (tagged or in
  the inventory). Their origin follows the usual rule (`created` only if both ends are
  `created`).

## Permissions

`iam:ListRoles`, `ListRoleTags`, `ListPolicies`, `ListPolicyTags`, `GetRole`,
`GetPolicy`, `ListAttachedRolePolicies`, and, with `--apply`, `dynamodb:UpdateItem` on
the inventory table.
