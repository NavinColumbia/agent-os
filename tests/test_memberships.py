from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_os.application.ports import MembershipStore
from agent_os.infrastructure.sql_memberships import SQLMembershipStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


SECRET = "membership-test-secret-that-is-long-enough"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 13, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def store(tmp_path: Path, clock: Clock | None = None) -> SQLMembershipStore:
    return SQLMembershipStore(
        f"sqlite:///{tmp_path / 'memberships.sqlite3'}",
        signing_secret=SECRET,
        create_schema=True,
        clock=clock,
    )


def test_invitation_claim_membership_selection_and_revocation_are_idempotent(tmp_path: Path):
    memberships = store(tmp_path)
    assert isinstance(memberships, MembershipStore)
    try:
        invitation = memberships.create_invitation(
            tenant_id="org-a",
            roles=("viewer", "operator", "viewer"),
            actor_id="owner-a",
            expires_in_seconds=3600,
            idempotency_key="invite-user-b",
        )
        duplicate = memberships.create_invitation(
            tenant_id="org-a",
            roles=("operator", "viewer"),
            actor_id="owner-a",
            expires_in_seconds=3600,
            idempotency_key="invite-user-b",
        )
        assert duplicate["claim_token"] == invitation["claim_token"]
        assert duplicate["duplicate"] is True

        claimed = memberships.claim_invitation(
            token=invitation["claim_token"], subject_id="user-b",
        )
        repeated = memberships.claim_invitation(
            token=invitation["claim_token"], subject_id="user-b",
        )
        assert claimed == {
            "organization_id": "org-a",
            "subject_id": "user-b",
            "roles": ["operator", "viewer"],
            "duplicate": False,
        }
        assert repeated["duplicate"] is True
        assert memberships.roles_for("org-a", "user-b") == frozenset({"operator", "viewer"})
        assert memberships.roles_for("org-b", "user-b") is None
        assert memberships.organizations_for("user-b")[0]["organization_id"] == "org-a"
        assert memberships.list_members("org-b") == ()

        revoked = memberships.revoke_member(
            tenant_id="org-a", subject_id="user-b", actor_id="owner-a",
            reason="Access no longer required", idempotency_key="revoke-user-b",
        )
        repeated_revoke = memberships.revoke_member(
            tenant_id="org-a", subject_id="user-b", actor_id="owner-a",
            reason="Access no longer required", idempotency_key="revoke-user-b",
        )
        assert revoked is not None and revoked["active"] is False
        assert repeated_revoke == {"subject_id": "user-b", "active": False, "duplicate": True}
        assert memberships.roles_for("org-a", "user-b") is None
        assert memberships.organizations_for("user-b") == ()
        experience = SQLNotificationStore(
            f"sqlite:///{tmp_path / 'memberships.sqlite3'}", create_schema=True,
        )
        events = experience.list_experience_events(
            "org-a", audience_ids=("tenant:members",), limit=100,
        ).events
        assert [event["kind"] for event in events] == [
            "membership.invitation.created",
            "membership.invitation.claimed",
            "membership.revoked",
        ]
        assert "Access no longer required" not in " ".join(
            event["safe_summary"] for event in events
        )
        assert not experience.list_experience_events(
            "org-b", audience_ids=("tenant:members",), limit=100,
        ).events
        experience.close()
    finally:
        memberships.close()


def test_invitation_tokens_are_single_claimant_tamper_evident_and_expiring(tmp_path: Path):
    clock = Clock()
    memberships = store(tmp_path, clock)
    try:
        invitation = memberships.create_invitation(
            tenant_id="org-a", roles=("viewer",), actor_id="owner-a",
            expires_in_seconds=300, idempotency_key="short-invite",
        )
        token = str(invitation["claim_token"])
        with pytest.raises(ValueError, match="invalid"):
            memberships.claim_invitation(token=token[:-1] + "x", subject_id="user-b")

        memberships.claim_invitation(token=token, subject_id="user-b")
        with pytest.raises(ValueError, match="already claimed"):
            memberships.claim_invitation(token=token, subject_id="user-c")

        later = memberships.create_invitation(
            tenant_id="org-a", roles=("viewer",), actor_id="owner-a",
            expires_in_seconds=300, idempotency_key="expiring-invite",
        )
        clock.now += timedelta(seconds=301)
        with pytest.raises(ValueError, match="expired"):
            memberships.claim_invitation(token=later["claim_token"], subject_id="user-c")
    finally:
        memberships.close()


def test_membership_authority_validates_roles_and_prevents_self_revocation(tmp_path: Path):
    memberships = store(tmp_path)
    try:
        with pytest.raises(ValueError, match="roles"):
            memberships.create_invitation(
                tenant_id="org-a", roles=("system",), actor_id="owner-a",
                expires_in_seconds=3600, idempotency_key="invalid-role",
            )
        assert memberships.revoke_member(
            tenant_id="org-a", subject_id="missing", actor_id="owner-a",
            reason="Not present", idempotency_key="missing-user",
        ) is None
        with pytest.raises(ValueError, match="own active session"):
            memberships.revoke_member(
                tenant_id="org-a", subject_id="owner-a", actor_id="owner-a",
                reason="Unsafe", idempotency_key="self-revoke",
            )
    finally:
        memberships.close()


def test_membership_migration_forces_tenant_and_subject_scoped_rls():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/99zzz-memberships-v2.sql"
    ).read_text()
    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert "subject_id = current_setting('app.subject_id', true)" in migration
    assert "tenant_id = current_setting('app.tenant_id', true)" in migration
    assert "GRANT SELECT, INSERT, UPDATE" in migration
    assert "GRANT DELETE" not in migration
    assert "token_digest" in migration
