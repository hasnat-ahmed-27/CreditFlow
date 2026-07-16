"""
Scheduler service tests: every route requires a valid access token, all data
is tenant-scoped (cross-account access answers 404 and lists never leak),
calendar mutations need a publish role while any member can read, schedule
creation validates content against the read model (exists, ours, approved),
the due-scan fires one-off schedules exactly once (Redis lock AND DB guard),
recurring schedules re-arm to the next wall-clock occurrence (DST-safe,
month-end-clamped) and stay pending until cancelled, firing emits
content.scheduled with the documented payload, and the content_events
consumer maintains the read model idempotently and cancels schedules for
deleted content.

No infra: SQLite via conftest, Celery fully eager (tasks.scan_due_schedules /
tasks.fire_schedule called as plain functions — the exact code beat and the
worker would run), fire lock on fakeredis, publisher stubbed,
consumer.handle_event called directly (the exact function the broker would).
"Due" schedules are made by creating them normally (future publish_at) and
then backdating the row — the API refuses past times by design.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import recurrence
import tasks
from models import ContentRef, ScheduledPost, as_utc, utcnow


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues.
    Default role is owner: most calendar mutations require a publish role."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _content_event(account_id: str, content_id: str | None = None, status: str = "approved",
                   title: str = "Launch post", image_url: str | None = None,
                   version: int = 1) -> tuple[dict, str]:
    """(payload, event_id) shaped like the Content service's event_payload."""
    payload = {
        "content_id": content_id or _uid(),
        "account_id": account_id,
        "status": status,
        "version": version,
        "title": title,
        "generation_job_id": None,
        "image_url": image_url,
    }
    return payload, f"evt_{uuid.uuid4().hex}"


def _seed_content(account_id: str, status: str = "approved", **kw) -> str:
    """Feed a content.* event through the real consumer; returns content_id."""
    payload, event_id = _content_event(account_id, status=status, **kw)
    key = "content.created" if status == "draft" else "content.approved"
    consumer.handle_event(key, payload, event_id)
    return payload["content_id"]


def _future() -> str:
    return (utcnow() + timedelta(days=1)).isoformat()


def _create(client, account_id: str, content_id: str, publish_at: str | None = None,
            tz: str = "UTC", recurrence_: str | None = None, role: str = "owner") -> dict:
    r = client.post("/schedules", json={
        "content_id": content_id,
        "publish_at": publish_at or _future(),
        "timezone": tz,
        "recurrence": recurrence_,
    }, headers=_auth(account_id, role=role))
    assert r.status_code == 201, r.text
    return r.json()


def _backdate(db, schedule_id: str, minutes: int = 5) -> datetime:
    """Make a schedule due NOW (the API refuses past times); returns the due
    occurrence exactly as the scan will see it."""
    row = db.get(ScheduledPost, schedule_id)
    row.publish_at = utcnow() - timedelta(minutes=minutes)
    db.commit()
    db.refresh(row)
    return as_utc(row.publish_at)


