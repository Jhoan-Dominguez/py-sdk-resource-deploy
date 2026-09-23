# Deployment inventory

The inventory tracks every resource the tool has deployed, in one place. It is built from
two sources that back each other up:

1. **Tags on each AWS resource.** Each resource says who owns it, even if the inventory is
   lost.
2. **A DynamoDB table.** It holds what AWS doesn't: when a resource was deployed, by whom,
   in which deployment, whether to keep it, and when it was deleted.

## Ownership tags

The keys are namespaced (`resource-deploy:`) so they never overwrite the `ManagedBy` /
`Project` / `Environment` tags that Terraform's `default_tags` put on the same account.

| Tag | Value |
|---|---|
| `resource-deploy:managed-by` | `tf-resource-deploy` |
| `resource-deploy:project` | `PROJECT_NAME` |
| `resource-deploy:environment` | `dev`, `mock`, `stg`, `prod`, ... |
| `resource-deploy:service` | `iam`, ... |
| `resource-deploy:deployment-id` | the deployment that created or adopted it |
| `resource-deploy:origin` | `created` or `adopted` |

## Origin: `created` vs `adopted`

- **created**: the tool created the resource. `undeploy` may delete it
  ([undeploy.md](undeploy.md)).
- **adopted**: the resource already existed, untagged, the first time a deploy reached it.
  The deploy adds the tags with `origin=adopted` and then manages it as usual (it may still
  update its content, e.g. a policy document or trust policy). **`undeploy` never deletes
  an adopted resource.**

The origin tag is written once; only `inventory import --reclassify` changes it.
Role/policy attachments can't be tagged, so the tool works out their origin: an attachment
is `created` if this run attached it, or if both its role and its policy are `created`.
Otherwise it's `adopted`.

> Resources deployed by the tool **before the inventory existed** have no tags, so a deploy
> marks them `adopted`. That's the safe default: they won't be deleted automatically. Use
> `inventory import` (below) to classify them as `created` if the tool did deploy them.

## Importing existing resources

`inventory import` records resources that **already exist** in AWS: for example ones
deployed before the inventory existed, or ones created by hand from the same JSON files.
It lets you choose their origin.

```
python3 src/main.py inventory import iam --env dev --origin created             # dry-run
python3 src/main.py inventory import iam --env dev --origin created --apply
python3 src/main.py inventory import iam --env dev --origin created --reclassify \
    --resource sf-onboarding-dev-deploy --apply
```

- It only looks at the resources the service defines (for `iam`, the ones generated from
  `src/utils/data/iam/iam-<env>/`). Anything that doesn't exist is reported as
  `not deployed, skipped`. **It never creates, updates or attaches anything**; use
  `deploy` for that.
- `--origin created|adopted` is **required**. It's the origin given to resources that
  have no origin tag yet. Only choose `created` for resources this tool deployed:
  `created` resources can be deleted by `undeploy`.
- A resource that **already has** an origin tag keeps it
  (`keeps origin=adopted (pass --reclassify ...)`). With `--reclassify`, only its
  `resource-deploy:origin` tag is changed; the other ownership tags keep their values.
  `--reclassify` is the only way to change an origin after it is set.
- `--resource NAME` (repeatable) limits the import to those role/policy names. A role's
  attachments are included when the role or the policy is selected. Unknown names print a
  warning.
- Attachments follow the same rule as in a deploy: `created` only if both the role and
  the policy are `created`.
- In the inventory, an imported resource gets `created_at` = its real creation date in
  AWS, `created_by = unknown` (nobody knows who created it), plus `imported_at` /
  `imported_by`, and `last_action = import`. If the resource was already recorded, its
  `created_*` fields keep their earlier values.

Typical first run for an environment the old tool already deployed:

1. `inventory import iam --env dev --origin created`: review the dry-run.
2. The same command with `--apply`.
3. For anything you did **not** deploy with this tool, run it with `--origin adopted
   --reclassify --resource <name> --apply`, or leave those resources out of step 2 with
   `--resource`.

## The table

### Creating it (default: `INVENTORY_TABLE_MODE=create`)

