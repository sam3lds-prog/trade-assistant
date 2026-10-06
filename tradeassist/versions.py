"""Version labels stored with every analysis and snapshot.

Scores and states from a later version of the logic are not comparable with older ones, so
each stored result records which logic produced it. Raise the matching number whenever a
change would alter the output for the same data.
"""

SIGNAL_ENGINE_VERSION = "signals-2.0"        # patterns, structure, provisional/confirmed status
TREND_ENGINE_VERSION = "trend_engine_v2.0"
REGIME_ENGINE_VERSION = "regime_v1.0"
EVALUATOR_VERSION = "evaluator_v1.0"
OPTIONS_EVALUATOR_VERSION = "options_evaluator_v1.0"
