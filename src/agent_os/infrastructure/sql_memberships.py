"""Tenant membership and invitation store for provider-neutral multi-user access."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from typing import Any, Mapping

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import MembershipStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


membership_metadata = MetaData()
_MEMBER_ROLES = frozenset({"owner", "operator", "viewer"})


memberships = Table(
    "aos_v2_memberships",
    membership_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("subject_id", String(256), primary_key=True),
    Column("roles", JSON, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("invitation_id", String(96), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("revoked_by", String(256), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_reason", Text, nullable=True),
    Column("revocation_key", String(200), nullable=True),
)

invitations = Table(
    "aos_v2_invitations",
    membership_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("invitation_id", String(96), primary_key=True),
    Column("token_digest", String(64), nullable=False),
    Column("roles", JSON, nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("expires_in_seconds", Integer, nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("claimed_by", String(256), nullable=True),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key"),
    UniqueConstraint("tenant_id", "token_digest"),
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SQLMembershipStore(MembershipStore):
    def __init__(
        self,
        database_url: str,
        *,
        signing_secret: str | bytes,
        create_schema: bool = False,
        clock=None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._secret = signing_secret.encode() if isinstance(signing_secret, str) else signing_secret
        if len(self._secret) < 32:
            raise ValueError("membership invitation signing secret must be at least 32 bytes")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._experience_events = SQLExperienceEventLog(
            lambda tenant_id: self._connection(tenant_id=tenant_id),
        )
        if create_schema:
            membership_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

    @contextmanager
    def _connection(self, *, tenant_id: str | None = None, subject_id: str | None = None):
        if tenant_id is None and subject_id is None:
            raise ValueError("membership query requires tenant or subject scope")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                if tenant_id is not None:
                    connection.execute(
                        text("SELECT set_config('app.tenant_id', :value, true)"),
                        {"value": tenant_id},
                    )
                if subject_id is not None:
                    connection.execute(
                        text("SELECT set_config('app.subject_id', :value, true)"),
                        {"value": subject_id},
                    )
            yield connection

    @staticmethod
    def _roles(raw: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        roles = tuple(sorted(set(str(item).strip() for item in raw if str(item).strip())))
        if not roles or set(roles) - _MEMBER_ROLES:
            raise ValueError("membership roles must be owner, operator, or viewer")
        return roles

    def _invitation_id(self, tenant_id: str, idempotency_key: str) -> str:
        material = f"agent-os:invitation:v1:{tenant_id}:{idempotency_key}".encode()
        return "invitation-" + hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def _token(self, tenant_id: str, invitation_id: str) -> str:
        tenant = _b64(tenant_id.encode())
        invitation = _b64(invitation_id.encode())
        material = f"aosinv.{tenant}.{invitation}".encode()
        signature = _b64(hmac.new(
            self._secret, b"agent-os:invitation-token:v1:" + material, hashlib.sha256,
        ).digest())
        return f"{material.decode()}.{signature}"

    def _parse_token(self, token: str) -> tuple[str, str]:
        if len(token) > 2_000:
            raise ValueError("invitation token is invalid")
        try:
            prefix, tenant_raw, invitation_raw, supplied = token.split(".")
            material = f"{prefix}.{tenant_raw}.{invitation_raw}".encode()
            expected = _b64(hmac.new(
                self._secret, b"agent-os:invitation-token:v1:" + material, hashlib.sha256,
            ).digest())
            if prefix != "aosinv" or not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            tenant_id = _decode(tenant_raw).decode()
            invitation_id = _decode(invitation_raw).decode()
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            raise ValueError("invitation token is invalid") from exc
        if not tenant_id or not invitation_id.startswith("invitation-"):
            raise ValueError("invitation token is invalid")
        return tenant_id, invitation_id

    def roles_for(self, tenant_id: str, subject_id: str) -> frozenset[str] | None:
        with self._connection(tenant_id=tenant_id) as connection:
            row = connection.execute(select(
                memberships.c.roles, memberships.c.active,
            ).where(and_(
                memberships.c.tenant_id == tenant_id,
                memberships.c.subject_id == subject_id,
            ))).mappings().one_or_none()
        if row is None or not row["active"]:
            return None
        return frozenset(self._roles(list(row["roles"])))

    def organizations_for(self, subject_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._connection(subject_id=subject_id) as connection:
            rows = connection.execute(select(
                memberships.c.tenant_id,
                memberships.c.roles,
                memberships.c.created_at,
            ).where(and_(
                memberships.c.subject_id == subject_id,
                memberships.c.active.is_(True),
            )).order_by(memberships.c.tenant_id).limit(256)).mappings().all()
        return tuple({
            "organization_id": row["tenant_id"],
            "roles": list(self._roles(list(row["roles"]))),
            "joined_at": _utc(row["created_at"]).isoformat(),
        } for row in rows)

    def list_members(self, tenant_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._connection(tenant_id=tenant_id) as connection:
            rows = connection.execute(select(memberships).where(
                memberships.c.tenant_id == tenant_id,
            ).order_by(memberships.c.subject_id).limit(1_000)).mappings().all()
        return tuple({
            "subject_id": row["subject_id"],
            "roles": list(self._roles(list(row["roles"]))),
            "active": bool(row["active"]),
            "joined_at": _utc(row["created_at"]).isoformat(),
            "revoked_at": None if row["revoked_at"] is None else _utc(row["revoked_at"]).isoformat(),
            "revoked_reason": row["revoked_reason"],
        } for row in rows)

    def create_invitation(
        self,
        *,
        tenant_id: str,
        roles: tuple[str, ...],
        actor_id: str,
        expires_in_seconds: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        roles = self._roles(roles)
        idempotency_key = idempotency_key.strip()
        if (
            not tenant_id.strip() or len(tenant_id) > 128
            or not actor_id.strip() or len(actor_id) > 255
            or not 8 <= len(idempotency_key) <= 200
            or not 300 <= expires_in_seconds <= 30 * 24 * 60 * 60
        ):
            raise ValueError("invitation identity or expiry is invalid")
        invitation_id = self._invitation_id(tenant_id, idempotency_key)
        token = self._token(tenant_id, invitation_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = _utc(self._clock())
        expires_at = now + timedelta(seconds=expires_in_seconds)
        try:
            with self._connection(tenant_id=tenant_id) as connection:
                prior = connection.execute(select(invitations).where(and_(
                    invitations.c.tenant_id == tenant_id,
                    invitations.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
                if prior is not None:
                    if (
                        tuple(prior["roles"]) != roles
                        or prior["expires_in_seconds"] != expires_in_seconds
                    ):
                        raise ValueError("invitation idempotency key was reused with different terms")
                    return self._invitation_record(prior, token, duplicate=True)
                connection.execute(insert(invitations).values(
                    tenant_id=tenant_id,
                    invitation_id=invitation_id,
                    token_digest=digest,
                    roles=list(roles),
                    idempotency_key=idempotency_key,
                    expires_in_seconds=expires_in_seconds,
                    created_by=actor_id,
                    created_at=now,
                    expires_at=expires_at,
                ))
                self._experience_events.append(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key(
                        "membership.invitation.created", invitation_id,
                    ),
                    resource_type="invitation",
                    resource_id=invitation_id,
                    projection_revision=1,
                    kind="membership.invitation.created",
                    audience_ids=("tenant:members",),
                    safe_summary="A membership invitation was created.",
                    occurred_at=now,
                )
        except IntegrityError as exc:
            raise ValueError("invitation registration conflicted with another writer") from exc
        return {
            "invitation_id": invitation_id,
            "organization_id": tenant_id,
            "roles": list(roles),
            "created_by": actor_id,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "claimed_by": None,
            "claimed_at": None,
            "claim_token": token,
            "duplicate": False,
        }

    @staticmethod
    def _invitation_record(
        row: Mapping[str, Any], token: str, *, duplicate: bool,
    ) -> Mapping[str, Any]:
        return {
            "invitation_id": row["invitation_id"],
            "organization_id": row["tenant_id"],
            "roles": list(row["roles"]),
            "created_by": row["created_by"],
            "created_at": _utc(row["created_at"]).isoformat(),
            "expires_at": _utc(row["expires_at"]).isoformat(),
            "claimed_by": row["claimed_by"],
            "claimed_at": None if row["claimed_at"] is None else _utc(row["claimed_at"]).isoformat(),
            "claim_token": token,
            "duplicate": duplicate,
        }

    def claim_invitation(
        self,
        *,
        token: str,
        subject_id: str,
    ) -> Mapping[str, Any]:
        if not subject_id.strip() or len(subject_id) > 255:
            raise ValueError("invitation claimant identity is invalid")
        tenant_id, invitation_id = self._parse_token(token)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connection(tenant_id=tenant_id) as connection:
            invitation = connection.execute(select(invitations).where(and_(
                invitations.c.tenant_id == tenant_id,
                invitations.c.invitation_id == invitation_id,
            )).with_for_update()).mappings().one_or_none()
            if invitation is None or not hmac.compare_digest(invitation["token_digest"], digest):
                raise LookupError("invitation does not exist")
            now = _utc(self._clock())
            if _utc(invitation["expires_at"]) <= now:
                raise ValueError("invitation has expired")
            if invitation["claimed_by"] is not None:
                if invitation["claimed_by"] != subject_id:
                    raise ValueError("invitation was already claimed")
                return {
                    "organization_id": tenant_id,
                    "subject_id": subject_id,
                    "roles": list(invitation["roles"]),
                    "duplicate": True,
                }
            prior = connection.execute(select(memberships).where(and_(
                memberships.c.tenant_id == tenant_id,
                memberships.c.subject_id == subject_id,
            )).with_for_update()).mappings().one_or_none()
            roles = self._roles(list(invitation["roles"]))
            if prior is None:
                connection.execute(insert(memberships).values(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    roles=list(roles),
                    active=True,
                    invitation_id=invitation_id,
                    created_at=now,
                    updated_at=now,
                ))
            else:
                combined = self._roles(list(prior["roles"]) + list(roles))
                connection.execute(update(memberships).where(and_(
                    memberships.c.tenant_id == tenant_id,
                    memberships.c.subject_id == subject_id,
                )).values(
                    roles=list(combined), active=True, invitation_id=invitation_id,
                    updated_at=now, revoked_by=None, revoked_at=None,
                    revoked_reason=None, revocation_key=None,
                ))
                roles = combined
            connection.execute(update(invitations).where(and_(
                invitations.c.tenant_id == tenant_id,
                invitations.c.invitation_id == invitation_id,
            )).values(claimed_by=subject_id, claimed_at=now))
            self._experience_events.append(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "membership.invitation.claimed", invitation_id, subject_id,
                ),
                resource_type="membership",
                resource_id=subject_id,
                projection_revision=1,
                kind="membership.invitation.claimed",
                audience_ids=("tenant:members", subject_id),
                safe_summary="A member joined the organization.",
                occurred_at=now,
            )
        return {
            "organization_id": tenant_id,
            "subject_id": subject_id,
            "roles": list(roles),
            "duplicate": False,
        }

    def revoke_member(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if actor_id == subject_id:
            raise ValueError("an owner cannot revoke their own active session")
        if not reason or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("membership revocation reason and idempotency key are required")
        key = and_(
            memberships.c.tenant_id == tenant_id,
            memberships.c.subject_id == subject_id,
        )
        with self._connection(tenant_id=tenant_id) as connection:
            row = connection.execute(select(memberships).where(key).with_for_update()).mappings().one_or_none()
            if row is None:
                return None
            if not row["active"]:
                if row["revocation_key"] != idempotency_key:
                    raise ValueError("membership was already revoked by another decision")
                return {"subject_id": subject_id, "active": False, "duplicate": True}
            now = _utc(self._clock())
            connection.execute(update(memberships).where(key).values(
                active=False, updated_at=now, revoked_by=actor_id, revoked_at=now,
                revoked_reason=reason, revocation_key=idempotency_key,
            ))
            self._experience_events.append(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "membership.revoked", subject_id, idempotency_key,
                ),
                resource_type="membership",
                resource_id=subject_id,
                projection_revision=2,
                kind="membership.revoked",
                audience_ids=("tenant:members", subject_id),
                safe_summary="Organization membership was revoked.",
                occurred_at=now,
            )
        return {
            "subject_id": subject_id,
            "active": False,
            "revoked_by": actor_id,
            "revoked_at": now.isoformat(),
            "revoked_reason": reason,
            "duplicate": False,
        }

    def close(self) -> None:
        self._engine.dispose()
