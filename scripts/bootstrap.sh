#!/usr/bin/env bash
#
# One-time AWS bootstrap for the Bedrock-fronting API.
# Idempotent: safe to re-run; each step skips if already done.
#
# Usage:
#   ./scripts/bootstrap.sh \
#       --github-org <gh-org-or-user> \
#       --github-repo <gh-repo-name> \
#       [--region ap-southeast-2] \
#       [--model-id au.anthropic.claude-opus-4-6-v1] \
#       [--profile <aws-profile>]
#
# Requires: aws cli, docker (with buildx), node + npm, jq.

set -euo pipefail

# ---- defaults ---------------------------------------------------------------
REGION="ap-southeast-2"
MODEL_ID="au.anthropic.claude-opus-4-6-v1"
GH_ORG=""
GH_REPO=""
AWS_PROFILE_ARG=""

# ---- arg parsing ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --github-org)  GH_ORG="$2"; shift 2 ;;
    --github-repo) GH_REPO="$2"; shift 2 ;;
    --region)      REGION="$2"; shift 2 ;;
    --model-id)    MODEL_ID="$2"; shift 2 ;;
    --profile)     export AWS_PROFILE="$2"; AWS_PROFILE_ARG="--profile $2"; shift 2 ;;
    -h|--help)
      sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$GH_ORG" || -z "$GH_REPO" ]]; then
  echo "Error: --github-org and --github-repo are required." >&2
  echo "Run with --help for usage." >&2
  exit 2
fi

# Pin region for every downstream tool (aws cli, boto3, cdk) so bootstrap
# and deploy can't drift onto whatever the profile's default region is.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_REGION="$REGION"

# ---- helpers ----------------------------------------------------------------
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing required tool: $1" >&2; exit 1; }; }

for tool in aws docker node npm jq; do need "$tool"; done

# ---- preflight: AWS identity ------------------------------------------------
say "Checking AWS credentials"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
WHO=$(aws sts get-caller-identity --query Arn --output text)
echo "Account: $ACCOUNT"
echo "Identity: $WHO"
echo "Region:   $REGION"
echo "Model:    $MODEL_ID"
echo "GitHub:   $GH_ORG/$GH_REPO"

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/infra"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- step 1: cdk bootstrap (idempotent) -------------------------------------
say "Step 1/5: cdk bootstrap (idempotent)"
cd "$INFRA_DIR"
[[ -d node_modules ]] || npm ci
npx cdk bootstrap "aws://$ACCOUNT/$REGION"  --context githubOrg="$GH_ORG" --context githubRepo="$GH_REPO"

# ---- step 2: deploy OidcStack (creates ECR + GitHub OIDC role) --------------
say "Step 2/5: cdk deploy BedrockApiOidc"
npx cdk deploy BedrockApiOidc \
  --require-approval never \
  --context githubOrg="$GH_ORG" \
  --context githubRepo="$GH_REPO" \
  --outputs-file ./cdk.out/oidc-outputs.json

DEPLOY_ROLE_ARN=$(jq -r '.BedrockApiOidc.DeployRoleArn' ./cdk.out/oidc-outputs.json)
ECR_REPO_URI=$(jq -r '.BedrockApiOidc.EcrRepoUri' ./cdk.out/oidc-outputs.json)
echo "DeployRoleArn: $DEPLOY_ROLE_ARN"
echo "EcrRepoUri:    $ECR_REPO_URI"

# ---- step 3: build & push first image (ARM64) -------------------------------
say "Step 3/5: build & push initial ARM64 image"
cd "$PROJECT_DIR"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

IMAGE_TAG_LATEST="${ECR_REPO_URI}:latest"
IMAGE_TAG_STAMPED="${ECR_REPO_URI}:bootstrap-$(date +%Y%m%d%H%M%S)"

# docker may be a real docker, or podman pretending to be docker (via the
# podman-docker shim — `docker --version` mimics docker's output, so we
# feature-detect on buildx instead).
if docker buildx version >/dev/null 2>&1; then
  # Ensure buildx is set up (idempotent)
  if ! docker buildx ls | grep -q '\*'; then
    docker buildx create --use --name bedrock-api-builder
  fi
  docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    -t "$IMAGE_TAG_LATEST" \
    -t "$IMAGE_TAG_STAMPED" \
    --push \
    .
else
  say "buildx not available (looks like podman); using plain build + push"
  docker build \
    --platform linux/arm64 \
    -t "$IMAGE_TAG_LATEST" \
    -t "$IMAGE_TAG_STAMPED" \
    .
  docker push "$IMAGE_TAG_LATEST"
  docker push "$IMAGE_TAG_STAMPED"
fi

# ---- step 4: deploy BedrockApi stack ---------------------------------------
say "Step 4/5: cdk deploy BedrockApi"
cd "$INFRA_DIR"
npx cdk deploy BedrockApi \
  --require-approval never \
  --context githubOrg="$GH_ORG" \
  --context githubRepo="$GH_REPO" \
  --context modelId="$MODEL_ID" \
  --outputs-file ./cdk.out/app-outputs.json

CF_DOMAIN=$(jq -r '.BedrockApi.CloudFrontDomain' ./cdk.out/app-outputs.json)
SECRET_ARN=$(jq -r '.BedrockApi.TenantKeysSecretArn' ./cdk.out/app-outputs.json)

# ---- step 5: hand off to user ----------------------------------------------
say "Step 5/5: Done. Next steps:"
cat <<EOF

  1. Populate tenant API keys (replace with real keys):
       aws secretsmanager put-secret-value \\
         --region $REGION \\
         --secret-id bedrock-api/tenant-keys \\
         --secret-string '{"acme":"sk-acme-CHANGE-ME","globex":"sk-globex-CHANGE-ME"}'

  2. Smoke test (after keys are populated, ~30s for ECS task to be healthy):
       curl -sS https://$CF_DOMAIN/v1/models -H 'Authorization: Bearer sk-acme-CHANGE-ME'

  3. Configure GitHub Actions:
       Settings -> Secrets and variables -> Actions -> New repository secret:
         Name:  AWS_ROLE_TO_ASSUME
         Value: $DEPLOY_ROLE_ARN

  4. Push to main. The Deploy workflow will rebuild + redeploy on each push.

CloudFront domain: https://$CF_DOMAIN
Tenant keys secret: $SECRET_ARN

EOF
