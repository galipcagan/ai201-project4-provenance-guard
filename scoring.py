"""Confidence scoring — fuse the two detection signals into one calibrated score.

Implements planning.md §5.2 (weighted blend + agreement adjustment) and §5.3
(asymmetric thresholds + false-positive guards). The whole point: agreement
amplifies, disagreement abstains.
"""

# §5.3 thresholds. Asymmetric on purpose — a false positive (accusing a human)
# is the costly error, so the bar to call something AI is high.
AI_THRESHOLD = 0.75
HUMAN_THRESHOLD = 0.30


def _clamp01(x):
    return max(0.0, min(1.0, x))


def fuse(s1, w1, s2, w2):
    """§5.2 — blend two scores by weight, then shrink toward 0.5 on *directional*
    disagreement.

    The shrink applies only when the signals fall on opposite sides of 0.5 (one
    says AI, the other human) — that is genuine conflict and we should abstain.
    When both signals lean the same way they corroborate, even if their
    magnitudes differ, so we trust the weighted blend.

    Returns (confidence, raw, agreement). All scores are 0=human .. 1=AI.
    """
    total_w = w1 + w2
    raw = (w1 * s1 + w2 * s2) / total_w if total_w else 0.5

    same_side = (s1 - 0.5) * (s2 - 0.5) >= 0
    agreement = 1.0 if same_side else (1.0 - abs(s1 - s2))

    confidence = 0.5 + (raw - 0.5) * agreement
    return _clamp01(confidence), raw, agreement


def classify(confidence, s1, s2, degraded=False):
    """§5.3 — map a fused confidence to one of three attributions, with guards.

    - both-lean rule: never `likely_ai` unless BOTH signals individually lean AI.
    - degraded mode: with only one signal, never `likely_ai` (cap at uncertain).
    """
    both_lean_ai = (s1 > 0.5) and (s2 > 0.5)

    if confidence >= AI_THRESHOLD and both_lean_ai and not degraded:
        return "likely_ai"
    if confidence <= HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"


def score_confidence(llm, stylometry, degraded=False):
    """Top-level entry point: combine both signal dicts into a final verdict.

    `llm` and `stylometry` are the dicts returned by detection.detect_*.
    In degraded mode (Signal 1 down) the missing signal carries weight 0, so
    `fuse` naturally falls back to the surviving signal — and `classify`
    refuses to emit `likely_ai` on one signal alone.
    """
    s1, w1 = llm["score"], llm["weight"]
    s2, w2 = stylometry["score"], stylometry["weight"]

    confidence, raw, agreement = fuse(s1, w1, s2, w2)
    attribution = classify(confidence, s1, s2, degraded=degraded)

    return {
        "confidence": round(confidence, 4),
        "attribution": attribution,
        "raw_blend": round(raw, 4),
        "agreement": round(agreement, 4),
    }
