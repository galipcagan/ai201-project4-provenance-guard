# Provenance Guard

A multi-signal AI-content classification service for a writing platform. A creator
submits text; the system estimates whether it was AI-generated, expresses that estimate
as a **confidence score with honest uncertainty**, attaches a plain-language
**transparency label**, writes the decision to a structured **audit log**, enforces
**rate limits**, and gives creators an **appeals** path to contest any verdict.

Full design rationale — signals, blind spots, scoring math, the false-positive analysis,
and the API contract — lives in [planning.md](planning.md). Section references below (§)
point there.

---

## Architecture

The submission and appeal flows, with the component diagram, are in
[planning.md → ## Architecture](planning.md#architecture). In brief:

**Submission:** `POST /submit` → rate limiter → two detection signals (Groq LLM +
stylometry) → confidence fusion (§5) → transparency label (§6) → audit log → response.

**Appeal:** `POST /appeal` → log appeal beside the original decision → set status
`under_review` → response (a human reviews; no automated re-classification).

---

## Setup & running

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# create .env with your Groq key:
#   GROQ_API_KEY=gsk_...
.venv/Scripts/python.exe app.py                               # serves on :5000
```

Stack: Flask, Groq (`llama-3.3-70b-versatile`), Flask-Limiter, SQLite (stdlib).

---

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/submit` | POST | Classify text. Body: `{"text": ..., "creator_id": ...}` |
| `/appeal` | POST | Contest a verdict. Body: `{"content_id": ..., "creator_reasoning": ...}` |
| `/log` | GET | Read the audit log as JSON. Optional `?limit=N&creator_id=...` |
| `/health` | GET | Liveness. |

`/submit` returns `content_id`, `attribution`, `confidence`, `label` (verdict + text),
a per-signal `signals` breakdown, `status`, and `timestamp`. See §4 for the full contract.

---

## Detection signals (multi-signal pipeline)

Two **independent** signals that measure fundamentally *different* properties of the text:

1. **Groq LLM** (`detect_llm`) — judges the *semantic / distributional* fingerprint: how
   smooth, hedged, and generically "average" the text reads to a language model.
2. **Stylometric heuristics** (`detect_stylometry`, pure Python) — measures *surface
   structure*: sentence-length burstiness, mean sentence length, transition-word density,
   and type-token ratio, damped by casual-language markers.

**Why these two (the reasoning, not just the implementation).** The core requirement is
that the signals be *genuinely* independent, so their blind spots don't overlap — otherwise
two signals are just one signal counted twice. The LLM is strong on *meaning* but is
non-deterministic, opaque, and fooled by human-edited AI or by formulaic human prose.
Stylometry is deterministic, transparent, free, and catches *structural* regularity the
LLM can miss — but it's unreliable on short text, genre-naive, and gameable by an adversary
who varies sentence length. Critically, they fail in **different directions**: the LLM
over-flags formal human writing, while stylometry under-flags cleverly edited AI. Pairing a
meaning-based model with a structure-based heuristic means a case that fools one still has
to get past the other. Each signal also reports its own confidence as a `weight` (0–1), so
the fusion can down-weight a signal that knows it's on shaky ground (e.g. stylometry on a
40-word poem).

**What I'd change deploying this for real.** (1) The LLM's self-reported "confidence" is
just the model's say-so — I'd replace it with a real signal like token-level perplexity
from a smaller open model, which is measurable rather than asserted. (2) The stylometry
weights (0.35 burstiness, 0.30 transitions, …) are hand-tuned from intuition; I'd *learn*
them from a labeled corpus of human vs. AI text and calibrate the thresholds against a real
false-positive-rate target instead of picking 0.75/0.30 by judgment. (3) I'd add a third,
orthogonal signal (e.g. edit-history / provenance metadata) since two signals still leave
correlated failure modes.

## Confidence scoring (uncertainty, not a binary flip)

