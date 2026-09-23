"""Entry point: deploy this project's AWS resources via boto3.

Run from the repo root with the variables from .env.template exported:
    python3 src/main.py deploy iam --env dev             # dry-run
    python3 src/main.py deploy iam --env dev --apply
    python3 src/main.py inventory list

`iam` is the only registered service today; the logic lives in src/deploy/
(see src/deploy/cli.py for the full option list) and the user guide in docs/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