`inventory init --apply`, or the first `deploy ... --apply`, creates `INVENTORY_TABLE` with:

- partition key `pk` (string) and sort key `sk` (string);
- on-demand billing (`PAY_PER_REQUEST`);
- **deletion protection** turned on, because undeploy relies on this table;
- point-in-time recovery turned on;
- the `resource-deploy:managed-by` and `resource-deploy:project` tags.

A dry-run never creates it; it prints `does not exist, would be created` instead.

### Using an existing table (`INVENTORY_TABLE_MODE=existing`)

Set `INVENTORY_TABLE` to the existing table's name and `INVENTORY_TABLE_MODE=existing`. The tool:

- **never creates** the table, and fails with a clear error if it doesn't exist (so a typo
  in the name doesn't silently create a new table);
- requires a composite primary key of **two string attributes**. The key names are read
  from the table, so a shared single-table design with `PK`/`SK` works as-is;
- never changes the table's settings;
- marks its own items with `record_type = "deploy-inventory"` and only reads those back,
  so it can share a table with other data.

In `create` mode, an existing table with a valid key layout is also just used.

The identity running the tool needs `dynamodb:DescribeTable`, `UpdateItem`, `Query` and
`Scan` on the table, plus `CreateTable`, `UpdateContinuousBackups`, `TagResource` in
`create` mode.

### Item layout

| Attribute | Example | Notes |
|---|---|---|
| *partition key* | `sf-onboarding#iam#dev` | `<project>#<service>#<environment>` |
| *sort key* | `arn:aws:iam::<account>:role/sf-onboarding-dev-deploy` | ARN, or `attachment:<role arn>\|<policy arn>` |
| `record_type` | `deploy-inventory` | tells the tool's items apart in shared tables |
| `project`, `service`, `environment`, `account_id` | | used by the filters in `inventory list` |
| `resource_type` | `iam_role`, `iam_policy`, `iam_role_policy_attachment` | |
| `resource_name` | `sf-onboarding-dev-deploy` | |
| `origin` | `created` / `adopted` | copied from the tag |
| `status` | `active` / `deleted` | deleted entries stay as history |
| `depends_on` | list of resource ids | an attachment depends on its role and policy |
| `created_at`, `created_by`, `created_deployment_id` | | set the **first** time the resource is recorded; for imports `created_at` is the AWS creation date and `created_by` is `unknown` |
| `updated_at`, `updated_by`, `last_deployment_id` | | refreshed on every applied deploy or import |
| `last_action` | `deploy` / `import` / `undeploy` / `audit` | what the last run that touched it was |
| `imported_at`, `imported_by` | | only on imported resources, set the first time |
| `keep`, `kept_at`, `kept_by`, `keep_reason` | | set by `inventory keep`, removed by `unkeep` |
| `deleted_at`, `deleted_by`, `deleted_deployment_id` | | set when undeploy removes it (or an audit finds it gone) |
| `deleted_note` | `not found in AWS by audit` | only when the audit marked it deleted |
| `expires_at` | `2026-10-01T00:00:00Z` | set by `--ttl` or `inventory expire` ([expiry.md](expiry.md)) |
| `last_error`, `last_error_at` | | why the last undeploy couldn't remove it; cleared on success or redeploy |

`*_by` is the ARN of the AWS identity that ran the tool (from `sts:GetCallerIdentity`).

## Reading it

```
python3 src/main.py inventory list [--env dev] [--service iam] [--status active|deleted] \
    [--deployment-id <id>]
python3 src/main.py inventory audit        # does it still match AWS? see audit.md
```

It prints one row per resource, sorted by environment, service, type and name, with
`KEEP` and `EXPIRES` columns. Under the table it lists keep reasons and the last undeploy error of any
resource that couldn't be removed. `--deployment-id` shows what a given deployment
created. When you pass both `--env` and `--service`, it runs a single DynamoDB `Query`;
otherwise it scans the table, filtered to your `PROJECT_NAME`.

A resource that is recorded again after being deleted (redeployed or reimported) starts a
new lifecycle: its `created_*` fields are reset and its `deleted_*` / `last_error` fields
are removed.
