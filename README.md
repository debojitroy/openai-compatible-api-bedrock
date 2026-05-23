# OpenAI-Compatible API for AWS Bedrock

A drop-in OpenAI-compatible HTTP layer in front of AWS Bedrock. Point any OpenAI SDK at it and it routes chat completions through Bedrock's `Converse` / `ConverseStream` API. Multi-tenant via API keys stored in AWS Secrets Manager.

## Features

- `/v1/chat/completions` — non-streaming and SSE streaming, OpenAI-shaped requests/responses
- `/v1/completions` — legacy text completions (translated to chat under the hood)
- `/v1/models` — returns the configured Bedrock model id
- Multi-tenant API keys (`Authorization: Bearer <key>`) sourced from a single Secrets Manager secret, cached in-process
- Real token usage from Bedrock (`prompt_tokens` / `completion_tokens` / `total_tokens`)
- `finish_reason` mapped from Bedrock stop reasons (`end_turn`, `max_tokens`, `tool_use`, content/guardrail filters)
- Production-ready async streaming — boto3's sync iterator is offloaded to threads, so the event loop stays responsive and shutdown is prompt
- ARM64/Graviton container, deployed via AWS CDK to ECS Fargate behind ALB + CloudFront

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Set the required env vars and run:

```bash
export BEDROCK_MODEL_ID=au.anthropic.claude-opus-4-6-v1   # or any inference profile / model ID
export TENANT_KEYS_SECRET_ID=bedrock-api/tenant-keys      # JSON {"tenant": "key"} in Secrets Manager
export AWS_REGION=ap-southeast-2
# AWS credentials must be in env (env vars, profile, instance role, etc.)

./run.sh "$BEDROCK_MODEL_ID"
```

Press **Ctrl+G** to quit (Ctrl+C is intentionally remapped — see `run.sh`). To enable autoreload during development: `RELOAD=1 ./run.sh "$BEDROCK_MODEL_ID"`.

> **Note**: Claude 4-family models on Bedrock require a cross-region inference profile (`au.`, `us.`, `eu.`, `apac.`, or `global.` prefix), not the raw model ID. Use `aws bedrock list-inference-profiles --region <region>` to discover available profiles.

## Configuration

| Env var                    | Required | Default | Notes                                                     |
|----------------------------|----------|---------|-----------------------------------------------------------|
| `BEDROCK_MODEL_ID`         | yes      | —       | Bedrock model id or inference profile id                  |
| `TENANT_KEYS_SECRET_ID`    | yes      | —       | Secrets Manager secret name/ARN (JSON map)                |
| `AWS_REGION`               | yes      | —       | Bedrock + Secrets Manager region                          |
| `TENANT_KEYS_CACHE_TTL`    | no       | `300`   | Seconds to cache the tenant key map                       |
| `WEB_CONCURRENCY`          | no       | `2`     | uvicorn worker count (container only)                     |

The tenant keys secret holds a JSON object mapping tenant id to API key:

```json
{
  "acme": "sk-acme-very-secret",
  "globex": "sk-globex-very-secret"
}
```

Rotate keys without redeploying:

```bash
aws secretsmanager put-secret-value \
  --secret-id bedrock-api/tenant-keys \
  --secret-string '{"acme":"sk-new-key","globex":"sk-globex-very-secret"}'
```

New values take effect within `TENANT_KEYS_CACHE_TTL` seconds.

## Using it from an OpenAI client

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<your-cloudfront-domain>/v1",
    api_key="sk-acme-very-secret",  # one of the keys from the secret
)

resp = client.chat.completions.create(
    model="anything",  # ignored — server uses BEDROCK_MODEL_ID
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)

for chunk in client.chat.completions.create(
    model="anything",
    messages=[{"role": "user", "content": "Stream me a haiku."}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

The `model` field on the request is ignored — the server uses whatever `BEDROCK_MODEL_ID` it was started with. `/v1/models` returns that single configured model.

### curl

```bash
DOMAIN=<your-host-or-cloudfront-domain>
KEY=sk-acme-very-secret

# Models
curl -sS "https://$DOMAIN/v1/models" -H "Authorization: Bearer $KEY"

# Non-streaming chat
curl -sS "https://$DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hi in 5 words"}],"model":"x"}'

# Streaming
curl -N "https://$DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Count to 5"}],"model":"x","stream":true}'
```

## Tests

```bash
. .venv/bin/activate && python -m pytest tests/ -v
```

12 tests cover OpenAI ↔ Bedrock translation (system message split, content blocks, finish_reason mapping, streaming SSE) and auth (missing/invalid/valid key, /v1/models gating, key cache, missing secret env).

## API docs

When the server is running locally, FastAPI serves:
- Swagger UI at `/docs`
- ReDoc at `/redoc`

(Both endpoints are unauthenticated — they only describe the schema.)

## Deployment

Deployment to AWS (ECS Fargate ARM64 + ALB + CloudFront, multi-tenant Secrets Manager, GitHub Actions OIDC) is fully scripted in `infra/` (CDK TypeScript) and `.github/workflows/`.

See [DEPLOYMENT.md](./DEPLOYMENT.md) for the one-time bootstrap, GitHub secret config, and rollout steps.

## Project structure

```
app/
  main.py            FastAPI routes, Bedrock translation, async streaming
  auth.py            Bearer auth + tenant key map cached from Secrets Manager
tests/
  conftest.py        Shared fixtures (app reload, fake bedrock, fake secrets, client)
  test_main.py       Routing + Bedrock translation
  test_auth.py       Auth + tenant identification
infra/               CDK: OidcStack (GitHub OIDC role) + BedrockApiStack
.github/workflows/   test.yml + deploy.yml (OIDC, buildx ARM64, ECR, cdk deploy)
Dockerfile           python:3.12-slim, tini, non-root, multi-arch base
run.sh               ./run.sh <model-id> [host] [port] — Ctrl+G to quit
DEPLOYMENT.md        AWS bootstrap + rollout
CLAUDE.md            Conventions for AI agents working in this repo
```

## License

MIT (or whatever you choose — set the LICENSE file).
