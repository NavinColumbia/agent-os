"""Single source of truth for one bounded QA worker slice's public ETA."""
import os


DEFAULT_QA_DRIVE_BUDGET_S = 1500


def drive_budget_s(environ=None):
    env = os.environ if environ is None else environ
    try:
        return max(60, int(env.get("AOS_QA_DRIVE_BUDGET_S", DEFAULT_QA_DRIVE_BUDGET_S)))
    except (TypeError, ValueError):
        return DEFAULT_QA_DRIVE_BUDGET_S


def slice_eta_min(environ=None):
    """Ceiling minutes: never promise less time than the configured safety slice."""
    seconds = drive_budget_s(environ)
    return max(1, (seconds + 59) // 60)


__all__ = ["DEFAULT_QA_DRIVE_BUDGET_S", "drive_budget_s", "slice_eta_min"]
