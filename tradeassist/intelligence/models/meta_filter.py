"""Take or pass: should this setup be traded? (4.0, phase 3)

A gate, not a second opinion: take the trade only when the target/stop model's expected net return after costs
clears `min_edge`, and only once a trade model has been trained on matured outcomes (before that there is no
evidence, so every setup is passed). Every setup gets a decision, taken or not, and every one is graded, so the
scorecard can say what taking all of them, or none, would have earned beside what the gate chose.
"""

from __future__ import annotations

import math
from typing import Any


def decide(expected: float | None, trained: bool, min_edge: float) -> tuple[bool, str, dict[str, Any]]:
    gate = {"min_edge": min_edge, "expected": expected, "trained": trained}
    if not trained:
        return False, "no trade model trained yet: there is no evidence to trade on", gate
    if expected is None or not math.isfinite(expected):
        return False, "no expected return could be computed", gate
    if expected < min_edge:
        return False, f"expected net return {100 * expected:+.2f}% is below the {100 * min_edge:.2f}% edge required", gate
    return True, f"expected net return {100 * expected:+.2f}% clears the {100 * min_edge:.2f}% edge required", gate


__all__ = ["decide"]
