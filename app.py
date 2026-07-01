"""Provenance Guard — Flask application.

Milestone 3: submission endpoint + first detection signal (Groq LLM) + audit log.
See planning.md for the full spec; section references (§) below point to it.
"""
import uuid

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from detection import detect_llm, detect_stylometry
from scoring import score_confidence
from labels import make_label
import db

app = Flask(__name__)
db.init_db()


# --------------------------------------------------------------------------- #
# Rate limiting (§Rate limiting / README) — protect Groq calls and stop floods.
# Keyed by creator_id when present, else client IP. See README for the reasoning
# behind the specific numbers.
# --------------------------------------------------------------------------- #
def _rate_key():
    data = request.get_json(silent=True) or {}
    return data.get("creator_id") or get_remote_address()


limiter = Limiter(
    key_func=_rate_key,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

SUBMIT_LIMITS = "10 per minute; 100 per day"


@app.errorhandler(429)
def ratelimit_handler(exc):
    """Return the rate-limit rejection as JSON (§4)."""
    return (
        jsonify(
            {
                "error": "Rate limit exceeded. Please slow down and try again later.",
                "limit": str(exc.description),
            }
        ),
        429,
    )


def _new_content_id():
    return "c_" + uuid.uuid4().hex[:10]


# --------------------------------------------------------------------------- #
# Input validation (§4 — POST /submit contract)
# --------------------------------------------------------------------------- #
MIN_WORDS = 5  # below this, signals are meaningless (§8 #4 — junk/too-short input)


def _validate_submission(payload):
    """Return (text, creator_id) or raise ValueError with a user-facing message."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")

    text = payload.get("text")
    creator_id = payload.get("creator_id")

    if not text or not str(text).strip():
        raise ValueError("Field 'text' is required and must be non-empty.")
    if not creator_id or not str(creator_id).strip():
        raise ValueError("Field 'creator_id' is required and must be non-empty.")
    if len(str(text).split()) < MIN_WORDS:
        raise ValueError(
            f"Text is too short to analyze (need at least {MIN_WORDS} words)."
        )

    return str(text), str(creator_id)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/health", methods=["GET"])
def health():
    """Liveness check (§4)."""
    return jsonify({"status": "ok"})


@app.route("/submit", methods=["POST"])
@limiter.limit(SUBMIT_LIMITS)
def submit():
    """Classify a piece of content (Milestone 4: both signals + real scoring).

    Generates a unique content_id, runs Signal 1 (Groq LLM) and Signal 2
    (stylometry), fuses them into a calibrated confidence (§5), and returns the
    verdict + per-signal breakdown. If Signal 1 is unavailable, runs in degraded
    mode (§5.3) on stylometry alone, capped at `uncertain`, with `degraded: true`.
    """
    try:
        text, creator_id = _validate_submission(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    content_id = _new_content_id()

    # Signal 2 is pure-Python and always available.
    stylometry = detect_stylometry(text)

    # Signal 1 may fail (network/parse) -> degraded mode (weight 0 drops it out).
    try:
        llm = detect_llm(text)
        degraded = False
    except Exception as exc:
        app.logger.warning("Signal 1 (Groq) failed: %s", exc)
        llm = {"score": 0.5, "weight": 0.0, "confidence": "low",
               "rationale": "Signal 1 unavailable; ran in degraded mode."}
        degraded = True

    scored = score_confidence(llm, stylometry, degraded=degraded)
    attribution = scored["attribution"]
    confidence = scored["confidence"]
    label = make_label(attribution)

    # Write a structured entry to the audit log — both signal scores + combined.
    timestamp = db.log_submission(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "text": text,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm["score"],
            "stylometry_score": stylometry["score"],
            "label": label["verdict"],
            "status": "classified",
            "degraded": degraded,
        }
    )

    response = {
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "signals": {"llm": llm, "stylometry": stylometry},
        "scoring": {"raw_blend": scored["raw_blend"], "agreement": scored["agreement"]},
        "status": "classified",
        "degraded": degraded,
        "timestamp": timestamp,
    }
    return jsonify(response)


@app.route("/appeal", methods=["POST"])
@limiter.limit(SUBMIT_LIMITS)
def appeal():
    """Contest a classification (§7).

    Accepts content_id + creator_reasoning (creator_id optional). Logs the
    appeal beside the original decision, flips status to 'under_review', and
    returns confirmation. No automated re-classification — a human decides.
    """
    payload = request.get_json(silent=True) or {}
    content_id = payload.get("content_id")
    creator_reasoning = payload.get("creator_reasoning")
    creator_id = payload.get("creator_id")

    if not content_id or not str(content_id).strip():
        return jsonify({"error": "Field 'content_id' is required."}), 400
    if not creator_reasoning or not str(creator_reasoning).strip():
        return jsonify({"error": "Field 'creator_reasoning' is required."}), 400

    submission = db.get_submission(str(content_id))
    if submission is None:
        return jsonify({"error": f"No submission found for content_id '{content_id}'."}), 404
    if submission["status"] == "under_review":
        return jsonify({"error": "This content is already under review."}), 409

    appeal_id = "apl_" + uuid.uuid4().hex[:10]
    timestamp = db.record_appeal(
        appeal_id=appeal_id,
        content_id=str(content_id),
        creator_reasoning=str(creator_reasoning),
        creator_id=str(creator_id) if creator_id else None,
        original_attribution=submission["attribution"],
        original_confidence=submission["confidence"],
    )

    return jsonify(
        {
            "content_id": content_id,
            "appeal_id": appeal_id,
            "status": "under_review",
            "message": "Your appeal has been logged. This content is now under human review.",
            "original_decision": {
                "attribution": submission["attribution"],
                "confidence": submission["confidence"],
            },
            "timestamp": timestamp,
        }
    )


@app.route("/log", methods=["GET"])
def get_log():
    """Return the most recent audit-log entries as JSON (§4 GET /log).

    Optional query params: ?limit=N (default 20), ?creator_id=...
    No auth — for documentation/grading visibility; a real system would require it.
    """
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    creator_id = request.args.get("creator_id")

    entries = db.recent_entries(limit=limit, creator_id=creator_id)
    return jsonify({"count": len(entries), "entries": entries})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
