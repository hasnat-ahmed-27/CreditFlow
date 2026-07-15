"""
Scraper service tests: every route requires a valid access token, all data
is tenant-scoped (cross-account access answers 404 and lists never leak),
submitted URLs pass the SSRF gate (http/https only, no credentials, no
localhost/private/link-local/metadata targets — rejected with 422 and the
reason), a job walks pending -> running -> completed|failed through the
eager Celery worker with the mocked engine's canned content stored in the
fake Mongo and readable back through the API, failures conclude the job and
emit scrape.failed, recurring jobs re-arm and are re-fired by the beat scan,
the claim compare-and-set makes duplicate/stale dispatches no-ops, and the
scrape.requested consumer creates+runs jobs exactly once per event
(processed_events idempotency + the request_event_id crash-window guard).

No infra: SQLite ledger via conftest, mongomock behind store.get_client,
publisher stubbed, scraper_engine.scrape faked (MONGO_URL and the Celery
broker also point at dead addresses so nothing can reach the network), and
consumer.handle_event called directly (the exact function the broker would).
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import scraper_engine
import store
import tasks
import url_guard
from conftest import CANNED_HTML, CANNED_LINKS, CANNED_TEXT, CANNED_TITLE

VALID_URL = "https://competitor.example/pricing"


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _create_job(client, account_id: str, url: str = VALID_URL, **body) -> dict:
    r = client.post("/scrape-jobs", json={"url": url, **body}, headers=_auth(account_id))
    assert r.status_code == 201, r.text
    return r.json()


def _request_event(account_id: str, url: str = VALID_URL, **extra) -> tuple[dict, str]:
    """(payload, event_id) shaped like a scrape.requested publisher would send."""
    payload = {"account_id": account_id, "url": url,
               "requested_by_user_id": _uid(), **extra}
    return payload, f"evt_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_every_route_requires_auth(client):
    jid = _uid()
    for method, path in [
        ("post", "/scrape-jobs"),
        ("get", "/scrape-jobs"),
        ("get", f"/scrape-jobs/{jid}"),
        ("get", f"/scrape-jobs/{jid}/documents"),
        ("delete", f"/scrape-jobs/{jid}"),
        ("get", f"/scraped-documents/{jid}"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_refresh_token_is_rejected(client):
    token, _ = jwt_utils.sign_refresh_token(_uid(), _uid())
    r = client.get("/scrape-jobs", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_garbage_token_is_rejected(client):
    r = client.get("/scrape-jobs", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# URL validation / SSRF gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://example.com/file",                       # non-http scheme
    "javascript:alert(1)",                          # non-http scheme
    "file:///etc/passwd",                           # non-http scheme
    "https://user:pass@example.com/",               # credential smuggling
    "http://localhost/admin",                       # loopback by name
    "http://localhost./admin",                      # trailing-dot disguise
    "http://127.0.0.1:8000/",                       # loopback literal
    "http://[::1]/",                                # IPv6 loopback
    "http://[fd00::1]/",                            # IPv6 unique-local
    "http://10.0.0.5/",                             # RFC1918 private
    "http://192.168.1.1/router",                    # RFC1918 private
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://100.64.0.1/",                           # CGNAT
    "http://0x7f.0.0.1/",                           # hex-dotted loopback (inet_aton)
    "http://2130706433/",                           # decimal-int loopback
    "http://mongo:27017/",                          # dotless docker-network name
    "http://intranet/",                             # dotless internal name
    "http://metadata.google.internal/",             # blocked suffix
    "http://printer.local/",                        # blocked suffix
    "http://nas.lan/",                              # blocked suffix
])
def test_ssrf_targets_are_rejected(client, url, scrape_engine):
    r = client.post("/scrape-jobs", json={"url": url}, headers=_auth(_uid()))
    assert r.status_code == 422, f"{url} -> {r.status_code}: {r.text}"
    assert scrape_engine["calls"] == []  # nothing was ever fetched


@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://sub.example.co.uk/path?q=1",
    "https://8.8.8.8/status",                       # globally routable literal is fine
])
def test_public_urls_are_accepted(client, url):
    r = client.post("/scrape-jobs", json={"url": url}, headers=_auth(_uid()))
    assert r.status_code == 201, r.text


def test_unknown_recurrence_is_rejected(client):
    r = client.post("/scrape-jobs", json={"url": VALID_URL, "recurrence": "fortnightly"},
                    headers=_auth(_uid()))
    assert r.status_code == 422


# --------------------------------------------------------------------------
# The happy path: submit -> (eager) worker run -> stored document -> read back
# --------------------------------------------------------------------------

def test_successful_scrape_stores_and_returns_document(client, scrape_engine,
                                                       published_events):
    account = _uid()
    job = _create_job(client, account, job_type="pricing")

    # Eager Celery: the run already concluded inside POST.
    assert job["status"] == "completed"
    assert job["run_count"] == 1
    assert job["error"] is None
    assert job["result_document_id"]
    assert scrape_engine["calls"] == [{"url": VALID_URL, "job_type": "pricing"}]

    # Job detail embeds the extracted document, canned content included.
    detail = client.get(f"/scrape-jobs/{job['job_id']}", headers=_auth(account)).json()
    doc = detail["document"]
    assert doc["title"] == CANNED_TITLE
    assert doc["text"] == CANNED_TEXT
    assert doc["links"] == CANNED_LINKS
    assert doc["html"] == CANNED_HTML
    assert doc["job_id"] == job["job_id"]

    # The document is also directly addressable (scrape.completed carries its id).
    direct = client.get(f"/scraped-documents/{job['result_document_id']}",
                        headers=_auth(account))
    assert direct.status_code == 200
    assert direct.json()["title"] == CANNED_TITLE

    # Write landed BEFORE the announcement, and the payload carries the reference.
    assert [k for k, _ in published_events] == ["scrape.completed"]
    payload = published_events[0][1]
    assert payload["document_id"] == job["result_document_id"]
    assert payload["job_id"] == job["job_id"]
    assert payload["account_id"] == account
    assert payload["url"] == VALID_URL
    assert payload["run_count"] == 1


def test_job_is_pending_until_the_worker_runs(client, monkeypatch, scrape_engine):
    """With the dispatch suppressed the API answers `pending` — the status a
    production caller sees before the worker picks the job up — and running
    the task by hand walks it to completed."""
    monkeypatch.setattr(tasks.run_scrape, "delay", lambda *a, **k: None)
    account = _uid()
    job = _create_job(client, account)
    assert job["status"] == "pending"
    assert job["run_count"] == 0
    assert scrape_engine["calls"] == []

    result = tasks.run_scrape(job["job_id"], job["next_run_at"])
    assert result == "completed"
    detail = client.get(f"/scrape-jobs/{job['job_id']}", headers=_auth(account)).json()
    assert detail["status"] == "completed"


def test_failed_scrape_concludes_job_and_emits_failed(client, scrape_engine,
                                                      published_events):
    scrape_engine["error"] = scraper_engine.ScrapeError("fetch failed: net::ERR_TIMED_OUT")
    account = _uid()
    job = _create_job(client, account)

    assert job["status"] == "failed"
    assert "ERR_TIMED_OUT" in job["error"]
    assert job["result_document_id"] is None
    assert job["run_count"] == 1

    assert [k for k, _ in published_events] == ["scrape.failed"]
    payload = published_events[0][1]
    assert payload["job_id"] == job["job_id"]
    assert "ERR_TIMED_OUT" in payload["error"]

    # No document was stored for the failed run.
    docs = client.get(f"/scrape-jobs/{job['job_id']}/documents",
                      headers=_auth(account)).json()
    assert docs["total"] == 0


def test_robots_denial_is_a_failed_run(client, scrape_engine):
    scrape_engine["error"] = scraper_engine.RobotsDisallowedError(
        f"robots.txt disallows scraping {VALID_URL}")
    job = _create_job(client, _uid())
    assert job["status"] == "failed"
    assert "robots.txt" in job["error"]


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------

def test_jobs_and_documents_are_tenant_scoped(client):
    owner_account, other_account = _uid(), _uid()
    job = _create_job(client, owner_account)

    foreign = _auth(other_account)
    assert client.get(f"/scrape-jobs/{job['job_id']}", headers=foreign).status_code == 404
    assert client.get(f"/scrape-jobs/{job['job_id']}/documents",
                      headers=foreign).status_code == 404
    assert client.get(f"/scraped-documents/{job['result_document_id']}",
                      headers=foreign).status_code == 404
    assert client.delete(f"/scrape-jobs/{job['job_id']}", headers=foreign).status_code == 404
    assert client.get("/scrape-jobs", headers=foreign).json()["total"] == 0

    # ...while the owning account still sees everything.
    assert client.get(f"/scrape-jobs/{job['job_id']}",
                      headers=_auth(owner_account)).status_code == 200


def test_cross_account_delete_answers_404_even_for_members(client):
    """Existence must not leak: a foreign MEMBER gets 404 (not 403) — the
    role check only runs for resources in the caller's own account."""
    job = _create_job(client, _uid())
    r = client.delete(f"/scrape-jobs/{job['job_id']}", headers=_auth(_uid(), role="member"))
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def test_member_can_submit_and_read_but_not_delete(client):
    account = _uid()
    job = _create_job(client, account)  # created by owner

    member = _auth(account, role="member")
    r = client.post("/scrape-jobs", json={"url": VALID_URL}, headers=member)
    assert r.status_code == 201
    assert client.get(f"/scrape-jobs/{job['job_id']}", headers=member).status_code == 200

    r = client.delete(f"/scrape-jobs/{job['job_id']}", headers=member)
    assert r.status_code == 403


