# Runs src/main.py (the boto3 deploy CLI) inside a container instead of locally.
#
# Build:
#   docker build -t tf-resource-deploy .
#
# Run (dry-run by default; add --apply as an extra arg to write to AWS):
#   docker run --rm \
#     --env-file .env \
#     -v ~/.aws:/root/.aws:ro \
#     tf-resource-deploy iam --env dev
#
# .env must set AWS_REGION, AWS_ACCOUNT_ID, PROJECT_NAME and TRUSTED_PRINCIPAL_USER
# (see .env.template).
#
# AWS credentials: this image ships no credentials. Either mount ~/.aws read-only
# (as above, picked up by boto3's default credential chain) or pass temporary
# creds as env vars instead (-e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=...
# -e AWS_SESSION_TOKEN=...), e.g. from `make authenticate_aws`.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

ENTRYPOINT ["python3", "src/main.py"]
