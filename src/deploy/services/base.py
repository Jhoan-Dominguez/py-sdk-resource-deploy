"""Common interface for deploying one AWS service via boto3.

Each service (iam now; s3, dynamodb, etc. later) implements this interface
and gets registered in `deploy.cli.SERVICES` to become selectable on the CLI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ServiceDeployer(ABC):
    name: str

    @abstractmethod
    def deploy(self, environment: str, apply: bool) -> None:
        """Print what would change in `environment`; if `apply` is True, also apply it to AWS."""