def _row(db, schedule_id: str) -> ScheduledPost:
    db.expire_all()
    return db.get(ScheduledPost, schedule_id)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_every_route_requires_auth(client):
    sid = _uid()
    window = f"start={_future()}&end={(utcnow() + timedelta(days=2)).isoformat()}"
    for method, path in [
        ("post", "/schedules"),
        ("get", "/schedules"),
        ("get", f"/schedules/calendar?{window}"),
        ("get", f"/schedules/{sid}"),
        ("patch", f"/schedules/{sid}"),
        ("delete", f"/schedules/{sid}"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_refresh_token_is_rejected(client):
    token, _ = jwt_utils.sign_refresh_token(_uid(), _uid())
    r = client.get("/schedules", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

def test_create_returns_pending_schedule(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    item = _create(client, account_id, content_id, recurrence_="weekly")
    assert item["status"] == "pending"
    assert item["content_id"] == content_id
    assert item["account_id"] == account_id
    assert item["recurrence"] == "weekly"
    assert item["timezone"] == "UTC"
    assert item["fire_count"] == 0
    assert item["last_fired_at"] is None
    assert item["created_by_user_id"]

    r = client.get(f"/schedules/{item['schedule_id']}", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json() == item


def test_naive_publish_at_is_interpreted_in_the_given_timezone(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    # 9am New York in January is EST (UTC-5) -> stored as 14:00 UTC.
    item = _create(client, account_id, content_id,
                   publish_at="2031-01-15T09:00:00", tz="America/New_York")
    assert item["publish_at"] == "2031-01-15T14:00:00+00:00"
    assert item["timezone"] == "America/New_York"


def test_aware_publish_at_is_converted_to_utc(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    item = _create(client, account_id, content_id, publish_at="2031-01-15T09:00:00+05:00")
    assert item["publish_at"] == "2031-01-15T04:00:00+00:00"


def test_create_unknown_content_is_404(client):
    account_id = _uid()
    r = client.post("/schedules", json={"content_id": _uid(), "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 404


def test_create_requires_approved_content(client):
    account_id = _uid()
    draft = _seed_content(account_id, status="draft")
    r = client.post("/schedules", json={"content_id": draft, "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 409, r.text

    published = _seed_content(account_id, status="published")
    r = client.post("/schedules", json={"content_id": published, "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 409, r.text


def test_create_rejects_past_publish_at(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    past = (utcnow() - timedelta(minutes=1)).isoformat()
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": past},
                    headers=_auth(account_id))
    assert r.status_code == 422


def test_create_rejects_bad_timezone_and_recurrence(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future(),
                                        "timezone": "Mars/Olympus_Mons"},
                    headers=_auth(account_id))
    assert r.status_code == 422
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future(),
                                        "recurrence": "hourly"},
                    headers=_auth(account_id))
    assert r.status_code == 422


def test_members_read_but_cannot_mutate(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    item = _create(client, account_id, content_id)  # owner
    sid = item["schedule_id"]
    member = _auth(account_id, role="member")

    # Reads are any member's business.
    assert client.get("/schedules", headers=member).status_code == 200
    assert client.get(f"/schedules/{sid}", headers=member).status_code == 200

    # Mutations need a publish role — same gate as Content's status machine.
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=member)
    assert r.status_code == 403
    assert client.patch(f"/schedules/{sid}", json={"publish_at": _future()},
                        headers=member).status_code == 403
    assert client.delete(f"/schedules/{sid}", headers=member).status_code == 403

    # admin passes the same gate.
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=_auth(account_id, role="admin"))
    assert r.status_code == 201


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------

def test_cross_tenant_access_is_404(client, db_session):
    account_id = _uid()
    content_id = _seed_content(account_id)
    sid = _create(client, account_id, content_id)["schedule_id"]
    other = _auth(_uid())  # owner role in ANOTHER account
    assert client.get(f"/schedules/{sid}", headers=other).status_code == 404
    assert client.patch(f"/schedules/{sid}", json={"publish_at": _future()},
                        headers=other).status_code == 404
    assert client.delete(f"/schedules/{sid}", headers=other).status_code == 404
    # And the original is untouched.
    assert _row(db_session, sid).status == "pending"


def test_cannot_schedule_another_accounts_content(client):
    content_id = _seed_content(_uid())  # belongs to someone else
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=_auth(_uid()))
    assert r.status_code == 404


def test_list_never_leaks_other_accounts(client):
    account_a, account_b = _uid(), _uid()
    mine = _create(client, account_a, _seed_content(account_a))
    _create(client, account_b, _seed_content(account_b))
    data = client.get("/schedules", headers=_auth(account_a)).json()
    assert data["total"] == 1
    assert [i["schedule_id"] for i in data["items"]] == [mine["schedule_id"]]


# --------------------------------------------------------------------------
# Listing + calendar
# --------------------------------------------------------------------------

def test_list_paginates_and_filters_by_status(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    ids = {_create(client, account_id, content_id)["schedule_id"] for _ in range(3)}
    cancelled = _create(client, account_id, content_id)["schedule_id"]
    client.delete(f"/schedules/{cancelled}", headers=_auth(account_id))

    page1 = client.get("/schedules?limit=2&offset=0", headers=_auth(account_id)).json()
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    page2 = client.get("/schedules?limit=2&offset=2", headers=_auth(account_id)).json()
    assert len(page2["items"]) == 2

    pending = client.get("/schedules?status=pending", headers=_auth(account_id)).json()
    assert {i["schedule_id"] for i in pending["items"]} == ids
    got = client.get("/schedules?status=cancelled", headers=_auth(account_id)).json()
    assert [i["schedule_id"] for i in got["items"]] == [cancelled]

    assert client.get("/schedules?status=bogus", headers=_auth(account_id)).status_code == 422


def test_list_filters_by_content_id(client):
    account_id = _uid()
    content_a = _seed_content(account_id)
    content_b = _seed_content(account_id)
    on_a = _create(client, account_id, content_a)["schedule_id"]
    _create(client, account_id, content_b)
    got = client.get(f"/schedules?content_id={content_a}", headers=_auth(account_id)).json()
    assert [i["schedule_id"] for i in got["items"]] == [on_a]


def test_calendar_returns_items_in_range_ordered(client):
    account_id = _uid()
    content_id = _seed_content(account_id)
    inside_late = _create(client, account_id, content_id, publish_at="2031-03-20T10:00:00")
    inside_early = _create(client, account_id, content_id, publish_at="2031-03-05T10:00:00")
    _create(client, account_id, content_id, publish_at="2031-04-02T10:00:00")  # outside

    r = client.get("/schedules/calendar?start=2031-03-01T00:00:00&end=2031-04-01T00:00:00",
                   headers=_auth(account_id))
    assert r.status_code == 200, r.text
    data = r.json()
    # Month view, chronological, naive bounds taken as UTC.
    assert [i["schedule_id"] for i in data["items"]] == [
        inside_early["schedule_id"], inside_late["schedule_id"]]
    assert data["start"] == "2031-03-01T00:00:00+00:00"


def test_calendar_rejects_bad_ranges(client):
    account_id = _uid()
    r = client.get("/schedules/calendar?start=2031-03-02T00:00:00&end=2031-03-01T00:00:00",
                   headers=_auth(account_id))
    assert r.status_code == 422
    # start/end are required.
    assert client.get("/schedules/calendar", headers=_auth(account_id)).status_code == 422


# --------------------------------------------------------------------------
# Reschedule + cancel
# --------------------------------------------------------------------------

def test_reschedule_updates_time_timezone_and_recurrence(client):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id))["schedule_id"]
    r = client.patch(f"/schedules/{sid}", json={
        "publish_at": "2031-06-02T09:00:00",
        "timezone": "Europe/Berlin",       # June -> CEST (UTC+2)
        "recurrence": "daily",
    }, headers=_auth(account_id))
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["publish_at"] == "2031-06-02T07:00:00+00:00"
    assert item["timezone"] == "Europe/Berlin"
    assert item["recurrence"] == "daily"
    assert item["status"] == "pending"


def test_reschedule_explicit_null_clears_recurrence(client):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id),
                  recurrence_="weekly")["schedule_id"]
    r = client.patch(f"/schedules/{sid}", json={"recurrence": None},
                     headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json()["recurrence"] is None


def test_reschedule_rejects_past_time_and_empty_body(client):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id))["schedule_id"]
    past = (utcnow() - timedelta(minutes=1)).isoformat()
    assert client.patch(f"/schedules/{sid}", json={"publish_at": past},
                        headers=_auth(account_id)).status_code == 422
    assert client.patch(f"/schedules/{sid}", json={},
                        headers=_auth(account_id)).status_code == 422


def test_cancel_is_soft_and_terminal(client, db_session):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id))["schedule_id"]
    r = client.delete(f"/schedules/{sid}", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # Soft: the row survives for calendar history.
    assert _row(db_session, sid).status == "cancelled"
    # Terminal: no re-cancel, no reschedule.
    assert client.delete(f"/schedules/{sid}", headers=_auth(account_id)).status_code == 409
    assert client.patch(f"/schedules/{sid}", json={"publish_at": _future()},
                        headers=_auth(account_id)).status_code == 409


def test_fired_schedule_is_terminal(client, db_session):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id))["schedule_id"]
    _backdate(db_session, sid)
    assert tasks.scan_due_schedules() == 1
    assert _row(db_session, sid).status == "fired"
    assert client.patch(f"/schedules/{sid}", json={"publish_at": _future()},
                        headers=_auth(account_id)).status_code == 409
    assert client.delete(f"/schedules/{sid}", headers=_auth(account_id)).status_code == 409


# --------------------------------------------------------------------------
# Firing: due scan + one-off
# --------------------------------------------------------------------------

def test_scan_fires_due_one_off_and_emits_event(client, published_events, db_session):
    account_id = _uid()
    content_id = _seed_content(account_id, title="Big launch", image_url="/content/x/image")
    item = _create(client, account_id, content_id)
    sid = item["schedule_id"]
    due = _backdate(db_session, sid)

    assert tasks.scan_due_schedules() == 1  # eager Celery: fire ran inline

    row = _row(db_session, sid)
    assert row.status == "fired"
    assert row.fire_count == 1
    assert row.last_fired_at is not None

    assert len(published_events) == 1
    key, payload = published_events[0]
    assert key == "content.scheduled"
    assert payload["schedule_id"] == sid
    assert payload["account_id"] == account_id
    assert payload["content_id"] == content_id
    assert payload["publish_at"] == due.isoformat()
    assert payload["fired_at"]
    assert payload["fire_count"] == 1
    assert payload["recurrence"] is None
    assert payload["title"] == "Big launch"
    assert payload["image_url"] == "/content/x/image"


def test_scan_skips_future_and_non_pending(client, published_events, db_session):
    account_id = _uid()
    content_id = _seed_content(account_id)
    _create(client, account_id, content_id)  # future -> not due
    cancelled = _create(client, account_id, content_id)["schedule_id"]
    client.delete(f"/schedules/{cancelled}", headers=_auth(account_id))
    _backdate(db_session, cancelled)  # past but cancelled -> not due

    assert tasks.scan_due_schedules() == 0
    assert published_events == []


def test_double_fire_is_blocked_by_the_redis_lock(client, published_events, db_session):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id),
                  recurrence_="weekly")["schedule_id"]
    due_iso = _backdate(db_session, sid).isoformat()

    # Two overlapping beat scans dispatch the same (schedule, occurrence).
    assert tasks.fire_schedule(sid, due_iso) == "fired"
    assert tasks.fire_schedule(sid, due_iso) == "duplicate"
    assert len(published_events) == 1
    assert _row(db_session, sid).fire_count == 1