def test_delete_removes_job_and_its_documents(client):
    account = _uid()
    job = _create_job(client, account)
    doc_id = job["result_document_id"]

    r = client.delete(f"/scrape-jobs/{job['job_id']}", headers=_auth(account))
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "job_id": job["job_id"], "documents_deleted": 1}

    assert client.get(f"/scrape-jobs/{job['job_id']}", headers=_auth(account)).status_code == 404
    assert client.get(f"/scraped-documents/{doc_id}", headers=_auth(account)).status_code == 404


# --------------------------------------------------------------------------
# Listing: pagination + filters
# --------------------------------------------------------------------------

def test_list_pagination_and_filters(client, scrape_engine):
    account = _uid()
    _create_job(client, account, job_type="pricing")
    _create_job(client, account, job_type="pricing")
    _create_job(client, account, job_type="news")
    scrape_engine["error"] = scraper_engine.ScrapeError("boom")
    failed = _create_job(client, account, job_type="news")
    headers = _auth(account)

    full = client.get("/scrape-jobs", headers=headers).json()
    assert full["total"] == 4 and len(full["items"]) == 4

    page = client.get("/scrape-jobs?limit=2&offset=2", headers=headers).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert {j["job_id"] for j in page["items"]}.isdisjoint(
        {j["job_id"] for j in client.get("/scrape-jobs?limit=2", headers=headers).json()["items"]})

    only_failed = client.get("/scrape-jobs?status=failed", headers=headers).json()
    assert only_failed["total"] == 1
    assert only_failed["items"][0]["job_id"] == failed["job_id"]

    completed_news = client.get("/scrape-jobs?status=completed&job_type=news",
                                headers=headers).json()
    assert completed_news["total"] == 1
    assert completed_news["items"][0]["job_type"] == "news"

    assert client.get("/scrape-jobs?status=exploded", headers=headers).status_code == 422


