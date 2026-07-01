"""Audit log — structured, persistent record of every attribution decision.

Backed by SQLite (stdlib, no external service). Every /submit writes one row;
appeals in Milestone 5 will update `status` and add appeal records. This is the
canonical record graders rely on (planning.md §1 step 6, §4 GET /log).
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                content_id        TEXT PRIMARY KEY,
                creator_id        TEXT NOT NULL,
                text              TEXT NOT NULL,
                attribution       TEXT NOT NULL,
                confidence        REAL NOT NULL,
                llm_score         REAL,
                stylometry_score  REAL,
                label             TEXT,
                status            TEXT NOT NULL DEFAULT 'classified',
                degraded          INTEGER NOT NULL DEFAULT 0,
                timestamp         TEXT NOT NULL,
                creator_reasoning TEXT,
                appeal_id         TEXT,
                appealed_at       TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id            TEXT PRIMARY KEY,
                content_id           TEXT NOT NULL,
                creator_id           TEXT,
                creator_reasoning    TEXT NOT NULL,
                original_attribution TEXT,
                original_confidence  REAL,
                timestamp            TEXT NOT NULL
            )
            """
        )
        # Migration: add appeal columns to pre-existing submissions tables.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(submissions)")}
        for col in ("creator_reasoning", "appeal_id", "appealed_at"):
            if col not in existing:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} TEXT")


def utc_now_iso():
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_submission(record):
    """Insert one decision into the audit log.

    `record` is a dict with keys: content_id, creator_id, text, attribution,
    confidence, llm_score, stylometry_score, label, status, degraded.
    A timestamp is added here so all entries are stamped consistently.
    """
    ts = utc_now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO submissions (
                content_id, creator_id, text, attribution, confidence,
                llm_score, stylometry_score, label, status, degraded, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["content_id"],
                record["creator_id"],
                record["text"],
                record["attribution"],
                record["confidence"],
                record.get("llm_score"),
                record.get("stylometry_score"),
                record.get("label"),
                record.get("status", "classified"),
                1 if record.get("degraded") else 0,
                ts,
            ),
        )
    return ts


def get_submission(content_id):
    """Return the submission row as a dict, or None if it doesn't exist."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def record_appeal(appeal_id, content_id, creator_reasoning, creator_id,
                  original_attribution, original_confidence):
    """Log an appeal and flip the submission to 'under_review'.

    The original decision fields are left untouched (§7) — we only set status,
    attach the reasoning, and insert a separate appeal record. Returns the
    timestamp used for both writes.
    """
    ts = utc_now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO appeals (
                appeal_id, content_id, creator_id, creator_reasoning,
                original_attribution, original_confidence, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (appeal_id, content_id, creator_id, creator_reasoning,
             original_attribution, original_confidence, ts),
        )
        conn.execute(
            """
            UPDATE submissions
               SET status = 'under_review',
                   creator_reasoning = ?,
                   appeal_id = ?,
                   appealed_at = ?
             WHERE content_id = ?
            """,
            (creator_reasoning, appeal_id, ts, content_id),
        )
    return ts


def recent_entries(limit=20, creator_id=None):
    """Return the most recent audit entries (newest first) as a list of dicts."""
    query = "SELECT * FROM submissions"
    params = []
    if creator_id:
        query += " WHERE creator_id = ?"
        params.append(creator_id)
    query += " ORDER BY timestamp DESC, rowid DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    entries = []
    for row in rows:
        entry = dict(row)
        entry["degraded"] = bool(entry["degraded"])
        entries.append(entry)
    return entries
