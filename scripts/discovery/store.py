#!/usr/bin/env python3
"""signals.db — the discovery engine's local signal store (schema v1).

Deterministic, stdlib-only (sqlite3). Mirrors the research pipeline's
state-dir database pattern (traces.db): the DB lives in ``$OJ_STATE_DIR``,
is gitignored by location, and is never committed. Committed fixtures carry
only sanitized samples (design §6).

Every recorded column has a consumer (D7): see design §4.7 (``--calibrate``,
the cycle summary line, ``research_slug`` linkage, the fixture exporter).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,
  source_key    TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT,
  metrics       TEXT NOT NULL,
  pre_qualify   TEXT,
  score         INTEGER,
  category      TEXT,
  triage_reason TEXT,
  status        TEXT NOT NULL DEFAULT 'NEW',
  research_slug TEXT,
  triggered_at  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
"""

# Status lifecycle: NEW -> TRIAGED -> TRIGGERED -> DONE | FAILED
VALID_STATUSES = frozenset({"NEW", "TRIAGED", "TRIGGERED", "DONE", "FAILED"})

_COLUMNS = (
    "source, source_key, title, url, metrics, pre_qualify, score, category,"
    " triage_reason, status, research_slug, triggered_at, created_at, updated_at"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, sort_keys=True)


def _loads(text: Optional[str]) -> dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


@dataclass
class Signal:
    """One market-signal candidate (design §4.2). ``source_key`` is the
    stable per-source id used for dedupe."""

    source: str
    source_key: str
    title: str
    url: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    pre_qualify: str = ""
    score: Optional[int] = None
    category: str = ""
    triage_reason: str = ""
    status: str = "NEW"
    research_slug: str = ""
    triggered_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    id: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Signal":
        return cls(
            id=row["id"],
            source=row["source"],
            source_key=row["source_key"],
            title=row["title"],
            url=row["url"] or "",
            metrics=_loads(row["metrics"]),
            pre_qualify=row["pre_qualify"] or "",
            score=row["score"],
            category=row["category"] or "",
            triage_reason=row["triage_reason"] or "",
            status=row["status"],
            research_slug=row["research_slug"] or "",
            triggered_at=row["triggered_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


class SignalStore:
    """Thin, typed sqlite3 wrapper around the signals table (schema v1)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SignalStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- reads --------------------------------------------------------------

    def get(self, source: str, source_key: str) -> Optional[Signal]:
        row = self._conn.execute(
            "SELECT * FROM signals WHERE source=? AND source_key=?",
            (source, source_key),
        ).fetchone()
        return Signal.from_row(row) if row else None

    def list_by_status(self, status: str) -> list[Signal]:
        self._check_status(status)
        rows = self._conn.execute(
            "SELECT * FROM signals WHERE status=? ORDER BY id", (status,)
        ).fetchall()
        return [Signal.from_row(r) for r in rows]

    def list_by_source(self, source: str) -> list[Signal]:
        """All signals from one source, oldest first. Ordering by metric
        values is the caller's job (metrics are JSON, not SQL-ordered)."""
        rows = self._conn.execute(
            "SELECT * FROM signals WHERE source=? ORDER BY id", (source,)
        ).fetchall()
        return [Signal.from_row(r) for r in rows]

    def count_by_status(self, status: str) -> int:
        self._check_status(status)
        (n,) = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status=?", (status,)
        ).fetchone()
        return int(n)

    def stats(self) -> dict[str, int]:
        """Counts by status — the cycle summary line's input (D7 consumer)."""
        counts = {status: 0 for status in VALID_STATUSES}
        for status, n in self._conn.execute(
            "SELECT status, COUNT(*) FROM signals GROUP BY status"
        ):
            counts[status] = int(n)
        counts["total"] = sum(counts[s] for s in VALID_STATUSES)
        return counts

    def decision_rows(self) -> list[tuple[Optional[int], str, str]]:
        """``(score, category, status)`` triples for the calibrate consumer
        (D7, design §4.7). ``score`` may be None (never triaged) — the
        consumer filters on it; ``category`` may be blank."""
        return [
            (row[0], row[1] or "", row[2])
            for row in self._conn.execute(
                "SELECT score, category, status FROM signals ORDER BY id"
            )
        ]

    # -- writes -------------------------------------------------------------

    def upsert(self, signal: Signal) -> tuple[bool, int]:
        """Insert or refresh a signal, deduped on ``(source, source_key)``.

        Returns ``(inserted, id)``. On refresh, mutable fields are updated and
        ``updated_at`` bumped; ``status`` is left untouched (re-triage is a
        rules/decide concern, M3).
        """
        now = _now_iso()
        existing = self.get(signal.source, signal.source_key)
        if existing is None:
            cur = self._conn.execute(
                f"INSERT INTO signals ({_COLUMNS}) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    signal.source,
                    signal.source_key,
                    signal.title,
                    signal.url,
                    _dumps(signal.metrics),
                    signal.pre_qualify,
                    signal.score,
                    signal.category,
                    signal.triage_reason,
                    signal.status,
                    signal.research_slug,
                    signal.triggered_at,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            last_id = cur.lastrowid
            assert last_id is not None  # set immediately after INSERT
            return True, int(last_id)
        self._conn.execute(
            "UPDATE signals SET title=?, url=?, metrics=?, pre_qualify=?,"
            " updated_at=? WHERE id=?",
            (
                signal.title,
                signal.url,
                _dumps(signal.metrics),
                signal.pre_qualify,
                now,
                existing.id,
            ),
        )
        self._conn.commit()
        return False, int(existing.id or 0)

    def count_triggered_today(self, now_iso: str) -> int:
        """Triggers recorded on the UTC day of ``now_iso`` — the daily-cap
        input (design §4.7). ``triggered_at`` is written by the cycle as UTC
        ISO, so the date prefix is a day boundary (a signal keeps its
        ``triggered_at`` through DONE/FAILED)."""
        (n,) = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE triggered_at LIKE ?",
            (now_iso[:10] + "%",),
        ).fetchone()
        return int(n)

    def set_status(
        self,
        signal_id: int,
        status: str,
        *,
        score: Optional[int] = None,
        category: Optional[str] = None,
        triage_reason: Optional[str] = None,
        research_slug: Optional[str] = None,
        triggered_at: Optional[str] = None,
    ) -> None:
        """Status transition with optional metadata, and a bumped updated_at."""
        self._check_status(status)
        sets = ["status=?", "updated_at=?"]
        values: list[Any] = [status, _now_iso()]
        for col, val in (
            ("score", score),
            ("category", category),
            ("triage_reason", triage_reason),
            ("research_slug", research_slug),
            ("triggered_at", triggered_at),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                values.append(val)
        values.append(signal_id)
        self._conn.execute(f"UPDATE signals SET {', '.join(sets)} WHERE id=?", values)
        self._conn.commit()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _check_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
            )


__all__ = ["SCHEMA_SQL", "Signal", "SignalStore", "VALID_STATUSES"]
