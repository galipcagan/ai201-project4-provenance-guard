"""Transparency labels — the user-facing text for each verdict.

Verbatim from planning.md §6. Written for a non-technical creator: no jargon,
and the AI label is phrased as an appealable assessment, never a verdict of
guilt. The label is chosen by `attribution`, which is itself derived from the
calibrated confidence + false-positive guards (scoring.py / §5.3), so the label
always tracks the confidence band.
"""

_LABELS = {
    "likely_ai": (
        "⚠️ Likely AI-generated. Several independent checks strongly suggest this "
        "text was produced by an AI writing tool. This is an automated assessment, "
        "not a final decision, and no action has been taken automatically — if you "
        "wrote this yourself, you can appeal and a person will review it."
    ),
    "likely_human": (
        "✅ Likely human-written. Our checks found no strong sign that an AI tool "
        "generated this text."
    ),
    "uncertain": (
        "○ Unclear. Our system couldn't confidently tell how this text was created, "
        "so we're not making a claim either way. This often happens with very short "
        "pieces or a distinctive personal writing style."
    ),
}


def make_label(attribution):
    """Return {verdict, text} for one of the three attributions (§6)."""
    verdict = attribution if attribution in _LABELS else "uncertain"
    return {"verdict": verdict, "text": _LABELS[verdict]}
