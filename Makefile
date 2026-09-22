
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