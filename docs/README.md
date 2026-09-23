# tf-resource-deploy

A boto3 command-line tool that deploys AWS resources for a project on demand and keeps a
central **inventory** of everything it deployed, so you can later decide what to keep and
what to remove.

Today it deploys one service, `iam`: the IAM roles and policies used by whoever runs the
project's Terraform. Those stay outside Terraform on purpose, because Terraform can't
manage the permissions of the identity that is running it.

| Guide | What it covers |
|---|---|
| [inventory.md](inventory.md) | The inventory table, ownership tags, `created` vs `adopted`, importing existing resources, using an existing table |
| [undeploy.md](undeploy.md) | Removing resources: selection, `keep` protection, what's never deleted, confirmation |
| [audit.md](audit.md) | Comparing inventory, AWS and definitions: findings and how to fix them |
| [expiry.md](expiry.md) | Expiry dates (`--ttl`, `inventory expire`), `inventory expired`, `undeploy --expired` |
| [notes.md](notes.md) | **Read before first real use:** verification status, setup checklist, design decisions, breaking changes, known limitations, required permissions |
| [iam-service.md](iam-service.md) | How the JSON files in `src/utils/data/iam/` become roles and policies |

## Setup

1. Install the dependencies (Python 3.13): `pip install -r requirements.txt`.
2. Copy `.env.template` to `.env`, fill it in, and export it (or use Docker, below).
3. Authenticate to AWS (profile, `aws login`, or temporary credentials from
   `make authenticate_aws`).

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `AWS_ACCOUNT_ID` | yes | Target account. The tool stops if your credentials belong to a different account. |
| `AWS_REGION` | yes | Region for the boto3 clients (and for the inventory table). |
| `PROJECT_NAME` | yes | Prefix for resource names, e.g. `<PROJECT_NAME>-dev-deploy`. It also scopes the inventory. |
| `TRUSTED_PRINCIPAL_USER` | yes | IAM user allowed to assume the roles the `iam` service creates. |
| `INVENTORY_TABLE` | yes | Name of the DynamoDB inventory table. |
| `INVENTORY_TABLE_MODE` | no | `create` (default): the tool creates the table if it's missing. `existing`: only use a table that already exists. See [inventory.md](inventory.md). |

## Commands

Run everything from the repo root. **Every command that changes AWS is a dry-run unless you
pass `--apply`.** (`inventory keep`/`unkeep`/`expire` only change the inventory and apply
immediately.)

```
python3 src/main.py deploy iam --env dev                # show what would change
python3 src/main.py deploy iam --env dev --apply         # apply it and record it in the inventory
python3 src/main.py deploy iam --env dev,mock,stg,prod   # several environments, one deployment id

python3 src/main.py inventory init                      # check the inventory table
python3 src/main.py inventory init --apply              # ...and create it if missing (create mode)
python3 src/main.py inventory list                      # everything recorded for PROJECT_NAME
python3 src/main.py inventory list --env dev --service iam --status active

python3 src/main.py inventory import iam --env dev --origin created            # dry-run
python3 src/main.py inventory import iam --env dev --origin created --apply    # tag + record
python3 src/main.py inventory keep sf-onboarding-dev-deploy --reason "used by CI"
python3 src/main.py inventory unkeep sf-onboarding-dev-deploy

python3 src/main.py undeploy iam --env dev                     # show what would be deleted
python3 src/main.py undeploy iam --env dev --apply             # delete (asks you to type "dev")
python3 src/main.py undeploy --deployment-id <id> --apply      # delete what one run created

python3 src/main.py inventory audit [--apply]                  # inventory vs AWS vs definitions
python3 src/main.py deploy iam --env dev --ttl 7d --apply      # deploy with an expiry
python3 src/main.py inventory expire <name> --in 3d | --at 2026-10-01 | --clear
python3 src/main.py inventory expired                          # what's due
python3 src/main.py undeploy --expired [--apply]               # remove what's due (keep wins)
```

With Docker (reads `.env`, mounts `~/.aws`):

```
make run-deploy [SERVICE=iam] [ENV=dev] [APPLY=1]
make run-inventory [ARGS="--env dev"]
make run-import ORIGIN=created [SERVICE=iam] [ENV=dev] [ARGS="--reclassify"] [APPLY=1]
make run-undeploy [SERVICE=iam] [ENV=dev] [ARGS="--resource NAME"] [APPLY=1 CONFIRM=dev]
make run-audit [ARGS="--service iam --env dev"] [APPLY=1]
```

### The lifecycle

1. `deploy` (optionally `--ttl`): resources are created and recorded as `active`, `created`.
2. `inventory list` / `inventory expired`: review what exists and what's due;
   `inventory keep` what must stay.
3. `undeploy` (or `undeploy --expired`): deletes the `created`, non-kept ones and marks
   them `deleted` in the inventory, which keeps the history.
4. `inventory audit`, now and then or in CI: catches anything changed outside the tool.

### What a deploy does

1. Checks that your credentials belong to `AWS_ACCOUNT_ID`.
2. Checks the inventory table. With `--apply` in `create` mode, it creates the table if
   it's missing. A missing table in `existing` mode, or a table with the wrong key
   layout, stops the run before anything else is touched.
3. Generates a **deployment id** (`<UTC timestamp>-<random>`), shared by every
   environment in this run.
4. Creates or updates each resource, tags it with its ownership, and records it in the
   inventory right after that step succeeds. If the run fails halfway, the inventory still
   shows exactly what exists.

Each output line ends with the resource's origin in brackets, e.g. `[created]` or `[adopted]`.

## Roadmap

| Phase | Status | Scope |
|---|---|---|
| 1 | done | Ownership tags, `created`/`adopted` origin, inventory table, `inventory init` / `list` |
| 2 | done | `inventory import`: record (and reclassify) resources that already exist in AWS |
| 3 | done | `undeploy` (per service+environment, per resource or per deployment id), `inventory keep`/`unkeep` |
| 4 | done | `inventory audit` (missing / orphan / origin-mismatch / undefined / untracked), expiry (`--ttl`, `inventory expire`, `inventory expired`, `undeploy --expired`) |

## Adding a service

Create `src/deploy/services/<name>.py` with a class that implements `ServiceDeployer`
(`src/deploy/services/base.py`), then register it in `SERVICES` in `src/deploy/cli.py`.
The service must:

- tag what it creates with `run.tags(ORIGIN_CREATED)`, and tag untagged resources it takes
  over with `run.tags(ORIGIN_ADOPTED)`;
- implement `import_existing(run, options)`: find the resources it defines that already
  exist, tag them per `ImportOptions` and record them, **without changing their content**;
- call `run.record(ResourceRecord(...))` after each resource, listing in `depends_on` the
  ids it depends on (undeploy uses it for selection and `keep` protection);
- implement `undeploy(run, items)`: delete the given inventory items in dependency order,
  refuse any whose AWS origin tag isn't `created`, and call `run.record_removal(item,
  error)` for each one. Which items are allowed is decided beforehand by the shared
  planner (`src/deploy/undeploy.py`), not by the service;
- implement `environments()` (the environments it has definitions for) and `audit(run,
  items)`: return `Finding`s comparing the inventory, AWS and its definitions, without
  changing anything;
- only call write APIs when `run.apply` is true.

Then document it in `docs/<name>-service.md`, link it from the table at the top of this
file, and add its permissions to [notes.md](notes.md#permissions).