**Why this approach.** The goal is a score that *communicates uncertainty* rather than
forcing a coin-flip near the middle. Fusion (`scoring.py`, §5.2) is a weight-blended score
followed by a **direction-aware agreement adjustment**: when the two signals point the
*same* way they corroborate (trust the blend); when they point in *opposite* directions
the score is pulled toward the uncertain middle. That's the whole philosophy in one line —
**agreement amplifies, disagreement abstains** — and it's what lets a genuine conflict
(one signal says AI, the other says human) resolve to "we don't know" instead of a
confident guess. Thresholds are deliberately **asymmetric** because, on a writing platform,
a false positive (accusing a real human) is far more damaging than a false negative
(§3, §5.3):

```
confidence ≥ 0.75          → likely_ai       (high bar — cautious about accusing)
0.30 < confidence < 0.75   → uncertain
confidence ≤ 0.30          → likely_human
```

Two extra false-positive guards back the thresholds up: the verdict is **never `likely_ai`
unless both signals individually lean AI**, and in **degraded mode** (Groq down) stylometry
alone can never produce an AI verdict — it's capped at `uncertain`.

### Two example submissions (meaningful variation, not a constant)

Actual scores from Milestone-4 testing, showing the scoring produces a wide, meaningful
spread:

**High-confidence case — clearly AI-generated marketing prose:**
> "Artificial intelligence represents a transformative paradigm shift in modern society.
> It is important to note that … Furthermore, stakeholders across various sectors must
> collaborate to ensure responsible deployment."

| LLM score | Stylometry score | Agreement | **Confidence** | Verdict |
|:---------:|:----------------:|:---------:|:--------------:|:-------:|
| 0.90 | 0.51 | same side → 1.0 | **0.81** | `likely_ai` |

**Low-confidence (opposite end) case — clearly human, casual restaurant review:**
> "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the
> broth was fine but they put WAY too much sodium in it …"

| LLM score | Stylometry score | Agreement | **Confidence** | Verdict |
|:---------:|:----------------:|:---------:|:--------------:|:-------:|
| 0.20 | 0.01 | same side → 1.0 | **0.13** | `likely_human` |

A spread of **0.81 vs 0.13** on real inputs — the two signals corroborated in both cases,
so the score moved confidently to opposite ends. A third, borderline input (a formal human
academic paragraph) scored LLM 0.80 but stylometry 0.40 — *opposite* sides — and the
agreement adjustment pulled it to **0.62 → `uncertain`**, refusing to falsely accuse a
human. That's the design working: confident when signals agree, humble when they don't.

**What I'd change deploying this for real.** The 0.75/0.30 thresholds are engineering
judgment, not empirically calibrated. For production I'd tune them against a measured
false-positive rate on real creator content (targeting, say, <1% of human work ever
labeled `likely_ai`) and likely widen the `uncertain` band further, since being wrong in
public is far costlier than abstaining.

---

## Transparency label

The label returned by `/submit` changes with the confidence band. Three verbatim variants
(§6); only the AI label carries a warning + appeal pointer, and none uses accusatory
language:

- **Likely AI-generated** (`confidence ≥ 0.75`):
  > ⚠️ Likely AI-generated. Several independent checks strongly suggest this text was
  > produced by an AI writing tool. This is an automated assessment, not a final decision,
  > and no action has been taken automatically — if you wrote this yourself, you can appeal
  > and a person will review it.

- **Likely human-written** (`confidence ≤ 0.30`):
  > ✅ Likely human-written. Our checks found no strong sign that an AI tool generated this
  > text.

- **Unclear / uncertain** (`0.30 < confidence < 0.75`):
  > ○ Unclear. Our system couldn't confidently tell how this text was created, so we're not
  > making a claim either way. This often happens with very short pieces or a distinctive
  > personal writing style.

All three variants confirmed reachable by submitting texts that land in each band.

---

## Appeals workflow

`POST /appeal` with `content_id` + `creator_reasoning` (`creator_id` optional):

- validates the `content_id` exists (`404` if not),
- rejects a second appeal on the same content (`409`),
- writes an **appeal record** to the `appeals` table and updates the submission to
  `status = under_review` with the `creator_reasoning` attached,
