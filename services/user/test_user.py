"""
User/Tenant service tests: individual-account auto-creation from
user.registered (idempotency both by event_id and by user), create-team,
invite -> accept -> member.joined, RBAC on role update / removal, the
account switcher, and the default-account endpoint Auth will call at login.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from creditflow_common import jwt_utils

import consumer
from models import Account, AccountMember, Invite, utcnow


def _uid() -> str:
    return str(uuid.uuid4())


def _auth(user_id: str, account_id: str | None = None, role: str = "owner") -> dict:
    """Bearer header signed with the test keypair — mimics what Auth issues
    (account_id defaults to user_id, Auth's current placeholder)."""
    token, _ = jwt_utils.sign_access_token(user_id, account_id or user_id, role)
    return {"Authorization": f"Bearer {token}"}


def _register(user_id: str, email: str | None = None, event_id: str | None = None) -> None:
    """Feed a user.registered event through the real consumer handler."""
    consumer.handle_event(
        "user.registered",
        {"user_id": user_id, "email": email or f"{user_id[:8]}@example.com"},
        event_id or str(uuid.uuid4()),
    )


def _create_team(client, owner_id: str, name: str = "Acme Inc") -> str:
    r = client.post("/accounts", json={"name": name}, headers=_auth(owner_id))
    assert r.status_code == 201, r.text
    return r.json()["account_id"]


def _invite_and_accept(client, account_id: str, manager_id: str, invitee_id: str,
                       role: str = "member") -> None:
    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": f"{invitee_id[:8]}@example.com", "role": role},
                    headers=_auth(manager_id))
    assert r.status_code == 201, r.text
    r = client.post("/invites/accept", json={"token": r.json()["dev_invite_token"]},
                    headers=_auth(invitee_id))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# user.registered consumer -> individual account
# --------------------------------------------------------------------------

def test_user_registered_creates_individual_account(client, published_events):
    uid = _uid()
    _register(uid, "alice@example.com")

    r = client.get(f"/users/{uid}/default-account")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "owner"
    assert body["user_id"] == uid

    r = client.get(f"/accounts/{body['account_id']}", headers=_auth(uid))
    assert r.status_code == 200
    profile = r.json()
    assert profile["type"] == "individual"
    assert profile["name"] == "alice@example.com"
    assert profile["plan_tier"] == "free"
    assert profile["seat_count"] == 1

    # exactly one account.created, carrying the owner
    created = [p for rk, p in published_events if rk == "account.created"]
    assert len(created) == 1 and created[0]["owner_user_id"] == uid


def test_user_registered_redelivery_same_event_id_is_noop(client, db_session):
    uid = _uid()
    event_id = str(uuid.uuid4())
    _register(uid, event_id=event_id)
    _register(uid, event_id=event_id)  # broker redelivery

    accounts = db_session.scalars(
        select(Account).join(AccountMember, AccountMember.account_id == Account.id)
        .where(AccountMember.user_id == uid)
    ).all()
    assert len(accounts) == 1


def test_user_registered_reemit_with_fresh_event_id_is_noop(client, db_session):
    uid = _uid()
    _register(uid)
    _register(uid)  # producer re-emitted: new event_id, same user

    accounts = db_session.scalars(
        select(Account).join(AccountMember, AccountMember.account_id == Account.id)
        .where(AccountMember.user_id == uid)
    ).all()
    assert len(accounts) == 1


def test_default_account_404_for_unknown_user(client):
    assert client.get(f"/users/{_uid()}/default-account").status_code == 404


# --------------------------------------------------------------------------
# Create team
# --------------------------------------------------------------------------

