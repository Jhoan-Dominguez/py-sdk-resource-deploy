# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A small boto3 CLI (not Terraform) that bootstraps the **IAM identities that run Terraform** for the
`sf-onboarding` project (an AWS order-processing workflow: SQS → Lambda → Step Functions → DynamoDB,
ECS/Fargate backend + Keycloak, CloudFront/WAF edge, S3+CloudFront frontend). It turns JSON policy
documents under `src/utils/data/iam/iam-<env>/` into IAM managed policies and roles for the `dev`,
`mock`, `stg` and `prod` environments, and records everything it deploys in a central DynamoDB
inventory so resources can later be reviewed and removed (deploy + undeploy lifecycle).

User-facing docs live in `docs/` (`README.md` index, `inventory.md`, `undeploy.md`, `audit.md`,
`expiry.md`, `iam-service.md`, `web-ui.md`, `notes.md`; `docs/history.txt` is a pasted chat
transcript, not a doc). **Every
change to the tool must update them** — they are part of "done", not optional.

It is deliberately outside Terraform: these are the permissions of the identity that applies
Terraform (plus the permissions boundary Terraform's `module.iam` references by ARN), so managing them
from the same state would be a circular bootstrap dependency.

The Terraform code itself **does not live in this repo**. Leftover Terraform tooling (`.tflint.hcl`,
`.terraform-docs.yml`, the `terraform_*` pre-commit hooks and Makefile targets, Terraform entries in
`.gitignore`) targets paths like `terraform/environment/...` that don't exist here; references to
those paths in docstrings are to the separate Terraform repo.

## Commands

Run the CLI from the repo root with the variables in `.env.template` exported (all required except
`INVENTORY_TABLE_MODE`); the tool refuses to start without them:
```
python3 src/main.py deploy iam --env dev                 # dry-run (default): only read APIs are called
python3 src/main.py deploy iam --env dev --apply         # write to AWS + record in the inventory
python3 src/main.py deploy iam --env dev,mock,stg,prod   # several environments, one deployment id
python3 src/main.py inventory init [--apply]             # validate / create the inventory table
python3 src/main.py inventory list [--env dev] [--service iam] [--status active]
python3 src/main.py inventory import iam --env dev --origin created|adopted [--reclassify] \
    [--resource NAME ...] [--apply]                     # tag + record existing resources only
python3 src/main.py inventory keep|unkeep <name|arn|deployment-id>... [--reason TEXT]
python3 src/main.py undeploy iam --env dev [--resource NAME ...] [--apply [--confirm dev]]
python3 src/main.py undeploy --deployment-id <id> [--apply [--confirm <envs>]]
python3 src/main.py undeploy --expired [iam] [--env dev] [--apply [--confirm <envs>]]
python3 src/main.py inventory audit [--service iam] [--env dev] [--apply]   # --apply fixes inventory only
python3 src/main.py inventory expire <target>... --in 7d | --at 2026-10-01 | --clear
python3 src/main.py inventory expired                   # deploy/import also take --ttl 7d
```

Via Docker (reads `.env`, mounts `~/.aws` for boto3's credential chain):
```
make run-deploy [SERVICE=iam] [ENV=dev] [APPLY=1]
make run-inventory [ARGS="--env dev"]
make run-import ORIGIN=created [ENV=dev] [ARGS="--reclassify"] [APPLY=1]
make run-undeploy [ENV=dev] [ARGS="--resource NAME"] [APPLY=1 CONFIRM=dev]
make run-audit [ARGS="--service iam --env dev"] [APPLY=1]
```

Optional local web UI (Streamlit, `src/webui/`, labels in Spanish), same env vars:
```
pip install -r requirements-ui.txt      # requirements.txt + streamlit + pandas
make run-ui                             # exports .env; serves on 127.0.0.1:8501 (no login)
```

AWS auth into the sandbox account: `make authenticate_aws` (prints STS temp creds for the onboarding
role). `requirements.txt` includes `botocore[crt]` because some local AWS CLI profiles use `aws login`.

Lint/format (pre-commit, scoped to `src/`; ruff + black config in `pyproject.toml`, py313, line
length 100 — ignore `pyproject_black.toml`, it's an unused leftover):
```
make fmt                 # black + ruff --fix across all files (its terraform_fmt step is a leftover)
make pre-commit-python   # black + ruff on staged files
```
A local `.venv/` has black, ruff and pre-commit installed.
mypy is configured (`mypy.ini`) but its pre-commit hook is commented out. There is no test suite:
`pyproject.toml` points pytest at `python_tests/unit`, which doesn't exist. To exercise the CLI without
AWS, run `main([...])` inside `moto`'s `mock_aws()` (moto isn't in `requirements.txt`; install it in a
throwaway venv). For the web UI, `streamlit.testing.v1.AppTest.from_file("src/webui/app.py")`
works inside `mock_aws()` too (buttons/inputs are addressed by their `key=`).

## Architecture

- `src/main.py` — entry point; puts `src/` on `sys.path` and calls `deploy.cli.main`.
- `src/deploy/cli.py` — argparse with `deploy <service>`, `undeploy`, and
  `inventory init|list|import|keep|unkeep|expire|expired|audit` subcommands. Also holds the
  expiry selection (`expired_items`: a dependent doesn't expire while a `created` dependency is
  unexpired) and applies audit fixes (inventory only).
  Verifies `sts:GetCallerIdentity` matches `AWS_ACCOUNT_ID` before any command, validates/creates the
  inventory table before touching a service, then builds one `DeployRun` per environment (all sharing
  one deployment id, built by `_runs()`) and passes it to the service. `SERVICES` is the service
  registry: a new service is a `services/<name>.py` class implementing `ServiceDeployer`, registered
  there.
  Its public helpers (`inventory_store`, `verified_caller_arn`, `expired_items`, `undeploy_plan`)
  are shared with the web UI; `undeploy_plan` is the one selection function behind both
  `undeploy` and the UI's preview.
- `src/webui/` — Streamlit UI. `backend.py` is its only bridge to `deploy`: reads call the
  library (cached 30 s), **every write runs `cli.main([...])`** with captured stdout under a lock,
  so no safety rule is reimplemented in the UI. `components/run_flow.py` enforces dry-run before
  apply (same argv) and passes the typed environments as `--confirm` for undeploy.
- `src/deploy/resources.py` — `DeployRun` (run context: env, apply flag, deployment id, caller ARN,
  `action` deploy/import, `record()` callback, `tags(origin)`), `ResourceRecord`, `ImportOptions`,
  and the `resource-deploy:*` ownership tag
  keys (namespaced so they don't clash with Terraform's `default_tags`).
- `src/deploy/inventory.py` — `InventoryStore` over DynamoDB. The table's key attribute names are read
  from `describe_table`, so an existing table with any string `HASH`+`RANGE` keys works
  (`INVENTORY_TABLE_MODE=existing` never creates one). Items are upserted with `if_not_exists` for the
  `created_*` fields and marked `record_type=deploy-inventory`.
- `src/deploy/undeploy.py` — `plan_undeploy()`: the service-agnostic rules for what an undeploy
  may delete (selection closed over dependents; skip other-account, `adopted`, kept, and anything
  linked to a kept item through `depends_on`; a dependent pulled in by the closure is only deleted if
  something it depends on is deleted). Services never decide this themselves.
- `src/deploy/config.py` — the only place env vars are read.
- `src/deploy/services/iam.py` — the only service today. Idempotent "ensure" functions compare AWS
  state to the desired document and only call write APIs when `run.apply`. Updating a policy creates a
  new default version, pruning the oldest non-default one at the 5-version cap.

Service contract (`services/base.py`): tag created resources `origin=created`, tag untagged existing
ones `origin=adopted` (the origin tag is never rewritten; adopted resources must never be deleted by
undeploy), call `run.record(...)` right after each resource succeeds (so partial failures leave an
accurate inventory), and fill `depends_on` so undeploy can delete in reverse order. Services also
implement `import_existing(run, options)`: tag + record resources that already exist, never changing
their content; the origin tag is only overwritten with `--reclassify`. And `undeploy(run, items)`:
delete the pre-approved items in dependency order, re-check the AWS origin tag, report each outcome via
`run.record_removal(item, error)`, and never detach a policy from anything outside the plan (block
instead). Plus `environments()` and `audit(run, items) -> list[Finding]` (read-only; kinds in
`resources.FINDING_*`), and optionally `definitions(environment) -> list[Definition]` (what
`deploy` would manage, from local files only; feeds the UI's catalog).

Inventory writes: DynamoDB rejects unused `ExpressionAttributeNames`, so build them per expression
(`inventory._names`). `put()` resets the lifecycle when the item was `status=deleted` (conditional
update, then an unconditional one).

All four roadmap phases (see `docs/README.md`) are done: inventory + tags, import, undeploy + keep,
audit + expiry. `docs/notes.md` holds the verification status (mocked AWS only so far), design
decisions, breaking changes, known limitations and the per-command permission list; **any caveat worth
telling the user belongs there too, not only in chat.**

### Policy file conventions (`src/utils/data/iam/iam-<env>/`)

The file name decides what gets created (full detail in `docs/iam-service.md`):
- `tf-<env>-N-*.json` → all attached to one role, `<PROJECT_NAME>-<env>-deploy`. The `N` orders them
  by concern (1 state-s3, 2 network, 3 iam-roles, 4 lambda/sfn/logs, 5 data stores, 6 containers,
  7 edge, 8 static site).
- `*-boundary.json` → created as a managed policy, **never attached** (it's the permissions boundary
  Terraform references by ARN).
- `*-no-boundary.json` → alternate variant, skipped.
- Any other file (`front-publish-site.json`, `keycloak-admin-exec.json`) → its own single-policy role.
- Names: a stem that already contains the env token is used as-is; otherwise `-<env>` is appended,
  since IAM names are account-wide and these files repeat across env folders.
- All roles get the same trust policy: only `arn:aws:iam::<AWS_ACCOUNT_ID>:user/<TRUSTED_PRINCIPAL_USER>`
  may assume them.
- JSON files never contain the account id literally — use the `${AWS_ACCOUNT_ID}` placeholder, which
  is substituted at load time.
- `dev`, `stg`, `prod` intentionally carry the same 12-file set (env token swapped). `mock` has 9:
  no boundary, state-s3 or no-boundary files, because mock uses local Terraform state and no
  boundary. When changing a policy, apply the equivalent change to every env folder.

## Conventions

- No hardcoded account ids, user names, project names or regions in code or policy JSON — anything
  environment/identity-specific must be a required env var in `config.py` and listed in
  `.env.template`.
- All new code, comments and docstrings are in English (some older tooling comments, e.g. in
  `.pre-commit-config.yaml`, are Spanish; leave them unless asked).
- `.dockerignore` excludes `*.json` globally but re-includes `src/utils/data/iam/**/*.json`; keep that
  exception if data files move.
