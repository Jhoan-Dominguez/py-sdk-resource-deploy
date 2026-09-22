"""Entry point: deploy this project's AWS resources via boto3.

Run from the repo root (AWS_ACCOUNT_ID, AWS_REGION, PROJECT_NAME and
TRUSTED_PRINCIPAL_USER are all required, see .env.template):
    AWS_ACCOUNT_ID=011221923990 AWS_REGION=us-east-1 PROJECT_NAME=sf-onboarding \
        TRUSTED_PRINCIPAL_USER=jhoan.dominguez python3 src/main.py iam --env dev
    AWS_ACCOUNT_ID=011221923990 AWS_REGION=us-east-1 PROJECT_NAME=sf-onboarding \
        TRUSTED_PRINCIPAL_USER=jhoan.dominguez python3 src/main.py iam --env dev --apply

`iam` is the only registered service today; the deploy logic itself lives in
src/deploy/ (see src/deploy/cli.py for the full option list and src/deploy/
services/ for how to add another service).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
