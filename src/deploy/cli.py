"""CLI to deploy this project's AWS resources via boto3 (not Terraform).

Usage (from the repo root, via the src/main.py entry point):
    python3 src/main.py iam --env dev                 # dry-run: no write APIs are called
    python3 src/main.py iam --env dev --apply          # applies the changes to AWS
    python3 src/main.py iam --env dev,mock,stg,prod    # several environments in one run

To add a new service (s3, dynamodb, ...): create `services/<name>.py` with a
class implementing `ServiceDeployer` (see services/base.py and the example in
services/iam.py) and register it in SERVICES below.
"""

from __future__ import annotations

import argparse
import sys

from .services.base import ServiceDeployer
from .services.iam import IamService

SERVICES: dict[str, type[ServiceDeployer]] = {
    "iam": IamService,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("service", choices=sorted(SERVICES), help="Which service to deploy")
    parser.add_argument(
        "--env", required=True, help="Comma-separated environment(s): dev, mock, stg, prod"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Without this flag only the plan is shown (dry-run)"
    )
    args = parser.parse_args(argv)

    deployer = SERVICES[args.service]()
    for environment in (e.strip() for e in args.env.split(",")):
        mode = "apply" if args.apply else "dry-run"
        print(f"\n=== {args.service} / {environment} ({mode}) ===")
        deployer.deploy(environment=environment, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