- **leaves the original decision (attribution, confidence, signal scores) untouched**, and
- returns an `appeal_id` + confirmation. No automated re-classification — a human decides.

The appealed submission then shows `status: under_review` and the populated
`creator_reasoning` in `GET /log`.

---

## Rate limiting

Applied to `/submit` and `/appeal` via Flask-Limiter (in-memory store), keyed by
`creator_id` when present, otherwise client IP.

**Chosen limits: `10 per minute; 100 per day`.**

**Reasoning (not arbitrary):** a genuine creator submits their own writing occasionally —
a handful of pieces in a session, rarely more than a few per minute even when iterating.
`10/min` sits comfortably above real human use while stopping a script from flooding the
endpoint to (a) run up Groq costs or (b) probe the detector's behavior by brute force. The
`100/day` ceiling bounds sustained abuse that stays under the per-minute bar. Both are
per-creator, so one abuser can't rate-limit everyone else.

**Evidence — 12 rapid requests against the 10/min limit** (first 10 accepted, rest
rejected):

```
200
200
200
200
200
200
200
200
200
200
429
429
```

---

## Audit log

Every decision is written to SQLite (structured, not `print()`), readable as JSON via
`GET /log`. Each entry records: `timestamp`, `content_id`, `creator_id`, `attribution`,
`confidence`, both individual signal scores (`llm_score`, `stylometry_score`), `label`,
`status`, `degraded`, and — after an appeal — `appeal_id` + `creator_reasoning`.

**Sample `GET /log` output** (a submission per band; the third has been appealed, so its
`status` is `under_review` with `creator_reasoning` populated while its original verdict is
preserved):

```json
{
  "count": 3,
  "entries": [
    {
      "content_id": "c_2df5bdd612", "creator_id": "lab_unc",
      "attribution": "uncertain", "confidence": 0.6096,
      "llm_score": 0.8, "stylometry_score": 0.4255,
      "label": "uncertain", "status": "classified", "degraded": false,
      "appeal_id": null, "creator_reasoning": null,
      "text": "The relationship between monetary policy and asset price inf...",
      "timestamp": "2026-07-01T03:31:27+00:00"
    },
    {
      "content_id": "c_a2fa9e79e6", "creator_id": "lab_human",
      "attribution": "likely_human", "confidence": 0.1538,
      "llm_score": 0.2, "stylometry_score": 0.0,
      "label": "likely_human", "status": "classified", "degraded": false,
      "appeal_id": null, "creator_reasoning": null,
      "text": "ok so i finally tried that new ramen place downtown and hone...",
      "timestamp": "2026-07-01T03:31:27+00:00"
    },
    {
      "content_id": "c_03dd5e5106", "creator_id": "lab_ai",
      "attribution": "likely_ai", "confidence": 0.8622,
      "llm_score": 0.9, "stylometry_score": 0.7362,
      "label": "likely_ai", "status": "under_review", "degraded": false,
      "appeal_id": "apl_0651d24379",
      "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
      "text": "In todays fast-paced digital world, leveraging cutting-edge ...",
      "timestamp": "2026-07-01T03:31:26+00:00"
    }
  ]
}
```

> Note: `/log` has no auth — it exists for documentation and grading visibility. A real
> deployment would require authentication.

---

## Known limitations

**The system will most reliably get *formal writing by non-native English speakers*
wrong** — leaning `likely_ai`, or at best `uncertain`, on genuine human work.

