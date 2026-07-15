"""
Notification service tests: every consumed event type renders the right
template to the right recipient (verification/reset from Auth, invite/welcome
from User, receipt/dunning from Billing, quota and low-balance alerts from
Usage/Credits, post status from Social), account-scoped events resolve their
address through the recipients read model (including the placeholder
account_id == user_id fallback) and conclude as failed-and-logged when no
address is known, the provider layer falls back from Resend to Mailgun and
maps transient failures onto raise-without-commit (broker retry) while
permanent failures land as status=failed log rows, the consumer is
idempotent (a redelivered event never double-sends), notification.sent is
emitted only for sent mail, non-notifiable keys are recorded and skipped,
and the read routes require a valid token and never leak across tenants.

No infra: SQLite via conftest, publisher stubbed, both mailer.py provider
functions faked (base URLs also point at a dead address so nothing can reach
the network — proven by a test that calls the REAL provider function), and
consumer.handle_event called directly (the exact function the broker would).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from creditflow_common import jwt_utils
from creditflow_common.idempotency import ProcessedEvent

import consumer
import mailer
import templates
from conftest import (
    MAILGUN_MESSAGE_ID,
    RESEND_MESSAGE_ID,
    REAL_SEND_VIA_MAILGUN,
    REAL_SEND_VIA_RESEND,
)
from models import AccountRecipient, KnownUser, NotificationLog


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(account_id: str, role: str = "owner", user_id: str | None = None) -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues."""
    token, _ = jwt_utils.sign_access_token(user_id or _uid(), account_id, role)
    return {"Authorization": f"Bearer {token}"}


def _handle(routing_key: str, data: dict, event_id: str | None = None) -> str:
    """Drive the consumer exactly like the broker would; returns the event_id."""
    event_id = event_id or _uid()
    consumer.handle_event(routing_key, data, event_id)
    return event_id


def _logs(db) -> list[NotificationLog]:
    return db.scalars(select(NotificationLog).order_by(NotificationLog.created_at,
                                                       NotificationLog.id)).all()


def _register_user(email: str = "owner@example.com") -> str:
    """Seed the read model with a user.registered event; returns the user_id
    (== the placeholder account_id Auth puts in today's tokens)."""
    user_id = _uid()
    _handle("user.registered", {"user_id": user_id, "email": email,
                                "verification_token": "vtok-seed"})
    return user_id


# --------------------------------------------------------------------------
# Queue contract — the bindings must match what the producers pre-declared
# --------------------------------------------------------------------------

def test_bindings_match_producer_predeclared_queues():
    """Each producer's events.py declared `notifications.<its exchange>` for
    us — consuming any other name would strand their accumulating messages."""
    assert {(b.exchange, b.queue) for b in consumer.BINDINGS} == {
        ("user_events", "notifications.user_events"),
        ("account_events", "notifications.account_events"),
        ("billing_events", "notifications.billing_events"),
        ("usage_events", "notifications.usage_events"),
        ("credits_events", "notifications.credits_events"),
        ("social_events", "notifications.social_events"),
    }


# --------------------------------------------------------------------------
# Event type -> template -> recipient (payload-addressed events)
# --------------------------------------------------------------------------

def test_user_registered_sends_verification_email(client, db_session, mail_state,
                                                  published_events):
    user_id = _uid()
    event_id = _handle("user.registered", {
        "user_id": user_id, "email": "new@example.com",
        "verification_token": "vtok-123",
    })

    assert len(mail_state["resend_calls"]) == 1
    call = mail_state["resend_calls"][0]
    assert call["to"] == "new@example.com"
    assert call["subject"] == "Verify your CreditFlow email"
    assert "/verify-email?token=vtok-123" in call["text"]

    (row,) = _logs(db_session)
    assert (row.template, row.status, row.provider) == ("verification", "sent", "resend")
    assert row.recipient == "new@example.com"
    assert row.provider_message_id == RESEND_MESSAGE_ID
    assert row.event_id == event_id
    assert row.account_id == user_id  # placeholder convention: user_id scopes the log

    assert [rk for rk, _ in published_events] == ["notification.sent"]
    payload = published_events[0][1]
    assert payload["event_id"] == event_id
    assert payload["template"] == "verification"
    assert payload["recipient"] == "new@example.com"
    assert payload["provider"] == "resend"

    # The read model learned the address for later account-scoped alerts.
    assert db_session.get(KnownUser, user_id).email == "new@example.com"