def test_stale_or_replayed_fire_is_a_noop_without_the_lock(client, published_events,
                                                           db_session, fake_redis):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id))["schedule_id"]
    due_iso = _backdate(db_session, sid).isoformat()

    # A task for an occurrence the row doesn't have is stale (layer 2).
    wrong = (utcnow() - timedelta(hours=3)).isoformat()
    assert tasks.fire_schedule(sid, wrong) == "stale"

    assert tasks.fire_schedule(sid, due_iso) == "fired"
    # Even with the Redis lock wiped (restart/expiry), the DB guard holds.
    fake_redis.flushall()
    assert tasks.fire_schedule(sid, due_iso) == "stale"
    assert len(published_events) == 1


def test_fire_cancels_schedule_whose_content_vanished(client, published_events, db_session):
    account_id = _uid()
    content_id = _seed_content(account_id)
    sid = _create(client, account_id, content_id)["schedule_id"]
    due_iso = _backdate(db_session, sid).isoformat()
    # Content ref gone (deletion raced past the consumer's cancel sweep).
    db_session.delete(db_session.get(ContentRef, content_id))
    db_session.commit()

    assert tasks.fire_schedule(sid, due_iso) == "content_missing"
    assert _row(db_session, sid).status == "cancelled"
    assert published_events == []