def test_document_list_is_summary_only(client):
    account = _uid()
    job = _create_job(client, account)
    docs = client.get(f"/scrape-jobs/{job['job_id']}/documents",
                      headers=_auth(account)).json()
    assert docs["total"] == 1
    summary = docs["items"][0]
    assert summary["title"] == CANNED_TITLE
    assert "html" not in summary and "text" not in summary  # heavy fields on single-get only


# --------------------------------------------------------------------------
# Recurring jobs + the beat scan
# --------------------------------------------------------------------------

def test_recurring_job_rearms_and_is_refired_by_the_scan(client, scrape_engine,
                                                         published_events):
    account = _uid()
    job = _create_job(client, account, recurrence="daily")
    assert job["status"] == "completed" and job["run_count"] == 1

    # Re-armed roughly a day out (UTC interval — see store.RECURRENCE_DELTAS).
    next_run = store.as_utc(store.get_job(account, job["job_id"])["next_run_at"])
    delta = next_run - store.utcnow()
    assert timedelta(hours=23) < delta <= timedelta(days=1)

    # Nothing due yet — the scan dispatches nothing.
    assert tasks.scan_due_scrapes() == 0

    # Time passes: force the occurrence into the past, then let beat's scan run.
    store.get_db()[store.JOBS].update_one(
        {"_id": job["job_id"]},
        {"$set": {"next_run_at": store.utcnow() - timedelta(minutes=1)}})
    assert tasks.scan_due_scrapes() == 1

    refreshed = store.get_job(account, job["job_id"])
    assert refreshed["run_count"] == 2
    assert refreshed["status"] == "completed"
    assert store.as_utc(refreshed["next_run_at"]) > store.utcnow()  # re-armed again

    # One document per run, latest one referenced by the job.
    docs = client.get(f"/scrape-jobs/{job['job_id']}/documents",
                      headers=_auth(account)).json()
    assert docs["total"] == 2
    assert refreshed["result_document_id"] == docs["items"][0]["document_id"]
    assert [k for k, _ in published_events] == ["scrape.completed", "scrape.completed"]


