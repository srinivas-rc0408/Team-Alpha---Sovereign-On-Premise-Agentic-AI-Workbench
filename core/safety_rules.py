"""Deterministic safety judgments — no LLM in the decision path.

The agent's LLM extracts numbers and identifies which check applies (grounded
in the retrieved SOP text); everything below is plain Python comparisons. A
hallucinated number would produce a wrong CHECK, but never a wrong JUDGMENT of
whatever numbers it's given — and it's why these functions take limits as
arguments rather than hardcoding industry thresholds (e.g. ISO 10816-3 zone
boundaries) from memory: an unverified constant baked into "deterministic"
code is just a hallucination with better production values.
"""
import json
import os
from dataclasses import dataclass, asdict


@dataclass
class Verdict:
    status: str        # NORMAL | CAUTION | CRITICAL | OK | EXCEEDS | BELOW_MINIMUM
    severity: str       # none | low | medium | high
    overage: float      # amount past the safe limit (0 or negative if within)
    action: str

    def dict(self):
        return asdict(self)


def load_reference_limits(path: str = None) -> dict:
    """Optional operator-supplied limits (see config/safety_limits.example.json)
    so a plant doesn't have to restate its own verified numbers in every query.
    Never a source of invented numbers — if the file is absent, or a check's
    limit is null, the caller gets nothing and falls back to whatever the query
    or retrieved SOP text actually states."""
    path = path or os.getenv("SAFETY_LIMITS_FILE", "config/safety_limits.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


class SafetyChecker:
    """Pure functions: same inputs always produce the same verdict."""

    @staticmethod
    def check_pressure(reading_bar: float, safe_limit_bar: float, critical_limit_bar: float = None) -> dict:
        overage = round(reading_bar - safe_limit_bar, 3)
        if critical_limit_bar is not None and reading_bar >= critical_limit_bar:
            return Verdict("CRITICAL", "high", overage,
                            "Stop work, isolate, evacuate, notify supervisor immediately.").dict()
        if reading_bar > safe_limit_bar:
            return Verdict("CAUTION", "medium", overage,
                            "Increase monitoring frequency; notify area supervisor; suspend hot work.").dict()
        return Verdict("NORMAL", "none", overage, "Proceed with planned work.").dict()

    @staticmethod
    def check_temperature(reading_c: float, safe_limit_c: float, critical_limit_c: float = None) -> dict:
        overage = round(reading_c - safe_limit_c, 3)
        if critical_limit_c is not None and reading_c >= critical_limit_c:
            return Verdict("CRITICAL", "high", overage,
                            "Reduce firing/heat input immediately; investigate root cause.").dict()
        if reading_c > safe_limit_c:
            return Verdict("CAUTION", "medium", overage, "Increase monitoring; verify instrumentation.").dict()
        return Verdict("NORMAL", "none", overage, "Within normal operating range.").dict()

    @staticmethod
    def check_vibration(reading_mms: float, limit_mms: float) -> dict:
        overage = round(reading_mms - limit_mms, 3)
        if reading_mms > limit_mms:
            return Verdict("EXCEEDS", "high", overage,
                            "Reading exceeds the supplied limit — schedule inspection/shutdown per SOP.").dict()
        return Verdict("OK", "none", overage, "Within the supplied limit.").dict()

    @staticmethod
    def check_wall_thickness(measured_mm: float, minimum_mm: float) -> dict:
        margin = round(measured_mm - minimum_mm, 3)
        if measured_mm < minimum_mm:
            return Verdict("BELOW_MINIMUM", "high", -margin,
                            "Below minimum allowable thickness — remove from service pending engineering review.").dict()
        margin_pct = round(margin / minimum_mm * 100, 1) if minimum_mm else 0.0
        status = "OK" if margin_pct > 10 else "CAUTION"
        severity = "none" if status == "OK" else "medium"
        return {"status": status, "severity": severity, "overage": margin,
                "remaining_margin_pct": margin_pct,
                "action": "Continue monitoring per inspection interval." if status == "OK"
                          else "Margin is thin — shorten the next inspection interval."}


if __name__ == "__main__":  # ponytail: self-check against SOP-MNT-402's real numbers
    c = SafetyChecker()
    assert c.check_pressure(18.4, 15, 18)["status"] == "CRITICAL"
    assert c.check_pressure(16.0, 15, 18)["status"] == "CAUTION"
    assert c.check_pressure(10.0, 15, 18)["status"] == "NORMAL"
    assert c.check_temperature(372, 360, 370)["status"] == "CRITICAL"
    assert c.check_vibration(12.0, 11.2)["status"] == "EXCEEDS"
    assert c.check_wall_thickness(5.0, 6.35)["status"] == "BELOW_MINIMUM"
    assert load_reference_limits("nonexistent.json") == {}
    print("safety_rules ok — deterministic verdicts match SOP-MNT-402's tiers")
