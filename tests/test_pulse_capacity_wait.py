import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import management
import pulse


def test_queued_pulse_is_not_running_silence(monkeypatch):
    monkeypatch.setattr(pulse, "_rows", lambda: [
        {"work_id": "queued", "status": "active", "stage": "queued",
         "beat_age_s": 999, "expected_cadence_s": 10},
        {"work_id": "running", "status": "active", "stage": "browser",
         "beat_age_s": 999, "expected_cadence_s": 10},
    ])
    assert [item["work_id"] for item in pulse.stalled()] == ["running"]


def test_management_defensively_ignores_queued_silence(monkeypatch):
    class FakePulse:
        @staticmethod
        def stalled():
            return [
                {"work_id": "queued", "stage": "queued", "progress": "awaiting slot",
                 "beat_age_s": 999, "expected_cadence_s": 10, "tenant_id": "t"},
                {"work_id": "running", "stage": "browser", "progress": "step 3",
                 "beat_age_s": 999, "expected_cadence_s": 10, "tenant_id": "t"},
            ]

    monkeypatch.setitem(sys.modules, "pulse", FakePulse)
    captured = []
    monkeypatch.setattr(
        management, "_duty_candidates",
        lambda _budget, _source, candidates: captured.extend(candidates) or len(candidates),
    )
    assert management.discover_stalled_pulses() == 1
    assert [item["work_id"] for item in captured] == ["running"]
