# CLAUDE.md

Project-specific guidance for Claude Code working in this repo.

## What this is

OpenAI-compatible HTTP API in front of AWS Bedrock. Lets clients use OpenAI SDKs unchanged (`base_url` swap + `Authorization: Bearer <api-key>`) while requests are translated to Bedrock's `Converse` / `ConverseStream` API. Multi-tenant: API keys live in Secrets Manager and identify the tenant.

Runtime is FastAPI on Python 3.12, deployed to ECS Fargate (ARM64/Graviton) behind ALB + CloudFront, in `ap-southeast-2`.

## Repo layout

```
app/
  main.py        # FastAPI routes, Bedrock translation, streaming
  auth.py        # Bearer auth + tenant key cache from Secrets Manager
tests/
  conftest.py    # shared fixtures: app_module, fake_bedrock, fake_secrets, client, auth_headers
  test_main.py   # routing + Bedrock translation
  test_auth.py   # auth + tenant identification
infra/           # CDK TypeScript: OidcStack + BedrockApiStack
.github/workflows/
  test.yml       # pytest + cdk synth
  deploy.yml     # buildx ARM64 → ECR → cdk deploy → ECS force redeploy
Dockerfile       # python:3.12-slim, ARM64-friendly, tini, non-root
run.sh           # ./run.sh <bedrock-model-id> [host] [port]
DEPLOYMENT.md    # one-time bootstrap + rollout instructions
```

## Local dev

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt boto3 pytest pytest-asyncio
./run.sh au.anthropic.claude-opus-4-6-v1   # Ctrl+G to quit (stty intr remap)
RELOAD=1 ./run.sh ...                       # opt in to autoreload (note: Ctrl+C may need 2 hits)
```

Required env at runtime:
- `BEDROCK_MODEL_ID` — Bedrock model id or inference profile id (Claude 4-family REQUIRES inference profile)
- `TENANT_KEYS_SECRET_ID` — name/ARN of the Secrets Manager secret holding `{"tenant": "key"}` JSON
- `AWS_REGION` and AWS creds in env (for boto3)
- Optional: `TENANT_KEYS_CACHE_TTL` (default 300s), `WEB_CONCURRENCY` (default 2)

## Tests

```bash
. .venv/bin/activate && python -m pytest tests/ -v
```

12 tests must pass before any merge. New behavior follows TDD: write the failing test first, watch it fail for the right reason, then minimal impl. The fixtures in `tests/conftest.py` reload `app.auth` and `app.main` so each test sees fresh module-level env reads — preserve that pattern when adding tests.

## Architecture rules

**Bedrock client is lazy.** `app/main.py:_bedrock` starts as `None`; `_get_client()` constructs on first use. Module load must never call `boto3.client(...)` at import time — the dev env's broken AWS profile config (or any creds glitch) would crash imports and break tests. Tests rely on this and patch `_bedrock` directly.

**Streaming offloads boto3 to threads.** boto3's `EventStream` is sync. The streaming generator is `async` and uses `await asyncio.to_thread(_next_event)` per event — never iterate `resp["stream"]` directly inside an async function or you'll block the event loop and Ctrl+C will hang. On `asyncio.CancelledError`, call `stream.close()` to unblock the worker.

**Non-streaming offloads `converse()` too.** `await asyncio.to_thread(_get_client().converse, ...)`. Don't reintroduce direct sync calls inside async handlers.

**Auth applies to every `/v1/*` route.** Use `tenant_id: str = Depends(require_tenant)`. The dependency reads `Authorization: Bearer <key>`, looks it up in the cached reverse map, attaches `request.state.tenant_id`, raises 401 on missing/invalid.

**OpenAI ↔ Bedrock translation lives in `app/main.py`** (`_to_bedrock_messages`, `_build_inference_config`, `_map_finish_reason`). Don't sprinkle translation logic into routes. System messages MUST be split out into the Bedrock `system` parameter — they don't go in `messages`.

**SSE format is plain `StreamingResponse`, not sse-starlette.** Manual `data: {json}\n\n` framing, ending with `data: [DONE]\n\n`. We removed sse-starlette intentionally (was double-formatting strings).

## Deployment rules

**ECS task is ARM64.** `runtimePlatform.cpuArchitecture = ecs.CpuArchitecture.ARM64`. Images are built with `docker buildx --platform linux/arm64`. Don't switch to x86_64 without intent — Graviton is ~20% cheaper and the base image (`python:3.12-slim`) is multi-arch.

**No NAT.** VPC is public-only, tasks have public IPs. Saves ~$30/mo.

**ALB defaults to internet-open; auth gates traffic.** To force CloudFront-only ingress, deploy with `--context cloudfrontPrefixListId=<id>` (region-specific `com.amazonaws.global.cloudfront.origin-facing` prefix list). Currently a documented hardening step, not enforced.

**CDK synth must work without AWS creds.** `infra/bin/app.ts` only sets `env` when `CDK_DEFAULT_ACCOUNT` is present, so CI synth gating works on PRs. Don't add `fromLookup`-style constructs without making them opt-in via context.

**GitHub Actions: pipe `${{ github.* }}` through `env:`** before using in `run:` commands — the security hook will block direct interpolation.

## Bedrock model IDs

Use **inference profile IDs**, not raw model IDs, for Claude 4-family. Examples:
- `au.anthropic.claude-opus-4-6-v1` (AU regions)
- `us.anthropic.claude-opus-4-6-v1` (US)
- `global.anthropic.claude-opus-4-6-v1` (cross-region global)

Raw IDs return: `ValidationException: ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile`.

List profiles: `aws bedrock list-inference-profiles --region <region>`.

## Tenant keys

Single Secrets Manager secret (default `bedrock-api/tenant-keys`) with JSON map: `{"tenant-id": "api-key", ...}`. App builds reverse lookup (key → tenant) cached for `TENANT_KEYS_CACHE_TTL` seconds (default 300). Rotate keys with:

```bash
aws secretsmanager put-secret-value --secret-id bedrock-api/tenant-keys --secret-string '{...}'
```

New keys propagate within ~5min without redeploy.

## What NOT to do

- Don't add a database, ORM, or session store — the service is stateless.
- Don't reintroduce `sse-starlette`. Plain `StreamingResponse` is the chosen pattern.
- Don't add `--reload` to the deployed CMD. It's local-dev opt-in only.
- Don't bypass auth for `/v1/models` "because it's just metadata" — OpenAI's API requires auth there too, and we want consistent tenant logging.
- Don't construct boto3 clients at module level. Always lazy.
- Don't add an Authorization-bearing health check probe. Use `/v1/models` accepting `200,401`.

## Memory

Project knowledge lives in MemPalace under `wing_bedrock_api`, rooms: `architecture`, `deployment`, `gotchas`, `testing`. Search with the `mempalace` MCP tools when context is missing.
