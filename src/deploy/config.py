"""Environment configuration required by every service in src/deploy/services/.

All variables are listed in .env.template; export them (or use --env-file
with Docker, see the Dockerfile) before running src/main.py.
"""

from __future__ import annotations

import os


class MissingEnvVarError(RuntimeError):
    pass


def _require_env(name: str, example: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvVarError(
            f"The {name} environment variable is required (e.g. {name}={example})."
        )
    return value


def aws_account_id() -> str:
    """AWS account id these resources are scoped to."""
    return _require_env("AWS_ACCOUNT_ID", None)


def aws_region() -> str:
    """AWS region to create boto3 clients in."""
    return _require_env("AWS_REGION", None)


def project_name() -> str:
    """Prefix used to name every resource this project creates (roles, policies, ...)."""
    return _require_env("PROJECT_NAME", None)


def trusted_principal_user() -> str:
    """IAM user name allowed to assume the roles src/deploy/services/iam.py creates."""
    return _require_env("TRUSTED_PRINCIPAL_USER", None)