def test_scan_rescues_a_stuck_pending_one_off(client):
    """A one-off job whose dispatch was lost (crash between commit and
    .delay) sits pending with next_run_at in the past — the beat scan is its
    rescue path and must run it exactly once."""
    account = _uid()
    # Isolated patch context (NOT the shared monkeypatch fixture — undoing
    # that would also revert the autouse mongomock/engine patches).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tasks.run_scrape, "delay", lambda *a, **k: None)  # lose the dispatch
        job = _create_job(client, account)
        assert job["status"] == "pending"

    # Dispatch restored (eager): the scan runs the stranded job inline.
    assert tasks.scan_due_scrapes() == 1
    refreshed = store.get_job(account, job["job_id"])
    assert refreshed["status"] == "completed"
    assert refreshed["next_run_at"] is None  # one-off: never due again
    assert tasks.scan_due_scrapes() == 0


def test_stale_or_duplicate_dispatch_is_a_noop(client, monkeypatch, scrape_engine):
    monkeypatch.setattr(tasks.run_scrape, "delay", lambda *a, **k: None)
    account = _uid()
    job = _create_job(client, account)
    occurrence = store.as_utc(store.get_job(account, job["job_id"])["next_run_at"])

    # Wrong occurrence (a straggler for a re-armed/rescheduled job): no claim.
    stale = (occurrence + timedelta(seconds=5)).isoformat()
    assert tasks.run_scrape(job["job_id"], stale) == "stale"
    assert scrape_engine["calls"] == []

    # Job already running (a beat overlap / duplicated dispatch): no claim.
    store.get_db()[store.JOBS].update_one({"_id": job["job_id"]},
                                          {"$set": {"status": "running"}})
    assert tasks.run_scrape(job["job_id"], occurrence.isoformat()) == "stale"
    assert scrape_engine["calls"] == []

    # And a running job is invisible to the due-scan.
    assert tasks.scan_due_scrapes() == 0


# --------------------------------------------------------------------------
# The scrape.requested consumer
# --------------------------------------------------------------------------

def test_consumer_creates_and_runs_a_job(client, db_session, scrape_engine,
                                         published_events):
    account = _uid()
    payload, event_id = _request_event(account, job_type="trend")
    consumer.handle_event("scrape.requested", payload, event_id)

    jobs, total = store.list_jobs(account)
    assert total == 1
    job = jobs[0]
    assert job["source"] == "event"
    assert job["request_event_id"] == event_id
    assert job["status"] == "completed"  # eager Celery ran it inline
    assert job["created_by_user_id"] == payload["requested_by_user_id"]
    assert scrape_engine["calls"] == [{"url": VALID_URL, "job_type": "trend"}]

    assert [k for k, _ in published_events] == ["scrape.completed"]
    assert published_events[0][1]["request_event_id"] == event_id
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_consumer_is_idempotent_on_redelivery(client, scrape_engine, published_events):
    account = _uid()
    payload, event_id = _request_event(account)
    consumer.handle_event("scrape.requested", payload, event_id)
    consumer.handle_event("scrape.requested", payload, event_id)  # broker redelivery

    _, total = store.list_jobs(account)
    assert total == 1
    assert len(scrape_engine["calls"]) == 1
    assert len(published_events) == 1


def test_consumer_survives_the_crash_window_without_duplicating(client, db_session,
                                                                scrape_engine):
    """Crash AFTER the Mongo job insert but BEFORE the processed_events
    commit: the redelivered event must find the existing job via
    request_event_id instead of creating a second one — and a concluded
    one-off (next_run_at None) must not be re-dispatched."""
    account = _uid()
    payload, event_id = _request_event(account)
    consumer.handle_event("scrape.requested", payload, event_id)

    # Simulate the lost commit, then redeliver.
    db_session.delete(db_session.get(ProcessedEvent, event_id))
    db_session.commit()
    consumer.handle_event("scrape.requested", payload, event_id)

    _, total = store.list_jobs(account)
    assert total == 1
    assert len(scrape_engine["calls"]) == 1  # the concluded job never re-ran


