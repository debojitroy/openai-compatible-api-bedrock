import json
import os
import time
from typing import Any, Dict, Optional

import boto3
from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)

_secrets_client: Any = None
_cache: Dict[str, Any] = {"reverse": None, "fetched_at": 0.0}
_CACHE_TTL = float(os.environ.get("TENANT_KEYS_CACHE_TTL", "300"))


def _get_secrets_client() -> Any:
    global _secrets_client
    if _secrets_client is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        kwargs = {"region_name": region} if region else {}
        _secrets_client = boto3.client("secretsmanager", **kwargs)
    return _secrets_client


def _load_reverse_map() -> Dict[str, str]:
    """Return {api_key: tenant_id}, cached for TENANT_KEYS_CACHE_TTL seconds."""
    secret_id = os.environ.get("TENANT_KEYS_SECRET_ID")
    if not secret_id:
        raise HTTPException(
            status_code=500,
            detail="TENANT_KEYS_SECRET_ID is not configured.",
        )

    now = time.time()
    if _cache["reverse"] is not None and now - _cache["fetched_at"] < _CACHE_TTL:
        return _cache["reverse"]

    try:
        resp = _get_secrets_client().get_secret_value(SecretId=secret_id)
        raw = json.loads(resp["SecretString"])
        if not isinstance(raw, dict):
            raise ValueError("Tenant keys secret must be a JSON object")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load tenant keys: {e}",
        ) from e

    reverse = {str(v): str(k) for k, v in raw.items()}
    _cache["reverse"] = reverse
    _cache["fetched_at"] = now
    return reverse


async def require_tenant(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> str:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    keys = _load_reverse_map()
    tenant_id = keys.get(creds.credentials)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    request.state.tenant_id = tenant_id
    return tenant_id
