"""
AI generation service tests: the quota gate denies before any upstream spend
(and fails closed when Usage is down), the happy path streams token chunks in
order over SSE and persists prompt/response/token counts, completion emits
ai.generation_completed with the exact payload Usage's consumer expects,
upstream errors emit ai.generation_failed and mark the job failed, and an
in-flight stream can be cancelled by job_id (partial persisted, estimated
usage metered).

No infra: SQLite via conftest, fakeredis for the pub/sub fan-out, the
RabbitMQ publisher stubbed, the Usage quota check stubbed, and OpenRouter
fully mocked — no real network anywhere. AI_EAGER_WORKER=1 runs the worker
inline in POST, so SSE reads are deterministic replays of the Redis buffer.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import fakeredis
from sqlalchemy import select

from creditflow_common import jwt_utils

import store
import usage_client
import worker
from models import GenerationJob, PromptRecord


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "member", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _generate(client, account_id: str, prompt: str = "Write a haiku about credit",
              model: str = "fast", **auth_kwargs) -> dict:
    r = client.post("/generations", json={"prompt": prompt, "model": model},
                    headers=_auth(account_id, **auth_kwargs))
    assert r.status_code == 202, r.text
    return r.json()


def _sse_events(client, account_id: str, job_id: str) -> list[dict]:
    """GET the SSE stream and parse it into message dicts (in order)."""
    r = client.get(f"/generations/{job_id}/stream", headers=_auth(account_id))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    messages = []
    for block in r.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        payload = json.loads(lines["data"])
        assert payload["type"] == lines["event"]
        messages.append(payload)
    return messages


def _job(db, job_id: str) -> GenerationJob:
    db.expire_all()
    return db.get(GenerationJob, job_id)


def _make_job(db, account_id: str, status: str = "streaming",
              prompt: str = "Write a haiku", model: str = "openai/gpt-4o-mini") -> GenerationJob:
    job = GenerationJob(account_id=account_id, created_by_user_id=_uid(),
                        model=model, status=status, prompt=prompt)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# --------------------------------------------------------------------------
# Auth + model selection
# --------------------------------------------------------------------------

def test_generation_requires_auth(client):
    r = client.post("/generations", json={"prompt": "hi"})
    assert r.status_code == 401


def test_models_endpoint_lists_both_choices(client):
    r = client.get("/models", headers=_auth(_uid()))
    assert r.status_code == 200
    models = {m["alias"]: m["model"] for m in r.json()["models"]}
    assert set(models) == {"fast", "quality"}
    assert models["fast"] != models["quality"]


def test_unknown_model_rejected_before_quota_or_upstream(client, quota, openrouter_stream):
    r = client.post("/generations", json={"prompt": "hi", "model": "evil/model"},
                    headers=_auth(_uid()))
    assert r.status_code == 422
    assert quota["calls"] == []
    assert openrouter_stream["calls"] == []


def test_model_alias_resolves_and_full_id_accepted(client, db_session, openrouter_stream):
    account_id = _uid()
    body = _generate(client, account_id, model="quality")
    quality_id = _job(db_session, body["job_id"]).model
    assert openrouter_stream["calls"][0]["model"] == quality_id
    # The full OpenRouter id is accepted too (still allowlisted).
    body2 = _generate(client, account_id, model=quality_id)
    assert _job(db_session, body2["job_id"]).model == quality_id


# --------------------------------------------------------------------------
# Quota gate (synchronous check against the Usage service)
# --------------------------------------------------------------------------

def test_quota_denied_returns_429_and_spends_nothing(client, db_session, quota,
                                                     openrouter_stream, published_events):
    quota["allowed"] = False
    r = client.post("/generations", json={"prompt": "hi"}, headers=_auth(_uid()))
    assert r.status_code == 429
    assert r.json()["detail"]["remaining_tokens"] == 0
    assert openrouter_stream["calls"] == []          # no paid upstream call
    assert published_events == []                    # no events
    assert db_session.scalars(select(GenerationJob)).all() == []  # no job row


def test_quota_service_down_fails_closed_503(client, quota, openrouter_stream):
    quota["error"] = usage_client.QuotaCheckError("connection refused")
    r = client.post("/generations", json={"prompt": "hi"}, headers=_auth(_uid()))
    assert r.status_code == 503
    assert openrouter_stream["calls"] == []


def test_quota_checked_with_prompt_estimate(client, quota):
    prompt = "x" * 400  # ~100 tokens at 4 chars/token
    _generate(client, _uid(), prompt=prompt)
    assert quota["calls"] == [100]


# --------------------------------------------------------------------------
# Streaming happy path
# --------------------------------------------------------------------------

def test_happy_path_streams_chunks_in_order_then_done(client):
    account_id = _uid()
    body = _generate(client, account_id)
    messages = _sse_events(client, account_id, body["job_id"])
    assert [m["type"] for m in messages] == ["token", "token", "token", "done"]
    assert [m["content"] for m in messages[:-1]] == ["Hello", " ", "world"]
    assert [m["seq"] for m in messages] == [1, 2, 3, 4]
    done = messages[-1]
    assert done["total_tokens"] == 10
    assert done["usage_estimated"] is False


def test_stream_replay_is_reconnect_safe(client):
    account_id = _uid()
    body = _generate(client, account_id)
    first = _sse_events(client, account_id, body["job_id"])
    second = _sse_events(client, account_id, body["job_id"])  # fresh subscriber, same buffer
    assert first == second


def test_happy_path_persists_job_and_history(client, db_session):
    account_id = _uid()
    body = _generate(client, account_id, prompt="Write a haiku about credit")
    job = _job(db_session, body["job_id"])
    assert job.status == "completed"
    assert job.response == "Hello world"
    assert job.prompt == "Write a haiku about credit"
    assert (job.input_tokens, job.output_tokens, job.total_tokens) == (7, 3, 10)
    assert float(job.cost_usd) == 0.00012
    assert job.usage_estimated is False
    assert job.completed_at is not None
    history = db_session.scalars(select(PromptRecord).where(PromptRecord.job_id == job.id)).all()
    assert len(history) == 1
    assert history[0].prompt == job.prompt
    assert history[0].response == "Hello world"
    assert history[0].account_id == account_id


def test_generation_completed_event_matches_usage_contract(client, published_events):
    account_id = _uid()
    user_id = _uid()
    body = _generate(client, account_id, user_id=user_id)
    assert len(published_events) == 1
    routing_key, payload = published_events[0]
    assert routing_key == "ai.generation_completed"
    # Exactly the fields Usage's consumer records (job_id is its dedup key).
    assert payload["account_id"] == account_id
    assert payload["user_id"] == user_id
    assert payload["job_id"] == body["job_id"]
    assert payload["model"] == body["model"]
    assert payload["input_tokens"] == 7
    assert payload["output_tokens"] == 3
    assert payload["total_tokens"] == 10
    assert payload["cost_usd"] == 0.00012
    assert payload["status"] == "completed"


def test_job_status_endpoint_scoped_by_account(client, db_session):
    account_id = _uid()
    body = _generate(client, account_id)
    r = client.get(f"/generations/{body['job_id']}", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    # Another account sees 404 (not 403 — job ids must not leak existence),
    # on the status endpoint and the stream alike.
    other = _auth(_uid())
    assert client.get(f"/generations/{body['job_id']}", headers=other).status_code == 404
    assert client.get(f"/generations/{body['job_id']}/stream", headers=other).status_code == 404


# --------------------------------------------------------------------------
# Upstream failure
# --------------------------------------------------------------------------

def test_upstream_error_marks_failed_and_emits_generation_failed(
        client, db_session, openrouter_stream, published_events):
    async def _boom(model, prompt, max_tokens=None):
        yield ("token", "par")
        raise Exception("OpenRouter HTTP 502: upstream exploded")

    openrouter_stream["factory"] = _boom
    account_id = _uid()
    body = _generate(client, account_id)

    job = _job(db_session, body["job_id"])
    assert job.status == "failed"
    assert "502" in job.error_reason
    assert db_session.scalars(select(PromptRecord)).all() == []  # no history for failures

    assert [rk for rk, _ in published_events] == ["ai.generation_failed"]
    payload = published_events[0][1]
    assert payload["job_id"] == body["job_id"]
    assert payload["account_id"] == account_id
    assert "502" in payload["reason"]

    # The SSE stream ends with the error event after the partial token.
    messages = _sse_events(client, account_id, body["job_id"])
    assert [m["type"] for m in messages] == ["token", "error"]
    assert "502" in messages[-1]["reason"]


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

def test_cancel_endpoint_sets_flag_and_rejects_finished_jobs(client, db_session, fake_redis):
    account_id = _uid()
    job = _make_job(db_session, account_id, status="streaming")
    r = client.post(f"/generations/{job.id}/cancel", headers=_auth(account_id))
    assert r.status_code == 202
    inspect = fakeredis.FakeRedis(server=fake_redis, decode_responses=True)
    assert inspect.exists(store.CANCEL_KEY.format(job_id=job.id)) == 1

    done = _make_job(db_session, account_id, status="completed")
    assert client.post(f"/generations/{done.id}/cancel",
                       headers=_auth(account_id)).status_code == 409
    # Other accounts can't cancel it (404, existence not leaked).
    assert client.post(f"/generations/{job.id}/cancel", headers=_auth(_uid())).status_code == 404


def test_cancel_stops_stream_persists_partial_and_meters_estimate(
        client, db_session, openrouter_stream, published_events):
    account_id = _uid()
    job = _make_job(db_session, account_id, status="pending", prompt="Write a haiku")

    async def _cancelled_midway(model, prompt, max_tokens=None):
        yield ("token", "Hello")
        # A concurrent POST /generations/{id}/cancel lands here.
        await store.request_cancel(job.id)
        yield ("token", " world")   # worker must stop BEFORE publishing this
        yield ("token", " never")
        yield ("usage", {"input_tokens": 99, "output_tokens": 99, "total_tokens": 198, "cost_usd": 1.0})

    openrouter_stream["factory"] = _cancelled_midway
    # Drive the worker coroutine directly — the exact function POST schedules.
    asyncio.run(worker.run_generation(job.id, job.account_id, job.created_by_user_id,
                                      job.model, job.prompt, None))

    fresh = _job(db_session, job.id)
    assert fresh.status == "cancelled"
    assert fresh.response == "Hello"          # partial text persisted
    assert fresh.usage_estimated is True      # never reached provider usage
    assert fresh.total_tokens == fresh.input_tokens + fresh.output_tokens

    # Usage was really incurred upstream, so the completion event still fires,
    # flagged as cancelled + estimated.
    assert [rk for rk, _ in published_events] == ["ai.generation_completed"]
    payload = published_events[0][1]
    assert payload["status"] == "cancelled"
    assert payload["usage_estimated"] is True
    assert payload["job_id"] == job.id
    assert payload["cost_usd"] is None

    # The SSE stream ends with a `cancelled` terminal after the one real token.
    messages = _sse_events(client, account_id, job.id)
    assert [m["type"] for m in messages] == ["token", "cancelled"]
    assert messages[0]["content"] == "Hello"