def test_consumer_drops_malformed_requests_forever(client, db_session, scrape_engine,
                                                   published_events):
    payload, event_id = _request_event(_uid())
    del payload["account_id"]
    consumer.handle_event("scrape.requested", payload, event_id)

    assert scrape_engine["calls"] == []
    assert published_events == []
    # Recorded: a redelivery of the same malformed event is skipped outright.
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_consumer_answers_ssrf_targets_with_scrape_failed(client, scrape_engine,
                                                          published_events):
    account = _uid()
    payload, event_id = _request_event(account, url="http://169.254.169.254/latest/")
    consumer.handle_event("scrape.requested", payload, event_id)

    _, total = store.list_jobs(account)
    assert total == 0  # no job was ever created
    assert scrape_engine["calls"] == []
    assert [k for k, _ in published_events] == ["scrape.failed"]
    failed = published_events[0][1]
    assert failed["job_id"] is None
    assert failed["account_id"] == account
    assert failed["request_event_id"] == event_id
    assert "not globally routable" in failed["error"]


def test_consumer_rejects_unknown_recurrence(client, published_events):
    payload, event_id = _request_event(_uid(), recurrence="fortnightly")
    consumer.handle_event("scrape.requested", payload, event_id)
    assert [k for k, _ in published_events] == ["scrape.failed"]
    assert "recurrence" in published_events[0][1]["error"]


# --------------------------------------------------------------------------
# Engine internals (real functions — the scrape() seam stays mocked)
# --------------------------------------------------------------------------

EXTRACT_HTML = """
<html><head><title> Acme — Pricing </title>
<meta name="description" content="Compare Acme plans.">
</head><body>
<h1>Pricing</h1><h2>Pro</h2>
<script>var secret = "SCRIPT_NOISE";</script>
<style>.x { color: red }</style>
<a href="/signup">Sign up</a>
<a href="https://other.example/partners">Partners</a>
<a href="mailto:sales@acme.example">Email sales</a>
<a href="/signup">Sign up (again)</a>
<p>Pro plan costs $49 per month.</p>
</body></html>
"""


def test_extract_pulls_structured_content():
    out = scraper_engine.extract(EXTRACT_HTML, "https://acme.example/pricing")
    assert out["title"] == "Acme — Pricing"
    assert out["description"] == "Compare Acme plans."
    assert out["headings"] == ["Pricing", "Pro"]
    # Links: absolute, deduped, http(s) only (no mailto:).
    assert out["links"] == ["https://acme.example/signup",
                            "https://other.example/partners"]
    assert "Pro plan costs $49 per month." in out["text"]
    assert "SCRIPT_NOISE" not in out["text"]  # script/style stripped


def test_extract_tolerates_empty_html():
    out = scraper_engine.extract("", "https://acme.example/")
    assert out == {"title": None, "description": None, "headings": [],
                   "text": "", "links": []}


def test_robots_rules_are_honored(monkeypatch):
    rules = "User-agent: *\nDisallow: /private/\n"
    monkeypatch.setattr(scraper_engine, "_fetch_robots_txt", lambda base: rules)
    assert scraper_engine.robots_allowed("https://acme.example/public/page") is True
    assert scraper_engine.robots_allowed("https://acme.example/private/page") is False


def test_robots_rules_match_our_user_agent(monkeypatch):
    rules = "User-agent: CreditFlowScraper\nDisallow: /\n"
    monkeypatch.setattr(scraper_engine, "_fetch_robots_txt", lambda base: rules)
    assert scraper_engine.robots_allowed("https://acme.example/anything") is False


def test_absent_robots_permits_the_fetch(monkeypatch):
    monkeypatch.setattr(scraper_engine, "_fetch_robots_txt", lambda base: None)
    assert scraper_engine.robots_allowed("https://acme.example/page") is True


# --------------------------------------------------------------------------
# url_guard unit coverage (beyond the API-level cases above)
# --------------------------------------------------------------------------

def test_validate_url_returns_the_url_unchanged():
    assert url_guard.validate_url(VALID_URL) == VALID_URL


def test_validate_url_rejects_overlong_urls():
    with pytest.raises(url_guard.InvalidTargetURL):
        url_guard.validate_url("https://example.com/" + "a" * url_guard.MAX_URL_LENGTH)


def test_validate_url_rejects_empty_and_hostless():
    for bad in ("", "   ", "http:///path", "https://"):
        with pytest.raises(url_guard.InvalidTargetURL):
            url_guard.validate_url(bad)
