# Deployment Guide

This stack deploys the OpenAI-compatible Bedrock API on AWS ECS Fargate (Graviton/ARM64) behind an ALB and CloudFront, with multi-tenant API keys in Secrets Manager. CI/CD via GitHub Actions using OIDC (no long-lived AWS keys).

## Architecture

```
Client ──HTTPS──> CloudFront ──HTTP──> ALB ──HTTP──> ECS Fargate (ARM64) ──> Bedrock
                                                          │
                                                          └──> Secrets Manager (tenant keys)
```

## One-time bootstrap

Use the bootstrap script (recommended):

```bash
./scripts/bootstrap.sh \
  --github-org <your-github-org-or-user> \
  --github-repo <your-repo-name>
# optional flags: --region ap-southeast-2 --model-id au.anthropic.claude-opus-4-6-v1 --profile <aws-profile>
```

The script is idempotent and runs:

1. `cdk bootstrap` for the target account/region
2. `cdk deploy BedrockApiOidc` — creates the ECR repo + GitHub OIDC role
3. `docker buildx --platform linux/arm64 --push` of the initial image
4. `cdk deploy BedrockApi` — VPC, ECS, ALB, CloudFront, Secrets Manager
5. Prints the `DeployRoleArn`, CloudFront domain, and the `aws secretsmanager put-secret-value` command for populating tenant keys

Requires: `aws`, `docker` (with buildx), `node`, `npm`, `jq` on `PATH`, plus AWS credentials with admin or equivalent on the target account.

### Or step-by-step manually

```bash
cd infra

# 1. CDK bootstrap (creates the cdk-* roles the GitHub deploy role assumes)
npx cdk bootstrap aws://<ACCOUNT_ID>/ap-southeast-2

# 2. OIDC + ECR (creates the ECR repo and GitHub Actions deploy role)
npx cdk deploy BedrockApiOidc \
  --context githubOrg=<your-github-org-or-user> \
  --context githubRepo=<your-repo-name>
# Capture DeployRoleArn and EcrRepoUri from the outputs.

# 3. Build and push the first image (ARM64)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-southeast-2
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker buildx build --platform linux/arm64 --push \
  -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/bedrock-api:latest ..

# 4. App stack
npx cdk deploy BedrockApi \
  --context githubOrg=<your-github-org-or-user> \
  --context githubRepo=<your-repo-name>
```

## GitHub repository configuration

Set these in **Settings → Secrets and variables → Actions**:

| Type   | Name                  | Value                                   |
|--------|-----------------------|-----------------------------------------|
| Secret | `AWS_ROLE_TO_ASSUME`  | The `DeployRoleArn` output from step 2  |

## First deploy

Push to `main` (or run **Actions → Deploy → Run workflow**). The workflow will:

1. Run pytest + `cdk synth`.
2. Build a `linux/arm64` image via QEMU and push to ECR.
3. `cdk deploy BedrockApi`.
4. Force-redeploy the ECS service to pick up the new image tag.

## Populate tenant API keys

The `bedrock-api/tenant-keys` secret is created with `{}`. Populate it via console or CLI:

```bash
aws secretsmanager put-secret-value \
  --region ap-southeast-2 \
  --secret-id bedrock-api/tenant-keys \
  --secret-string '{"acme":"sk-acme-very-secret","globex":"sk-globex-very-secret"}'
```

Keys are cached in-process for `TENANT_KEYS_CACHE_TTL` seconds (default 300). New keys propagate within ~5 minutes without a redeploy.

## Test the deployment

Get the CloudFront domain from the stack outputs:

```bash
aws cloudformation describe-stacks --stack-name BedrockApi \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomain'].OutputValue" --output text
```

```bash
DOMAIN=<dxxxxxxxx.cloudfront.net>
KEY=sk-acme-very-secret

curl -sS "https://$DOMAIN/v1/models" -H "Authorization: Bearer $KEY"

curl -sS "https://$DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}'
```

## Configurable bits (CDK context)

| Context key                  | Default                                 | Notes                                                    |
|------------------------------|-----------------------------------------|----------------------------------------------------------|
| `region`                     | `ap-southeast-2`                        | Deploy region                                            |
| `modelId`                    | `au.anthropic.claude-opus-4-6-v1`       | Bedrock model id or inference profile                    |
| `cloudfrontPrefixListId`     | (unset; ALB open to internet)           | Set to lock ALB ingress to CloudFront only               |

Pass via CLI: `npx cdk deploy ... --context modelId=us.anthropic.claude-...`.

## Hardening checklist (post-launch)

- [ ] Set `cloudfrontPrefixListId` so the ALB only accepts traffic from CloudFront. Look up `com.amazonaws.global.cloudfront.origin-facing` for your region.
- [ ] Enable AWS WAF on the CloudFront distribution for rate limiting and bot protection.
- [ ] Add CloudWatch alarms for ECS task failures, ALB 5xx rate, and Bedrock throttling.
- [ ] Enable Bedrock model invocation logging.
- [ ] Rotate tenant keys via `put-secret-value`; the app re-reads within `TENANT_KEYS_CACHE_TTL`.

## Cost notes

Approx steady-state cost (1 task, no traffic) in ap-southeast-2:
- 1× Fargate ARM64 task (0.5 vCPU / 1 GB): ~$10/mo
- ALB: ~$22/mo
- CloudFront: pay per request (~$0 idle)
- Secrets Manager: $0.40 per secret/mo
- ECR: $0.10/GB/mo
- No NAT (public subnets only)

Total floor: ~**$35/mo** before traffic. Bedrock token costs are separate and dominate at any real volume.