def test_create_team_creator_becomes_owner(client, published_events):
    uid = _uid()
    account_id = _create_team(client, uid, "My Team")

    r = client.get(f"/accounts/{account_id}", headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["type"] == "team"
    assert r.json()["seat_count"] == 1

    r = client.get(f"/accounts/{account_id}/members", headers=_auth(uid))
    assert r.json()["members"] == [
        {"user_id": uid, "role": "owner", "joined_at": r.json()["members"][0]["joined_at"]}
    ]
    assert ("account.created", ) in [(rk,) for rk, _ in published_events]


def test_create_team_requires_token(client):
    assert client.post("/accounts", json={"name": "NoAuth"}).status_code == 401


def test_account_profile_hidden_from_non_members(client):
    account_id = _create_team(client, _uid())
    assert client.get(f"/accounts/{account_id}", headers=_auth(_uid())).status_code == 403
    assert client.get(f"/accounts/{uuid.uuid4()}", headers=_auth(_uid())).status_code == 404


# --------------------------------------------------------------------------
# Invite -> accept -> member.joined
# --------------------------------------------------------------------------

def test_invite_accept_flow(client, published_events):
    owner, invitee = _uid(), _uid()
    account_id = _create_team(client, owner)

    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "bob@example.com", "role": "member"},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    token = r.json()["dev_invite_token"]
    invite_events = [p for rk, p in published_events if rk == "invite.created"]
    assert len(invite_events) == 1
    assert invite_events[0]["invite_token"] == token  # Notification gets the raw token
    assert invite_events[0]["email"] == "bob@example.com"

    r = client.post("/invites/accept", json={"token": token}, headers=_auth(invitee))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "member"

    joined = [p for rk, p in published_events if rk == "member.joined"]
    assert len(joined) == 1
    assert joined[0]["user_id"] == invitee and joined[0]["account_id"] == account_id

    # seat count reflects the new member; switcher shows the membership
    r = client.get(f"/accounts/{account_id}", headers=_auth(invitee))
    assert r.status_code == 200 and r.json()["seat_count"] == 2


def test_invite_requires_owner_or_admin(client):
    owner, member, outsider = _uid(), _uid(), _uid()
    account_id = _create_team(client, owner)
    _invite_and_accept(client, account_id, owner, member)

    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "x@example.com"}, headers=_auth(member))
    assert r.status_code == 403
    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "x@example.com"}, headers=_auth(outsider))
    assert r.status_code == 403


def test_invite_rejected_on_individual_account(client):
    uid = _uid()
    _register(uid)
    account_id = client.get(f"/users/{uid}/default-account").json()["account_id"]
    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "x@example.com"}, headers=_auth(uid))
    assert r.status_code == 400


def test_invite_token_single_use_and_expiry(client, db_session):
    owner, first, second = _uid(), _uid(), _uid()
    account_id = _create_team(client, owner)

    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "reuse@example.com"}, headers=_auth(owner))
    token = r.json()["dev_invite_token"]
    assert client.post("/invites/accept", json={"token": token}, headers=_auth(first)).status_code == 200
    # single-use: second redemption fails even for a different user
    assert client.post("/invites/accept", json={"token": token}, headers=_auth(second)).status_code == 400

    # expired invite is rejected
    r = client.post(f"/accounts/{account_id}/invites",
                    json={"email": "late@example.com"}, headers=_auth(owner))
    invite_id, token = r.json()["invite_id"], r.json()["dev_invite_token"]
    invite = db_session.get(Invite, invite_id)
    invite.expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert client.post("/invites/accept", json={"token": token}, headers=_auth(second)).status_code == 400
    assert client.post("/invites/accept", json={"token": "not-a-token"}, headers=_auth(second)).status_code == 400


def test_duplicate_pending_invite_conflicts(client):
    owner = _uid()
    account_id = _create_team(client, owner)
    body = {"email": "dup@example.com", "role": "member"}
    assert client.post(f"/accounts/{account_id}/invites", json=body, headers=_auth(owner)).status_code == 201
    assert client.post(f"/accounts/{account_id}/invites", json=body, headers=_auth(owner)).status_code == 409


# --------------------------------------------------------------------------
# Role update / removal RBAC
# --------------------------------------------------------------------------

