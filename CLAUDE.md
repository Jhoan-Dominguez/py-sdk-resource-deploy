# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Terraform IaC (plus a thin Python layer) for `sf-onboarding`: an AWS order-processing workflow (SQS →
Lambda → Step Functions → DynamoDB task tokens) fronted by an ECS/Fargate backend + Keycloak for auth,
edge CloudFront/WAF, and an S3+CloudFront static frontend. Target AWS account is a Bancolombia sandbox
(`011221923990`, `us-east-1`).

## Repo state — read before trusting the docs

**The repository is mid-build-out: the module READMEs and `terraform/environment/dev/docs/*.md`
describe a target architecture that is well ahead of what actually exists on disk.** Concretely, as of
now:

- `terraform/environment/dev/main.tf` references 8 modules that **do not exist** under `terraform/modules/`:
  `vpc`, `step_function`, `acm_certificate`, `container_image`, `rds_postgres`, `ecs_platform`, `edge`,
  `ecs_service`. Only these 10 modules exist: `dynamodb`, `ecr`, `iam`, `iam_policies`, `lambda`,
  `lambda_container`, `secret`, `sqs`, `ssm_parameter`, `static_site`. `terraform init`/`plan` on
  `environment/dev` will currently fail on the missing module sources.
- `main.tf` also points Lambda/container `source_dir`s at `src/app/lambda/...`, `src/app/backend`,
  `src/app/keycloak` — none of which exist. `src/` only contains `src/main.py`, an unused PyCharm
  boilerplate stub, not part of the application.
- `terraform/environment/{stg,prod,mock}/` and `python_tests/`, `test/` (mentioned in the dev README's
  "Full Repository Structure" diagram and in `pyproject.toml`'s `testpaths`) don't exist either.
- Before relying on a module/path/env the docs mention, verify it's actually on disk (`ls`/`find`) rather
  than trusting the README.

When adding the missing pieces, match the conventions of the 10 modules that already exist (below) —
they're the real source of truth for this project's style, not the aspirational docs.

Not a git repository yet (no `.git`).

## Commands

AWS access (assumes the onboarding role into the sandbox account):
```
make authenticate_aws
```

Python (`src/`, all driven through pre-commit; ruff config in `pyproject.toml` targets py313,
line-length 100; there is a second, unused `pyproject_black.toml` with different settings — line-length
88 — left over from before the ruff/black setup in `pyproject.toml`, don't use it):
```
make fmt                 # black --all-files, ruff --fix (manual stage), terraform_fmt
make pre-commit-python   # black + ruff only (mypy line is commented out in the Makefile)
```
No test suite exists yet — `pyproject.toml` points pytest at `python_tests/unit`, which hasn't been
created.

Terraform (run pre-commit targets from repo root; they operate on whatever `.tf` files are staged/changed):
```
make validate             # terraform_validate
make pre-commit-terraform # terraform_fmt, terraform_validate, terraform_tflint, terraform_docs
```
`make security` (checkov, detect-secrets) will no-op/fail: both hooks are commented out in
`.pre-commit-config.yaml`.

Per-environment (only `dev` exists):
```
cd terraform/environment/dev
terraform init && terraform plan && terraform apply
```
`terraform/environment/dev/Makefile` has two shortcuts:
- `make deploy-dev` — single `plan -parallelism=4 -out=tfplan` + `apply`.
- `make deploy-by-service` — applies modules one at a time with `-target` in dependency order (useful
  for bringing the environment up from scratch / debugging a single module), then a final untargeted
  apply. It predates several modules added later (`ecr_process_order`, `external_api_key`, the whole
  ECS/Keycloak/edge block) so its target list is incomplete — Terraform still resolves those as implicit
  dependencies of the targets it does list.

`terraform-docs` regenerates a module/environment's `README.md` from its own `docs/header.md` +
`docs/footer.md` (config at repo-root `.terraform-docs.yml`); every existing module and
`environment/dev` has its own `docs/` pair.

## Architecture conventions (from the modules that actually exist)

- **Layout**: `terraform/modules/<name>` are reusable, single-purpose modules (one AWS service/concern
  each — e.g. `dynamodb`, `sqs`, `ecr`); `terraform/environment/<env>` are root modules that compose
  them. Only `dev` is wired up.
- **Naming**: nearly everything is parameterized by `name_prefix = "${project_name}-${environment}"`.
  Common tags (`Project`, `Environment`, `ManagedBy`) come from the `aws` provider's `default_tags`
  block in `providers.tf`, not per-resource `tags`.
- **IAM is split in two, applied in order**: `modules/iam` creates the roles and their trust
  (assume-role) policies only; `modules/iam_policies` attaches the fine-grained permission policies
  and is composed *last* in `main.tf`, once every resource ARN it needs (Lambda ARNs, table ARN, state
  machine ARN, etc.) already exists. Roles that are only needed for a retired/optional path use
  `count = var.create_legacy_callback_path ? 1 : 0` (or similar feature-flag variables) rather than a
  separate module variant.
- **Two Lambda deployment modules**: `modules/lambda` zips `source_dir` with the `archive_file` data
  source (`hashicorp/archive` provider) and deploys a `.zip`-based function. `modules/lambda_container`
  instead hashes `source_dir`, then uses a `null_resource` with a `local-exec` provisioner to
  `docker buildx build` + push to ECR and deploy an image-based function — this means Docker and an
  authenticated AWS CLI must be available wherever `terraform apply` runs for anything using this
  module, and plans/applies against it are not pure-API operations.
- **State**: `environment/dev/backend.tf` uses an S3 backend with native locking (`use_lockfile = true`,
  requires Terraform >= 1.10, per `versions.tf`) — no DynamoDB lock table. The state contains secrets
  generated by `modules/secret`, so the backend bucket must stay private/versioned/encrypted. Backend
  block values are literals (Terraform doesn't allow variables there).
- **Terraform/provider versions**: modules generally pin `required_version = ">= 1.9.0, < 2.0.0"` (the
  `dev` environment itself requires `>= 1.10.0` for the S3 native lock) and `hashicorp/aws ~> 5.60`.
- Inline comments inside `.tf` files are mostly in Spanish (matching the team); keep that convention
  when editing existing files rather than switching a file's comments to English mid-file.