This is not a "needs more data" hand-wave; it's baked into what both signals measure. The
LLM signal keys on *smoothness and low idiosyncrasy* — but careful non-native writers often
adopt a deliberately formal, textbook register that reads as exactly that "safe, average"
prose the LLM associates with AI. The stylometry signal keys on *uniform sentence length
and low burstiness* — and formal ESL writing tends toward consistent, measured sentence
structure rather than the choppy, varied rhythm of casual native prose. So both signals are
pushed the *same* direction (toward AI) by the *same* underlying property — formality and
uniformity — even though that property has an innocent cause. Because they agree, the
direction-aware fusion does **not** rescue the case the way it rescued the borderline
academic paragraph; it can produce a confident wrong answer. (This is exactly the scenario
in the sample appeal, where the creator writes "I am a non-native English speaker and my
writing style may appear more formal than typical.")

Two things soften it rather than solve it: the asymmetric threshold makes an outright
`likely_ai` verdict require *strong* agreement, so many of these land in `uncertain`; and
the appeals path exists precisely so a human can correct what the detector can't. But it
remains a real, signal-level limitation. Other weak spots: very short texts (poetry,
haiku — too little data for stylometry), and adversarially "humanized" AI (defeats
stylometry; only the LLM has a chance).

## Spec reflection

**Where the spec guided the implementation.** Writing `planning.md` §5 *before* any code —
defining the per-signal `{score, weight}` shape, the fusion formula, and the asymmetric
thresholds — meant the confidence scoring was a lookup, not an improvisation. When it came
time to build `scoring.py`, the contract already existed: each signal knew what to return,
and the fusion knew how to combine it. The false-positive analysis in §3 (the "Maya"
scenario) directly produced the both-lean guard and the wide uncertain band. Deciding these
on paper first is the main reason the pieces fit together instead of being retrofitted.

**Where the implementation diverged, and why.** My committed §5.2 formula defined
`agreement = 1 − |s1 − s2|` — a *magnitude* difference. When I implemented and tested it in
Milestone 4, clearly-AI text (LLM 0.90, stylometry 0.51) came back `uncertain`, because the
formula treated those two scores as "disagreeing" even though *both leaned AI*. That was
wrong: they agree on direction, they just differ in strength. I diverged from the spec by
making the agreement adjustment **direction-aware** — it only shrinks the score when the
signals fall on *opposite* sides of 0.5 — and then updated `planning.md` §5.2 so the spec
and code stayed in sync. The divergence came from testing revealing a flaw the paper design
couldn't; keeping the document truthful about it was the point.

## AI usage

This project was built with AI assistance (Claude). Specific instances where I directed the
AI, reviewed its output, and revised or overrode it:

1. **Confidence-scoring fusion — overrode a spec-faithful but flawed result.** I directed
   the AI to implement `scoring.py` to match `planning.md` §5.2 exactly. It produced a
   correct literal translation of the formula (`agreement = 1 − |s1 − s2|`). Testing on the
   four deliberate inputs showed it *over-abstained* — clearly-AI text scored `uncertain`.
   I diagnosed the cause (magnitude vs. direction), **overrode the formula** to be
   direction-aware, and had the AI update both the code and the spec to match. This is the
   divergence documented above.

2. **Transparency-label wording — revised for a non-technical reader.** I directed the AI
   to draft the three label variants from §6. Its first draft used the word "signals"
   (internal jargon) and, for the AI label, could read as an accusation/penalty. I ran a
   "fresh-eyes" review and **revised**: "signals" → "checks", and added the reassurance
   "no action has been taken automatically" so a creator doesn't panic that their post was
   removed. The revision table is recorded in `planning.md` §6.

3. **Audit-log storage — chose SQLite over the suggested plain JSON.** The milestone
   suggested JSON *or* SQLite for the log. The AI's initial instinct was a JSON file; I
   **overrode** that in favor of SQLite because appeals need to *update* a submission's
   status (`classified` → `under_review`) in place, which is clean in SQL and awkward with
   an append-only JSON file. The schema was designed up front with `status` and
   `stylometry_score` columns so later milestones needed no migration.

## Project layout

| File | Role |
|------|------|
| [app.py](app.py) | Flask app: routes, validation, rate limiter, wiring |
| [detection.py](detection.py) | Signal 1 (Groq LLM) + Signal 2 (stylometry) |
| [scoring.py](scoring.py) | Confidence fusion + thresholds + guards (§5) |
| [labels.py](labels.py) | Verbatim transparency labels (§6) |
| [db.py](db.py) | SQLite audit log + appeals |
| [planning.md](planning.md) | Full spec, architecture diagram, AI tool plan |
