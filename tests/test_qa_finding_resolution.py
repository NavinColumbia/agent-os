import sys
from pathlib import Path


QA = Path(__file__).resolve().parents[1] / "scripts" / "qa"
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

from qa_explorer import Explorer


def test_control_reappearing_in_different_journey_does_not_resolve_reload_bug():
    explorer = Explorer("http://app", vision="persist confirmation", autostart=False)
    reload_contract = "Reload preserves the public confirmation and reference"
    explorer.bugs = [{
        "bug": "reload loses the confirmation",
        "blocking": False,
        "covers": [reload_contract],
        "expected_control": "Send another enquiry",
    }]

    explorer._mark_resolved_bugs(
        {"elements": [{'role': 'button', 'label': 'Send another enquiry'}]},
        step=12,
        grounded=["A second valid enquiry is accepted"],
        successful=True,
    )
    assert not explorer.bugs[0].get("resolved")

    explorer._mark_resolved_bugs(
        {"elements": [{'role': 'button', 'label': 'Send another enquiry'}]},
        step=13,
        grounded=[reload_contract],
        successful=True,
    )
    assert explorer.bugs[0]["resolved"] is True
    assert explorer.bugs[0]["resolved_step"] == 13
