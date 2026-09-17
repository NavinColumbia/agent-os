"""Local AuthZEN-shaped effect policy evaluation.

The runtime keeps policy evaluation in-process so a control-plane outage cannot
silently bypass or stall authorization.  Cedar/OPA adapters can implement the
same ``EffectPolicyEngine`` contract without changing the assurance kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import json
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_os.application.assurance import PolicyDecision


@dataclass(frozen=True)
class EffectPolicyRule:
    rule_id: str
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    risks: frozenset[str] = field(default_factory=frozenset)
    allowed: bool = True
    obligations: tuple[str, ...] = ()
    reason: str = "matched local effect policy"

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.actions or not self.resources:
            raise ValueError("policy rules require identity, action scopes, and resource scopes")

    def matches(
        self,
        action: Mapping[str, Any],
        resource: Mapping[str, Any],
    ) -> bool:
        name = str(action.get("name") or "")
        identifier = str(resource.get("id") or "")
        risk = str(action.get("risk") or "")
        return (
            any(fnmatchcase(name, item) for item in self.actions)
            and any(fnmatchcase(identifier, item) for item in self.resources)
            and (not self.risks or risk in self.risks)
        )


class LocalAuthZenPolicy:
    """Ordered local policy bundle with explicit default behavior."""

    def __init__(
        self,
        *,
        version: str,
        rules: tuple[EffectPolicyRule, ...] = (),
        default_allowed: bool = True,
    ) -> None:
        if not version.strip() or len({item.rule_id for item in rules}) != len(rules):
            raise ValueError("policy bundle needs a version and unique rule IDs")
        self.version = version
        self.rules = rules
        self.default_allowed = default_allowed

    @classmethod
    def from_signed_bundle(
        cls,
        bundle: Mapping[str, Any],
        *,
        public_key: Ed25519PublicKey,
        now: datetime | None = None,
    ) -> "LocalAuthZenPolicy":
        """Verify and materialize a locally evaluated policy bundle.

        Only the signed payload influences decisions. Distribution metadata is
        deliberately outside the trust boundary.
        """

        payload = bundle.get("payload")
        signature_raw = bundle.get("signature")
        if not isinstance(payload, Mapping) or not isinstance(signature_raw, str):
            raise ValueError("signed policy bundle requires payload and signature")
        encoded = json.dumps(
            payload, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode()
        try:
            signature = base64.urlsafe_b64decode(
                signature_raw + "=" * (-len(signature_raw) % 4)
            )
            public_key.verify(signature, encoded)
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("policy bundle signature is invalid") from exc
        version = str(payload.get("version") or "")
        issued_at = str(payload.get("issued_at") or "")
        expires_at = str(payload.get("expires_at") or "")
        try:
            issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("policy bundle timestamps are invalid") from exc
        if issued.tzinfo is None or expires.tzinfo is None or expires <= issued:
            raise ValueError("policy bundle timestamps require an ordered timezone-aware interval")
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not issued.astimezone(timezone.utc) <= instant < expires.astimezone(timezone.utc):
            raise ValueError("policy bundle is not active")
        raw_rules = payload.get("rules", ())
        if not isinstance(raw_rules, list):
            raise ValueError("policy bundle rules must be a list")
        rules: list[EffectPolicyRule] = []
        for raw in raw_rules:
            if not isinstance(raw, Mapping):
                raise ValueError("policy bundle rule must be an object")
            rules.append(EffectPolicyRule(
                rule_id=str(raw.get("rule_id") or ""),
                actions=tuple(str(item) for item in raw.get("actions", ())),
                resources=tuple(str(item) for item in raw.get("resources", ())),
                risks=frozenset(str(item) for item in raw.get("risks", ())),
                allowed=bool(raw.get("allowed", False)),
                obligations=tuple(str(item) for item in raw.get("obligations", ())),
                reason=str(raw.get("reason") or "matched signed local policy"),
            ))
        return cls(
            version=version,
            rules=tuple(rules),
            default_allowed=bool(payload.get("default_allowed", False)),
        )

    def evaluate(
        self,
        *,
        subject: Mapping[str, Any],
        action: Mapping[str, Any],
        resource: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> PolicyDecision:
        if not subject.get("id") or not action.get("name") or not resource.get("id"):
            return PolicyDecision(
                False, self.version, ("authorization request is incomplete",),
            )
        if resource.get("tenant_id") is None or context.get("mission_id") is None:
            return PolicyDecision(
                False, self.version, ("authorization context omits tenant or mission",),
            )
        for rule in self.rules:
            if rule.matches(action, resource):
                return PolicyDecision(
                    allowed=rule.allowed,
                    policy_version=self.version,
                    reasons=(f"policy rule {rule.rule_id}: {rule.reason}",),
                    obligations=rule.obligations,
                )
        if self.default_allowed:
            return PolicyDecision(
                True,
                self.version,
                ("local policy delegates the scoped decision to the assurance kernel",),
                ("record authorization decision",),
            )
        return PolicyDecision(
            False, self.version, ("no local policy rule authorized the effect",),
        )


def baseline_effect_policy() -> LocalAuthZenPolicy:
    """Conservative baseline; mission authority remains the narrower boundary."""

    return LocalAuthZenPolicy(
        version="agent-os-baseline-policy-v1",
        rules=(
            EffectPolicyRule(
                rule_id="deny-secret-export",
                actions=("secret.export", "credential.export", "*.exfiltrate"),
                resources=("*",),
                allowed=False,
                reason="secret and credential export is prohibited",
            ),
            EffectPolicyRule(
                rule_id="irreversible-needs-receipt",
                actions=("*",),
                resources=("*",),
                risks=frozenset({"irreversible"}),
                allowed=True,
                obligations=("retain effect-bound human approval receipt",),
                reason="irreversible effects remain subject to the kernel human-release gate",
            ),
        ),
        default_allowed=True,
    )