# --------------------------------------------------------------------------
# Recurring schedules (bonus): re-arm + recurrence math
# --------------------------------------------------------------------------

def test_recurring_schedule_rearms_and_stays_pending(client, published_events, db_session):
    account_id = _uid()
    sid = _create(client, account_id, _seed_content(account_id),
                  recurrence_="weekly")["schedule_id"]

    due0 = _backdate(db_session, sid)
    assert tasks.scan_due_schedules() == 1
    row = _row(db_session, sid)
    assert row.status == "pending"                      # recurring never becomes fired
    assert row.fire_count == 1
    assert as_utc(row.publish_at) == due0 + timedelta(days=7)  # re-armed one cadence out

    assert tasks.scan_due_schedules() == 0              # next occurrence is in the future

    due1 = _backdate(db_session, sid, minutes=1)
    assert tasks.scan_due_schedules() == 1
    row = _row(db_session, sid)
    assert row.fire_count == 2
    assert as_utc(row.publish_at) == due1 + timedelta(days=7)

    keys = [k for k, _ in published_events]
    assert keys == ["content.scheduled", "content.scheduled"]
    assert [p["fire_count"] for _, p in published_events] == [1, 2]
    assert published_events[0][1]["recurrence"] == "weekly"

    # Recurring stays active until cancelled — and cancel still works.
    r = client.delete(f"/schedules/{sid}", headers=_auth(account_id))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_daily_recurrence_preserves_local_time_across_dst():
    # 2024-03-10: US spring-forward. 9am America/New_York is 14:00 UTC in
    # EST but 13:00 UTC in EDT — wall-clock time must win over fixed 24h.
    base = datetime(2024, 3, 9, 14, 0, tzinfo=timezone.utc)  # 09:00 EST
    nxt = recurrence.next_occurrence(base, "daily", "America/New_York")
    assert nxt == datetime(2024, 3, 10, 13, 0, tzinfo=timezone.utc)  # 09:00 EDT


