"""Environment configuration required by the deploy tool.

All variables are listed in .env.template; export them (or use --env-file
with Docker, see the Dockerfile) before running src/main.py.
"""

from __future__ import annotations

import os


class MissingEnvVarError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvVarError(
            f"The {name} environment variable is required (see .env.template)."
        )
    return value


def aws_account_id() -> str:
    """AWS account id these resources are scoped to."""
    return _require_env("AWS_ACCOUNT_ID")


def aws_region() -> str:
    """AWS region to create boto3 clients in."""
    return _require_env("AWS_REGION")


def project_name() -> str:
    """Prefix used to name every resource this project creates (roles, policies, ...)."""
    return _require_env("PROJECT_NAME")


def trusted_principal_user() -> str:
    """IAM user name allowed to assume the roles src/deploy/services/iam.py creates."""
    return _require_env("TRUSTED_PRINCIPAL_USER")


def inventory_table() -> str:
    """Name of the DynamoDB table that holds the deployment inventory."""
    return _require_env("INVENTORY_TABLE")


def inventory_table_mode() -> str:
    """`create` (default): the tool creates the table if missing. `existing`: never create it."""
    return os.environ.get("INVENTORY_TABLE_MODE") or "create"
