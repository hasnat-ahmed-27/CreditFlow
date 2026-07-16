"""
OpenRouter streaming chat-completions client.

One async generator, `stream_chat`, yields ("token", text) for every content
delta and — once the upstream stream finishes — a single ("usage", {...})
item with provider-reported token counts and USD cost (we request
`usage: {"include": true}` so OpenRouter appends its usage accounting to the
final SSE chunk). Callers that stop early (cancellation) simply never see
the usage item.

Model catalogue (spec: at least 2 user-selectable choices — one fast/cheap,
one higher quality). Aliases keep the API stable while the concrete models
stay env-swappable per deployment.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

from creditflow_common.config import env

OPENROUTER_BASE_URL = env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
UPSTREAM_TIMEOUT_SECONDS = float(env("AI_UPSTREAM_TIMEOUT_SECONDS", "120"))

MODELS: dict[str, str] = {
    "fast": env("AI_MODEL_FAST", "openai/gpt-4o-mini"),
    "quality": env("AI_MODEL_QUALITY", "anthropic/claude-sonnet-4"),
}


class OpenRouterError(Exception):
    """Upstream call failed — HTTP error, malformed stream, or in-band error chunk."""


def resolve_model(requested: str) -> str | None:
    """Alias or exact allowlisted id -> full OpenRouter model id; None if the
    model isn't one we offer."""
    if requested in MODELS:
        return MODELS[requested]
    if requested in MODELS.values():
        return requested
    return None


def _api_key() -> str:
    # Read at call time (not import) so tests and the build-time import smoke
    # test never require the secret.
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise OpenRouterError("OPENROUTER_API_KEY is not configured")
    return key


async def stream_chat(model: str, prompt: str,
                      max_tokens: int | None = None) -> AsyncIterator[tuple[str, object]]:
    """Yield ("token", str) per delta, then ("usage", dict) after the stream ends."""
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "usage": {"include": True},  # cost + token counts on the final chunk
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "X-Title": "CreditFlow",
    }
    usage: dict | None = None
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with httpx.AsyncClient(base_url=OPENROUTER_BASE_URL, timeout=timeout) as client:
            async with client.stream("POST", "/chat/completions", json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise OpenRouterError(f"OpenRouter HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # keep-alive comments (": OPENROUTER PROCESSING") etc.
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        # Mid-stream failures arrive in-band with a 200 status.
                        raise OpenRouterError(str(chunk["error"].get("message") or chunk["error"]))
                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}).get("content") if choices else None
                    if delta:
                        yield ("token", delta)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OpenRouterError(f"OpenRouter stream failed: {exc}") from exc
    if usage is not None:
        yield ("usage", {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost_usd": usage.get("cost"),
        })
