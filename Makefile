
SERVICE ?= iam
ENV ?= dev

authenticate_aws:
	aws sts assume-role \
  --role-arn arn:aws:iam::011221923990:role/iaac-bancolombia-onboarding \
  --role-session-name sesion-local \
  --duration-seconds 3600
#  --role-arn arn:aws:iam::011221923990:role/front-publish-site \
#  --role-arn arn:aws:iam::011221923990:role/keycloak-admin-exec \

reset-precommit:
	@pre-commit clean
	@pre-commit install

fmt:
	pre-commit run black --all-files
	pre-commit run ruff --all-files --hook-stage manual -- --fix || true
	pre-commit run terraform_fmt --all-files

validate:
	pre-commit run terraform_validate --all-files

security:
	pre-commit run terraform_checkov --all-files
	pre-commit run detect-secrets --all-files

pre-commit-python:
	@echo "Running pre-commit hooks ..."
	pre-commit run black
	pre-commit run ruff
	#pre-commit run mypy

pre-commit-terraform:
	@echo "Running pre-commit hooks ..."
	pre-commit run terraform_fmt --all-files
	pre-commit run terraform_validate --all-files
	pre-commit run terraform_tflint --all-files
	pre-commit run terraform_docs --all-files
	#pre-commit run terraform_checkov --all-files

docker-build:
	docker build -t tf-resource-deploy .

DOCKER_RUN = docker run --rm --env-file .env -v $(HOME)/.aws:/root/.aws tf-resource-deploy

# Runs the deploy CLI in a container, mounting your local AWS profile (~/.aws) so boto3
# authenticates the same way it would outside Docker. Env vars come from .env
# (see .env.template). Usage: make run-deploy [SERVICE=iam] [ENV=dev] [APPLY=1]
run-deploy: docker-build
	$(DOCKER_RUN) deploy $(SERVICE) --env $(ENV) $(if $(APPLY),--apply,)

# Lists the deployment inventory. Usage: make run-inventory [ARGS="--env dev --service iam"]
run-inventory: docker-build
	$(DOCKER_RUN) inventory list $(ARGS)

# Imports already-deployed resources into the inventory. ORIGIN is required (created|adopted).
# Usage: make run-import ORIGIN=created [SERVICE=iam] [ENV=dev] [ARGS="--reclassify"] [APPLY=1]
run-import: docker-build
	$(DOCKER_RUN) inventory import $(SERVICE) --env $(ENV) --origin $(ORIGIN) $(ARGS) \
		$(if $(APPLY),--apply,)

# Deletes resources this tool created. Docker has no terminal to confirm, so APPLY=1 also
# needs CONFIRM=<envs> (e.g. CONFIRM=dev). Usage:
#   make run-undeploy [SERVICE=iam] [ENV=dev] [ARGS="--resource NAME"] [APPLY=1 CONFIRM=dev]
run-undeploy: docker-build
	$(DOCKER_RUN) undeploy $(SERVICE) --env $(ENV) $(ARGS) \
		$(if $(APPLY),--apply,) $(if $(CONFIRM),--confirm $(CONFIRM),)

# Compares the inventory with AWS and the definitions; APPLY=1 fixes the inventory only.
# Usage: make run-audit [ARGS="--service iam --env dev"] [APPLY=1]
run-audit: docker-build
	$(DOCKER_RUN) inventory audit $(ARGS) $(if $(APPLY),--apply,)

# Local web UI (src/webui/, docs/web-ui.md) on http://127.0.0.1:8501, with .env exported.
# Needs `pip install -r requirements-ui.txt`. It has no login: never bind it beyond localhost.
run-ui:
	set -a && . ./.env && set +a && \
		streamlit run src/webui/app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
