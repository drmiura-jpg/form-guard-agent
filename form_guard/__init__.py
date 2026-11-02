"""Form-Guard Agent — fill only the designated fields of a form, prove nothing else changed.

Pipeline: Scan -> Map & Self-Correct (Claude or offline demo engine) -> Surgical XML
Write -> Structural Verify -> Visual Diff & Human-in-the-Loop report.
"""

__version__ = "0.2.0"

# Current-generation Claude models used by the multi-model router (form_guard/mapper.py).
MODEL_OPUS = "claude-opus-5-5"      # long-text condensation, self-correction after validator rejection
MODEL_SONNET = "claude-sonnet-5-5"  # standard text / yen / person-name fields
MODEL_HAIKU = "claude-haiku-5-5"    # structured fields (dates, IDs, choices) and blank-cell label classification

DEFAULT_MODEL = MODEL_OPUS
