"""Form-Guard Agent — fill only the designated fields of a form, prove nothing else changed.

Pipeline: Scan -> Map & Self-Correct (Claude or offline demo engine) -> Surgical XML
Write -> Structural Verify -> Visual Diff & Human-in-the-Loop report.
"""

__version__ = "0.1.0"

DEFAULT_MODEL = "claude-opus-5-5"