def test_password_reset_email(client, db_session, mail_state):
    _handle("user.password_reset_requested", {
        "user_id": _uid(), "email": "resetme@example.com", "reset_token": "rtok-9",
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "resetme@example.com"
    assert call["subject"] == "Reset your CreditFlow password"
    assert "/reset-password?token=rtok-9" in call["text"]
    assert _logs(db_session)[0].template == "password_reset"


def test_invite_email_goes_to_invitee(client, db_session, mail_state):
    _handle("invite.created", {
        "invite_id": _uid(), "account_id": _uid(), "account_name": "Acme Team",
        "email": "invitee@example.com", "role": "member",
        "invite_token": "itok-42", "expires_at": "2026-07-23T00:00:00+00:00",
        "invited_by_user_id": _uid(),
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "invitee@example.com"
    assert call["subject"] == "You've been invited to Acme Team on CreditFlow"
    assert "/invites/accept?token=itok-42" in call["text"]
    assert "member" in call["text"]
    assert _logs(db_session)[0].template == "invite"


def test_member_joined_welcomes_the_new_member_and_feeds_read_model(client, db_session,
                                                                    mail_state):
    account_id, user_id = _uid(), _uid()
    _handle("member.joined", {
        "account_id": account_id, "account_name": "Acme Team",
        "user_id": user_id, "email": "joiner@example.com", "role": "admin",
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "joiner@example.com"
    assert call["subject"] == "Welcome to Acme Team on CreditFlow"
    assert _logs(db_session)[0].template == "member_joined"

    rec = db_session.scalar(select(AccountRecipient).where(
        AccountRecipient.account_id == account_id))
    assert (rec.user_id, rec.email, rec.role) == (user_id, "joiner@example.com", "admin")


# --------------------------------------------------------------------------
# Account-scoped events resolve recipients through the read model
# --------------------------------------------------------------------------

def test_invoice_paid_receipt_via_placeholder_account_id(client, db_session, mail_state):
    """Billing events carry the token's account_id — today that is the USER
    id (Auth placeholder), so resolution falls back to known_users."""
    user_id = _register_user("payer@example.com")
    mail_state["resend_calls"].clear()

    _handle("invoice.paid", {
        "account_id": user_id, "plan": "pro", "stripe_invoice_id": "in_123",
        "amount_paid": 2900, "currency": "usd", "dunning_recovered": False,
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "payer@example.com"
    assert call["subject"] == "Your CreditFlow payment receipt"
    assert "29.00 USD" in call["text"]
    assert "pro" in call["text"]
    assert "in_123" in call["text"]
    assert _logs(db_session)[-1].template == "receipt"


def test_team_account_alert_resolves_owner_via_account_created(client, db_session,
                                                               mail_state):
    """Real account ids: account.created maps account -> owner_user_id, and
    the owner's address joins through known_users — regardless of which
    event arrived first (email=NULL rows resolve lazily)."""
    owner_id, account_id = _uid(), _uid()
    _handle("account.created", {
        "account_id": account_id, "type": "team", "name": "Acme",
        "plan_tier": "free", "owner_user_id": owner_id,
    })  # arrives BEFORE we know the owner's email
    _handle("user.registered", {"user_id": owner_id, "email": "boss@example.com",
                                "verification_token": "v"})
    mail_state["resend_calls"].clear()

    _handle("payment.failed", {
        "account_id": account_id, "plan": "team", "stripe_invoice_id": "in_9",
        "amount_due": 9900, "currency": "usd",
        "grace_expires_at": "2026-07-23T00:00:00+00:00",
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "boss@example.com"
    assert call["subject"] == "Payment failed for your CreditFlow subscription"
    assert "99.00 USD" in call["text"]
    assert "grace period" in call["text"]
    assert _logs(db_session)[-1].template == "payment_failed"


def test_owner_preferred_over_other_members(client, db_session, mail_state):
    account_id, owner_id = _uid(), _uid()
    _handle("member.joined", {"account_id": account_id, "account_name": "Acme",
                              "user_id": _uid(), "email": "member@example.com",
                              "role": "member"})
    _handle("member.joined", {"account_id": account_id, "account_name": "Acme",
                              "user_id": owner_id, "email": "owner@example.com",
                              "role": "owner"})
    mail_state["resend_calls"].clear()

    _handle("credits.low_balance", {"account_id": account_id, "balance": 5,
                                    "threshold": 10})
    call = mail_state["resend_calls"][0]
    assert call["to"] == "owner@example.com"
    assert call["subject"] == "Your CreditFlow credit balance is low"
    assert "5" in call["text"] and "10" in call["text"]


def test_unknown_account_concludes_failed_and_logged(client, db_session, mail_state,
                                                     published_events):
    """No address known -> permanent failure: logged, event recorded as
    processed (never retried), no provider call, no notification.sent."""
    event_id = _handle("usage.threshold_reached", {
        "account_id": _uid(), "threshold_percent": 80,
        "used_tokens": 80000, "quota_tokens": 100000, "period": "2026-07",
    })
    assert mail_state["resend_calls"] == [] and mail_state["mailgun_calls"] == []
    (row,) = _logs(db_session)
    assert (row.status, row.recipient) == ("failed", None)
    assert "no known recipient" in row.error
    assert db_session.get(ProcessedEvent, event_id) is not None
    assert published_events == []


# --------------------------------------------------------------------------
# Quota threshold and social status templates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pct,template,subject", [
    (80, "quota_threshold_80", "You've used 80% of your monthly AI quota"),
    (100, "quota_threshold_100", "You've reached your monthly AI quota"),
])
def test_quota_threshold_variants(client, db_session, mail_state, pct, template, subject):
    user_id = _register_user("quota@example.com")
    mail_state["resend_calls"].clear()

    _handle("usage.threshold_reached", {
        "account_id": user_id, "threshold_percent": pct,
        "used_tokens": pct * 1000, "quota_tokens": 100000, "period": "2026-07",
    })
    call = mail_state["resend_calls"][0]
    assert call["to"] == "quota@example.com"
    assert call["subject"] == subject
    assert "100000" in call["text"] and "2026-07" in call["text"]
    assert _logs(db_session)[-1].template == template


def test_post_published_email_carries_the_post_url(client, db_session, mail_state):
    user_id = _register_user("poster@example.com")
    mail_state["resend_calls"].clear()

    _handle("post.published", {
        "job_id": _uid(), "account_id": user_id, "content_id": "content-7",
        "schedule_id": _uid(), "connection_id": _uid(), "source": "scheduled",
        "text_source": "content", "image_included": False,
        "linkedin_post_id": "urn:li:share:123",
        "linkedin_post_url": "https://www.linkedin.com/feed/update/urn:li:share:123/",
        "error": None, "fire_event_id": _uid(),
    })
    call = mail_state["resend_calls"][0]
    assert call["subject"] == "Your LinkedIn post is live"
    assert "https://www.linkedin.com/feed/update/urn:li:share:123/" in call["text"]
    assert _logs(db_session)[-1].template == "post_published"


def test_post_failed_email_carries_the_reason(client, db_session, mail_state):
    user_id = _register_user("poster@example.com")
    mail_state["resend_calls"].clear()

    _handle("post.failed", {
        "job_id": _uid(), "account_id": user_id, "content_id": "content-8",
        "source": "scheduled", "error": "no connected LinkedIn account",
    })
    call = mail_state["resend_calls"][0]
    assert call["subject"] == "Your LinkedIn post failed to publish"
    assert "no connected LinkedIn account" in call["text"]
    assert _logs(db_session)[-1].template == "post_failed"


def test_subscription_downgraded_dunning_notice(client, db_session, mail_state):
    user_id = _register_user("downgraded@example.com")
    mail_state["resend_calls"].clear()

    _handle("subscription.downgraded", {
        "account_id": user_id, "previous_plan": "pro", "plan": "free",
        "reason": "dunning_grace_expired",
    })
    call = mail_state["resend_calls"][0]
    assert call["subject"] == "Your CreditFlow subscription was downgraded"
    assert "pro" in call["text"] and "dunning_grace_expired" in call["text"]
    assert _logs(db_session)[-1].template == "subscription_downgraded"


# --------------------------------------------------------------------------
# Non-notifiable keys: recorded, never emailed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("routing_key,data", [
    ("user.logged_in", {"user_id": "u1", "email": "x@example.com", "jti": "j"}),
    ("account.updated", {"account_id": "a1", "change": "profile"}),
    ("subscription.updated", {"account_id": "a1", "plan": "team"}),
    ("refund.issued", {"account_id": "a1", "amount": 500}),
    ("ai.generation_failed", {"job_id": "g1", "account_id": "a1", "reason": "502"}),
])
def test_non_notifiable_keys_are_recorded_and_skipped(client, db_session, mail_state,
                                                      published_events, routing_key, data):
    """These keys share our queues (wildcard bindings / the AI service's
    extra binding) but are not in the spec's notification list — the event
    is deduped for good, but no mail and no log row."""
    event_id = _handle(routing_key, data)
    assert mail_state["resend_calls"] == [] and mail_state["mailgun_calls"] == []
    assert _logs(db_session) == []
    assert db_session.get(ProcessedEvent, event_id) is not None
    assert published_events == []


# --------------------------------------------------------------------------
# Provider failure paths — Resend primary, Mailgun fallback, DLQ mapping
# --------------------------------------------------------------------------

def test_resend_transient_falls_back_to_mailgun(client, db_session, mail_state,
                                                published_events):
    mail_state["errors"]["resend"] = mailer.MailerTransientError("resend HTTP 503")
    _handle("user.registered", {"user_id": _uid(), "email": "fb@example.com",
                                "verification_token": "v"})
    assert len(mail_state["resend_calls"]) == 1  # tried primary first
    assert len(mail_state["mailgun_calls"]) == 1
    (row,) = _logs(db_session)
    assert (row.status, row.provider) == ("sent", "mailgun")
    assert row.provider_message_id == MAILGUN_MESSAGE_ID
    assert published_events[0][1]["provider"] == "mailgun"


def test_resend_permanent_error_also_falls_back(client, db_session, mail_state):
    """A 4xx from Resend (e.g. bad API key) says nothing about Mailgun —
    the fallback still gets its chance."""
    mail_state["errors"]["resend"] = mailer.MailerError("resend HTTP 401: bad key")
    _handle("user.registered", {"user_id": _uid(), "email": "fb2@example.com",
                                "verification_token": "v"})
    (row,) = _logs(db_session)
    assert (row.status, row.provider) == ("sent", "mailgun")


def test_both_providers_transient_raises_for_broker_retry(client, db_session,
                                                          mail_state, published_events):
    """Transient everywhere -> raise WITHOUT committing: no processed_events
    row, no log row — the broker redelivers and the retry sends exactly one
    email once a provider recovers."""
    mail_state["errors"]["resend"] = mailer.MailerTransientError("resend HTTP 503")
    mail_state["errors"]["mailgun"] = mailer.MailerTransientError("mailgun HTTP 502")
    event_id = _uid()
    with pytest.raises(mailer.MailerTransientError):
        consumer.handle_event("user.registered",
                              {"user_id": _uid(), "email": "retry@example.com",
                               "verification_token": "v"}, event_id)
    assert _logs(db_session) == []
    assert db_session.get(ProcessedEvent, event_id) is None
    assert published_events == []

    # Provider recovers -> the redelivered event sends exactly once.
    mail_state["errors"].clear()
    consumer.handle_event("user.registered",
                          {"user_id": _uid(), "email": "retry@example.com",
                           "verification_token": "v"}, event_id)
    (row,) = _logs(db_session)
    assert row.status == "sent"
    assert len(mail_state["resend_calls"]) == 2  # first (failed) + retry


def test_permanent_failure_on_all_providers_is_logged_not_retried(client, db_session,
                                                                  mail_state,
                                                                  published_events):
    mail_state["errors"]["resend"] = mailer.MailerError("resend HTTP 422: bad address")
    mail_state["errors"]["mailgun"] = mailer.MailerError("mailgun HTTP 400: bad address")
    event_id = _handle("user.registered", {"user_id": _uid(),
                                           "email": "bad@@example.com",
                                           "verification_token": "v"})
    (row,) = _logs(db_session)
    assert (row.status, row.provider) == ("failed", None)
    assert "resend HTTP 422" in row.error and "mailgun HTTP 400" in row.error
    assert db_session.get(ProcessedEvent, event_id) is not None  # never retried
    assert published_events == []  # notification.sent only for sent mail


def test_resend_transient_without_mailgun_configured_retries(client, db_session,
                                                             mail_state, monkeypatch):
    """Mailgun unconfigured -> a transient Resend failure must still map to
    the broker-retry path, not a silent failure."""
    monkeypatch.delenv("MAILGUN_API_KEY")
    mail_state["errors"]["resend"] = mailer.MailerTransientError("resend HTTP 503")
    with pytest.raises(mailer.MailerTransientError):
        consumer.handle_event("user.registered",
                              {"user_id": _uid(), "email": "x@example.com",
                               "verification_token": "v"}, _uid())
    assert mail_state["mailgun_calls"] == []  # never attempted
    assert _logs(db_session) == []


def test_no_provider_configured_fails_and_logs(client, db_session, mail_state,
                                               monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY")
    monkeypatch.delenv("MAILGUN_API_KEY")
    event_id = _handle("user.registered", {"user_id": _uid(),
                                           "email": "x@example.com",
                                           "verification_token": "v"})
    assert mail_state["resend_calls"] == [] and mail_state["mailgun_calls"] == []
    (row,) = _logs(db_session)
    assert row.status == "failed"
    assert "no email provider configured" in row.error
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_real_provider_functions_hit_the_dead_address_guard(client):
    """Belt and braces: the UNMOCKED provider functions point at
    http://127.0.0.1:9 — an unmocked call fails instantly as transient
    instead of reaching Resend/Mailgun."""
    with pytest.raises(mailer.MailerTransientError):
        REAL_SEND_VIA_RESEND("x@example.com", "s", "b")
    with pytest.raises(mailer.MailerTransientError):
        REAL_SEND_VIA_MAILGUN("x@example.com", "s", "b")


# --------------------------------------------------------------------------
# Idempotency — a redelivered event never double-sends
# --------------------------------------------------------------------------

def test_redelivered_event_sends_once(client, db_session, mail_state):
    event_id = _uid()
    data = {"user_id": _uid(), "email": "once@example.com",
            "verification_token": "v"}
    consumer.handle_event("user.registered", data, event_id)
    consumer.handle_event("user.registered", data, event_id)  # broker redelivery
    assert len(mail_state["resend_calls"]) == 1
    assert len(_logs(db_session)) == 1


def test_redelivered_account_alert_sends_once(client, db_session, mail_state):
    user_id = _register_user("alert@example.com")
    mail_state["resend_calls"].clear()
    event_id = _uid()
    data = {"account_id": user_id, "threshold_percent": 100,
            "used_tokens": 100000, "quota_tokens": 100000, "period": "2026-07"}
    consumer.handle_event("usage.threshold_reached", data, event_id)
    consumer.handle_event("usage.threshold_reached", data, event_id)
    assert len(mail_state["resend_calls"]) == 1
    assert len([r for r in _logs(db_session) if r.template == "quota_threshold_100"]) == 1


# --------------------------------------------------------------------------
# Malformed payloads never poison the queue
# --------------------------------------------------------------------------

def test_missing_payload_email_is_logged_failed_not_raised(client, db_session,
                                                           mail_state):
    event_id = _handle("user.registered", {"user_id": _uid(),
                                           "verification_token": "v"})  # no email
    assert mail_state["resend_calls"] == []
    (row,) = _logs(db_session)
    assert (row.status, row.recipient) == ("failed", None)
    assert db_session.get(ProcessedEvent, event_id) is not None


def test_missing_optional_fields_render_as_blank(client, mail_state):
    """A producer omitting an optional field must degrade, not dead-letter."""
    user_id = _register_user("sparse@example.com")
    mail_state["resend_calls"].clear()
    _handle("invoice.paid", {"account_id": user_id})  # no plan/amount/invoice id
    call = mail_state["resend_calls"][0]
    assert call["subject"] == "Your CreditFlow payment receipt"
    assert "{" not in call["text"]  # nothing unrendered leaked through


# --------------------------------------------------------------------------
# Read routes — auth + tenant isolation
# --------------------------------------------------------------------------

def test_read_routes_require_token(client):
    assert client.get("/notifications").status_code == 401
    assert client.get("/notifications/some-id").status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get("/notifications", headers=bad).status_code == 401


def test_list_is_tenant_scoped(client, mail_state):
    user_a = _register_user("a@example.com")
    user_b = _register_user("b@example.com")

    resp = client.get("/notifications", headers=_auth(user_a))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["template"] == "verification"
    assert body["items"][0]["recipient"] == "a@example.com"
    # The other tenant's row never leaks.
    assert all(item["account_id"] == user_a for item in body["items"])

    resp_b = client.get("/notifications", headers=_auth(user_b))
    assert resp_b.json()["items"][0]["recipient"] == "b@example.com"


def test_list_filters_by_status_and_template(client, mail_state):
    user_id = _register_user("filters@example.com")
    mail_state["errors"]["resend"] = mailer.MailerError("resend HTTP 422")
    mail_state["errors"]["mailgun"] = mailer.MailerError("mailgun HTTP 400")
    _handle("usage.threshold_reached", {"account_id": user_id, "threshold_percent": 80,
                                        "used_tokens": 1, "quota_tokens": 2,
                                        "period": "2026-07"})
    headers = _auth(user_id)
    assert client.get("/notifications?status=sent", headers=headers).json()["total"] == 1
    assert client.get("/notifications?status=failed", headers=headers).json()["total"] == 1
    assert client.get("/notifications?template=verification",
                      headers=headers).json()["total"] == 1
    assert client.get("/notifications?status=bogus", headers=headers).status_code == 422


def test_get_notification_cross_account_answers_404(client, mail_state):
    user_a = _register_user("a2@example.com")
    _register_user("b2@example.com")
    notification_id = client.get("/notifications",
                                 headers=_auth(user_a)).json()["items"][0]["notification_id"]

    own = client.get(f"/notifications/{notification_id}", headers=_auth(user_a))
    assert own.status_code == 200
    assert own.json()["subject"] == "Verify your CreditFlow email"

    other = client.get(f"/notifications/{notification_id}", headers=_auth(_uid()))
    assert other.status_code == 404
    assert client.get(f"/notifications/{_uid()}", headers=_auth(user_a)).status_code == 404


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------
# templates.render — the spec's notification list is the source of truth
# --------------------------------------------------------------------------

def test_render_covers_exactly_the_spec_notifications():
    notifiable = {
        "user.registered", "user.password_reset_requested", "invite.created",
        "member.joined", "invoice.paid", "payment.failed",
        "subscription.downgraded", "usage.threshold_reached",
        "credits.low_balance", "post.published", "post.failed",
    }
    for key in notifiable:
        assert templates.render(key, {}) is not None, key
    for key in ("user.logged_in", "account.created", "account.updated",
                "subscription.updated", "refund.issued", "ai.generation_failed",
                "credits.credited", "credits.debited", "content.scheduled"):
        assert templates.render(key, {}) is None, key
