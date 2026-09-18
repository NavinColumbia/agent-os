"""Versioned, evidence-bound fault-campaign admission contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from math import isfinite
from typing import Any, Iterable, Mapping


class FaultKind(str, Enum):
    PROVIDER_THROTTLE = "provider_throttle"
    PARTIAL_TOOL_FAILURE = "partial_tool_failure"
    PROCESS_DEATH = "process_death"
    STALE_LEASE = "stale_lease"
    DUPLICATE_EVENT = "duplicate_event"
    ARTIFACT_CORRUPTION = "artifact_corruption"


@dataclass(frozen=True)
class FaultScenario:
    scenario_id: str
    fault_kind: FaultKind
    recovery_deadline_seconds: float
    maximum_data_loss_records: int = 0
    maximum_duplicate_effects: int = 0

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("fault scenario identity is required")
        if (
            not isfinite(self.recovery_deadline_seconds)
            or self.recovery_deadline_seconds <= 0
            or self.maximum_data_loss_records < 0
            or self.maximum_duplicate_effects < 0
        ):
            raise ValueError("fault scenario recovery bounds are invalid")


@dataclass(frozen=True)
class FaultCampaign:
    campaign_id: str
    revision: int
    environment: str
    production_shaped: bool
    scenarios: tuple[FaultScenario, ...]

    def __post_init__(self) -> None:
        if not self.campaign_id.strip() or not self.environment.strip() or self.revision < 1:
            raise ValueError("fault campaign identity, revision, and environment are required")
        identities = {scenario.scenario_id for scenario in self.scenarios}
        kinds = {scenario.fault_kind for scenario in self.scenarios}
        if len(identities) != len(self.scenarios):
            raise ValueError("fault campaign scenario identities must be unique")
        missing = set(FaultKind) - kinds
        if missing:
            raise ValueError(
                "fault campaign omits required kinds: "
                + ", ".join(sorted(item.value for item in missing))
            )


@dataclass(frozen=True)
class FaultObservation:
    observation_id: str
    campaign_id: str
    campaign_revision: int
    scenario_id: str
    fault_kind: FaultKind
    injection_succeeded: bool
    containment_succeeded: bool
    recovery_succeeded: bool
    recovery_seconds: float
    data_loss_records: int
    duplicate_effects: int
    cross_tenant_exposure: bool
    audit_complete: bool
    fallback_engaged: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(not item.strip() for item in (
            self.observation_id, self.campaign_id, self.scenario_id,
        )):
            raise ValueError("fault observation identity is required")
        if (
            self.campaign_revision < 1
            or not isfinite(self.recovery_seconds)
            or self.recovery_seconds < 0
            or self.data_loss_records < 0
            or self.duplicate_effects < 0
        ):
            raise ValueError("fault observation counters are invalid")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("fault observation requires durable evidence")


@dataclass(frozen=True)
class ScenarioVerdict:
    scenario_id: str
    fault_kind: FaultKind
    passed: bool
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "fault_kind": self.fault_kind.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class FaultCampaignReport:
    campaign_id: str
    campaign_revision: int
    campaign_sha256: str
    observation_set_sha256: str
    environment: str
    production_shaped: bool
    passed: bool
    production_release_evidence: bool
    scenarios: tuple[ScenarioVerdict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "campaign_revision": self.campaign_revision,
            "campaign_sha256": self.campaign_sha256,
            "observation_set_sha256": self.observation_set_sha256,
            "environment": self.environment,
            "production_shaped": self.production_shaped,
            "passed": self.passed,
            "production_release_evidence": self.production_release_evidence,
            "scenarios": [item.to_dict() for item in self.scenarios],
        }


def _canonical(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _campaign_dict(value: FaultCampaign) -> dict[str, Any]:
    return {
        "campaign_id": value.campaign_id,
        "revision": value.revision,
        "environment": value.environment,
        "production_shaped": value.production_shaped,
        "scenarios": [{
            "scenario_id": item.scenario_id,
            "fault_kind": item.fault_kind.value,
            "recovery_deadline_seconds": item.recovery_deadline_seconds,
            "maximum_data_loss_records": item.maximum_data_loss_records,
            "maximum_duplicate_effects": item.maximum_duplicate_effects,
        } for item in value.scenarios],
    }


def _observation_dict(value: FaultObservation) -> dict[str, Any]:
    return {
        "observation_id": value.observation_id,
        "campaign_id": value.campaign_id,
        "campaign_revision": value.campaign_revision,
        "scenario_id": value.scenario_id,
        "fault_kind": value.fault_kind.value,
        "injection_succeeded": value.injection_succeeded,
        "containment_succeeded": value.containment_succeeded,
        "recovery_succeeded": value.recovery_succeeded,
        "recovery_seconds": value.recovery_seconds,
        "data_loss_records": value.data_loss_records,
        "duplicate_effects": value.duplicate_effects,
        "cross_tenant_exposure": value.cross_tenant_exposure,
        "audit_complete": value.audit_complete,
        "fallback_engaged": value.fallback_engaged,
        "evidence_ids": list(value.evidence_ids),
    }


def parse_fault_campaign(raw: Mapping[str, Any]) -> FaultCampaign:
    return FaultCampaign(
        campaign_id=str(raw.get("campaign_id") or ""),
        revision=int(raw.get("revision") or 0),
        environment=str(raw.get("environment") or ""),
        production_shaped=(
            raw["production_shaped"]
            if isinstance(raw.get("production_shaped"), bool) else False
        ),
        scenarios=tuple(FaultScenario(
            scenario_id=str(item.get("scenario_id") or ""),
            fault_kind=FaultKind(str(item.get("fault_kind") or "")),
            recovery_deadline_seconds=float(item.get("recovery_deadline_seconds", 0)),
            maximum_data_loss_records=int(item.get("maximum_data_loss_records", 0)),
            maximum_duplicate_effects=int(item.get("maximum_duplicate_effects", 0)),
        ) for item in raw.get("scenarios", ())),
    )


def parse_fault_observation(raw: Mapping[str, Any]) -> FaultObservation:
    booleans = (
        "injection_succeeded", "containment_succeeded", "recovery_succeeded",
        "cross_tenant_exposure", "audit_complete", "fallback_engaged",
    )
    if any(not isinstance(raw.get(name), bool) for name in booleans):
        raise ValueError("fault observation outcome flags must be booleans")
    return FaultObservation(
        observation_id=str(raw.get("observation_id") or ""),
        campaign_id=str(raw.get("campaign_id") or ""),
        campaign_revision=int(raw.get("campaign_revision") or 0),
        scenario_id=str(raw.get("scenario_id") or ""),
        fault_kind=FaultKind(str(raw.get("fault_kind") or "")),
        injection_succeeded=raw["injection_succeeded"],
        containment_succeeded=raw["containment_succeeded"],
        recovery_succeeded=raw["recovery_succeeded"],
        recovery_seconds=float(raw.get("recovery_seconds", -1)),
        data_loss_records=int(raw.get("data_loss_records", -1)),
        duplicate_effects=int(raw.get("duplicate_effects", -1)),
        cross_tenant_exposure=raw["cross_tenant_exposure"],
        audit_complete=raw["audit_complete"],
        fallback_engaged=raw["fallback_engaged"],
        evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", ())),
    )


def evaluate_fault_campaign(
    campaign: FaultCampaign,
    observations: Iterable[FaultObservation],
) -> FaultCampaignReport:
    rows = tuple(observations)
    if len({row.observation_id for row in rows}) != len(rows):
        raise ValueError("fault observations contain duplicate identities")
    by_scenario = {row.scenario_id: row for row in rows}
    if len(by_scenario) != len(rows):
        raise ValueError("fault campaign requires exactly one observation per scenario")
    expected = {scenario.scenario_id for scenario in campaign.scenarios}
    if set(by_scenario) != expected:
        raise ValueError("fault campaign observation matrix is incomplete or unregistered")
    verdicts: list[ScenarioVerdict] = []
    for scenario in campaign.scenarios:
        row = by_scenario[scenario.scenario_id]
        if row.campaign_id != campaign.campaign_id or row.campaign_revision != campaign.revision:
            raise ValueError(f"fault observation {row.observation_id} belongs to another campaign")
        if row.fault_kind is not scenario.fault_kind:
            raise ValueError(f"fault observation {row.observation_id} changed the admitted fault kind")
        reasons: list[str] = []
        if not row.injection_succeeded:
            reasons.append("fault injection was not proven")
        if not row.containment_succeeded:
            reasons.append("fault escaped its admitted containment boundary")
        if not row.recovery_succeeded:
            reasons.append("recovery did not restore the accepted outcome")
        if row.recovery_seconds > scenario.recovery_deadline_seconds:
            reasons.append("recovery exceeded its admitted deadline")
        if row.data_loss_records > scenario.maximum_data_loss_records:
            reasons.append("data loss exceeded its admitted maximum")
        if row.duplicate_effects > scenario.maximum_duplicate_effects:
            reasons.append("duplicate effects exceeded their admitted maximum")
        if row.cross_tenant_exposure:
            reasons.append("cross-tenant exposure was observed")
        if not row.audit_complete:
            reasons.append("fault, diagnosis, and recovery are not fully auditable")
        if not row.fallback_engaged:
            reasons.append("the registered fallback did not engage")
        verdicts.append(ScenarioVerdict(
            scenario_id=scenario.scenario_id,
            fault_kind=scenario.fault_kind,
            passed=not reasons,
            reasons=tuple(reasons or ("fault was contained and recovered inside admitted bounds",)),
            evidence_ids=row.evidence_ids,
        ))
    passed = all(item.passed for item in verdicts)
    return FaultCampaignReport(
        campaign_id=campaign.campaign_id,
        campaign_revision=campaign.revision,
        campaign_sha256=_digest(_campaign_dict(campaign)),
        observation_set_sha256=_digest([
            _observation_dict(row) for row in sorted(rows, key=lambda item: item.observation_id)
        ]),
        environment=campaign.environment,
        production_shaped=campaign.production_shaped,
        passed=passed,
        production_release_evidence=passed and campaign.production_shaped,
        scenarios=tuple(verdicts),
    )
