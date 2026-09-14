import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import objectiveportfolio as op


def test_quantified_key_result_requires_directionally_valid_target():
    with pytest.raises(ValueError, match="exceed baseline"):
        op.normalize_key_result("Grow", "users", "accounts", "increase", 10, 5, owner="growth")
    with pytest.raises(ValueError, match="binary KR"):
        op.normalize_key_result("Launch", "launched", "bool", "binary", 0, 2, owner="pm")
    kr = op.normalize_key_result("Cut latency", "p95", "ms", "decrease", 500, 200,
                                 current_value=350, weight=2, owner="platform")
    assert kr["baseline"] == Decimal("500")
    assert op.key_result_progress(kr) == 0.5


def test_progress_is_bounded_but_source_measurement_need_not_be():
    assert op.key_result_progress({"direction": "increase", "baseline": 0,
                                   "target": 100, "current_value": 130}) == 1.0
    assert op.key_result_progress({"direction": "decrease", "baseline": 10,
                                   "target": 5, "current_value": 12}) == 0.0


def test_state_changes_require_evidence_and_follow_lifecycle():
    with pytest.raises(ValueError, match="evidence"):
        op.validate_transition("active", "blocked", "dependency failed", {})
    with pytest.raises(ValueError, match="invalid"):
        op.validate_transition("achieved", "active", "changed mind", {"review": "r-1"})
    result = op.validate_transition("at_risk", "active", "risk retired",
                                    {"measurement_id": 17})
    assert result["to_state"] == "active"


def test_tradeoff_makes_cost_and_revisit_condition_explicit():
    with pytest.raises(ValueError, match="forgone"):
        op.validate_tradeoff("speed", [], "ship now", "if incidents rise")
    item = op.validate_tradeoff("enterprise reliability", ["consumer growth", "new regions"],
                                "renewal risk dominates", "NRR exceeds 120%")
    assert item["forgone_options"] == ["consumer growth", "new regions"]


def test_dependency_transitions_are_explicit_and_evidence_led():
    with pytest.raises(ValueError, match="invalid dependency"):
        op.change_dependency("t", "d", "failed", {"incident": "i-1"},
                             changed_by="manager", expected_status="satisfied")
    with pytest.raises(ValueError, match="evidence"):
        op.change_dependency("t", "d", "satisfied", {}, changed_by="manager")


def test_hierarchical_rollup_weights_krs_and_child_objectives():
    objectives = [
        {"objective_id": "company", "title": "Durable growth", "weight": 1},
        {"objective_id": "sales", "parent_objective_id": "company", "weight": 3},
        {"objective_id": "quality", "parent_objective_id": "company", "weight": 1},
    ]
    krs = [
        {"key_result_id": "s1", "objective_id": "sales", "direction": "increase",
         "baseline": 0, "target": 100, "current_value": 50, "weight": 1},
        {"key_result_id": "q1", "objective_id": "quality", "direction": "decrease",
         "baseline": 10, "target": 0, "current_value": 0, "weight": 1},
    ]
    result = op.rollup(objectives, krs)
    assert result["progress"] == pytest.approx(0.625)
    assert result["roots"][0]["progress"] == pytest.approx(0.625)


def test_rollup_rejects_orphans_cycles_and_unknown_kr_targets():
    with pytest.raises(ValueError, match="orphan"):
        op.rollup([{"objective_id": "child", "parent_objective_id": "missing"}], [])
    with pytest.raises(ValueError, match="cycle"):
        op.rollup([{"objective_id": "a", "parent_objective_id": "b"},
                   {"objective_id": "b", "parent_objective_id": "a"}], [])
    with pytest.raises(ValueError, match="unknown objective"):
        op.rollup([], [{"objective_id": "missing"}])