def test_role_update_permissions(client, published_events):
    owner, admin, member = _uid(), _uid(), _uid()
    account_id = _create_team(client, owner)
    _invite_and_accept(client, account_id, owner, admin, role="admin")
    _invite_and_accept(client, account_id, owner, member, role="member")

    # a plain member cannot change roles
    r = client.patch(f"/accounts/{account_id}/members/{admin}",
                     json={"role": "member"}, headers=_auth(member))
    assert r.status_code == 403

    # nobody can touch the owner — not even an admin
    r = client.patch(f"/accounts/{account_id}/members/{owner}",
                     json={"role": "member"}, headers=_auth(admin))
    assert r.status_code == 403

    # an admin cannot promote a member to admin (owner-only)
    r = client.patch(f"/accounts/{account_id}/members/{member}",
                     json={"role": "admin"}, headers=_auth(admin))
    assert r.status_code == 403

    # "owner" is not a grantable role at all
    r = client.patch(f"/accounts/{account_id}/members/{member}",
                     json={"role": "owner"}, headers=_auth(owner))
    assert r.status_code == 422

    # the owner promotes a member to admin
    r = client.patch(f"/accounts/{account_id}/members/{member}",
                     json={"role": "admin"}, headers=_auth(owner))
    assert r.status_code == 200 and r.json()["role"] == "admin"
    updates = [p for rk, p in published_events if rk == "account.updated"]
    assert any(p["change"] == "member_role_updated" and p["user_id"] == member for p in updates)


def test_member_removal_permissions(client, published_events):
    owner, admin, admin2, member = _uid(), _uid(), _uid(), _uid()
    account_id = _create_team(client, owner)
    _invite_and_accept(client, account_id, owner, admin, role="admin")
    _invite_and_accept(client, account_id, owner, admin2, role="admin")
    _invite_and_accept(client, account_id, owner, member, role="member")

    # admin cannot remove the owner or a fellow admin
    assert client.delete(f"/accounts/{account_id}/members/{owner}",
                         headers=_auth(admin)).status_code == 403
    assert client.delete(f"/accounts/{account_id}/members/{admin2}",
                         headers=_auth(admin)).status_code == 403

    # admin removes a plain member
    r = client.delete(f"/accounts/{account_id}/members/{member}", headers=_auth(admin))
    assert r.status_code == 200
    assert any(rk == "account.updated" and p["change"] == "member_removed"
               for rk, p in published_events)

    # owner removes an admin; removed users lose access
    assert client.delete(f"/accounts/{account_id}/members/{admin2}",
                         headers=_auth(owner)).status_code == 200
    assert client.get(f"/accounts/{account_id}", headers=_auth(admin2)).status_code == 403
    # removing someone who is not a member -> 404
    assert client.delete(f"/accounts/{account_id}/members/{member}",
                         headers=_auth(owner)).status_code == 404


# --------------------------------------------------------------------------
# Account switcher
# --------------------------------------------------------------------------

def test_account_switcher_lists_all_memberships_with_roles(client):
    uid, team_owner = _uid(), _uid()
    _register(uid, "carol@example.com")           # individual account (owner)
    team_id = _create_team(client, team_owner)    # someone else's team
    _invite_and_accept(client, team_id, team_owner, uid, role="member")

    r = client.get("/users/me/accounts", headers=_auth(uid))
    assert r.status_code == 200
    accounts = {a["account_id"]: a for a in r.json()["accounts"]}
    assert len(accounts) == 2
    individual = client.get(f"/users/{uid}/default-account").json()["account_id"]
    assert accounts[individual]["role"] == "owner"
    assert accounts[individual]["type"] == "individual"
    assert accounts[team_id]["role"] == "member"
    assert accounts[team_id]["type"] == "team"


def test_rename_account_publishes_account_updated(client, published_events):
    owner = _uid()
    account_id = _create_team(client, owner, "Old Name")
    r = client.patch(f"/accounts/{account_id}", json={"name": "New Name"}, headers=_auth(owner))
    assert r.status_code == 200 and r.json()["name"] == "New Name"
    assert any(rk == "account.updated" and p["change"] == "profile" and p["name"] == "New Name"
               for rk, p in published_events)