def test_weekly_recurrence_in_utc_is_seven_days():
    base = datetime(2031, 6, 1, 9, 30, tzinfo=timezone.utc)
    assert recurrence.next_occurrence(base, "weekly", "UTC") == base + timedelta(days=7)


def test_monthly_recurrence_clamps_to_month_end():
    base = datetime(2025, 1, 31, 10, 0, tzinfo=timezone.utc)
    assert recurrence.next_occurrence(base, "monthly", "UTC") == \
        datetime(2025, 2, 28, 10, 0, tzinfo=timezone.utc)
    # December rolls the year.
    base = datetime(2030, 12, 15, 10, 0, tzinfo=timezone.utc)
    assert recurrence.next_occurrence(base, "monthly", "UTC") == \
        datetime(2031, 1, 15, 10, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# content_events consumer
# --------------------------------------------------------------------------

def test_consumer_builds_the_read_model_and_gates_scheduling(client, db_session):
    account_id = _uid()
    payload, event_id = _content_event(account_id, status="draft", title="WIP")
    consumer.handle_event("content.created", payload, event_id)
    content_id = payload["content_id"]

    # Draft content exists but is not schedulable yet.
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 409

    # Approval arrives -> the same content becomes schedulable.
    payload, event_id = _content_event(account_id, content_id=content_id,
                                       status="approved", title="Ready", version=2)
    consumer.handle_event("content.approved", payload, event_id)
    db_session.expire_all()
    ref = db_session.get(ContentRef, content_id)
    assert (ref.status, ref.title, ref.version) == ("approved", "Ready", 2)

    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 201


def test_consumer_is_idempotent_on_event_replay(client, db_session):
    account_id = _uid()
    payload, event_id = _content_event(account_id, title="v1")
    consumer.handle_event("content.approved", payload, event_id)
    payload["title"] = "should never apply"  # same event_id redelivered
    consumer.handle_event("content.approved", payload, event_id)

    db_session.expire_all()
    ref = db_session.get(ContentRef, payload["content_id"])
    assert ref.title == "v1"
    assert db_session.get(ProcessedEvent, event_id) is not None
    refs = db_session.scalars(select(ContentRef)
                              .where(ContentRef.account_id == account_id)).all()
    assert len(refs) == 1


def test_content_deleted_cancels_pending_schedules_only(client, db_session):
    account_id = _uid()
    content_id = _seed_content(account_id)
    fired_sid = _create(client, account_id, content_id)["schedule_id"]
    _backdate(db_session, fired_sid)
    assert tasks.scan_due_schedules() == 1  # one-off fired -> history

    pending_sid = _create(client, account_id, content_id)["schedule_id"]

    payload, event_id = _content_event(account_id, content_id=content_id)
    consumer.handle_event("content.deleted", payload, event_id)

    assert _row(db_session, pending_sid).status == "cancelled"
    assert _row(db_session, fired_sid).status == "fired"  # history untouched
    assert db_session.get(ContentRef, content_id) is None
    # And the content is no longer schedulable.
    r = client.post("/schedules", json={"content_id": content_id, "publish_at": _future()},
                    headers=_auth(account_id))
    assert r.status_code == 404
