"""
Persistent store + change detection (the "pipeline" layer).

A one-off scrape answers "what is on the site now?". A pipeline answers
"what changed since last time?" — that is the question clients pay monthly for.

SQLite, one file, no server. Each run is recorded; each item is keyed by its
own stable ID (UPC, or text+author for quotes) and hashed, so the diff is:

    new       — key never seen before
    changed   — key seen before, content hash differs
    unchanged — same hash
    gone      — seen in the previous run of this source, missing now
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started     TEXT NOT NULL,
    finished    TEXT,
    n_new       INTEGER DEFAULT 0,
    n_changed   INTEGER DEFAULT 0,
    n_unchanged INTEGER DEFAULT 0,
    n_gone      INTEGER DEFAULT 0,
    n_failures  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    source       TEXT NOT NULL,
    key          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    last_seen_run  INTEGER NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS changes (
    run_id   INTEGER NOT NULL,
    source   TEXT NOT NULL,
    key      TEXT NOT NULL,
    kind     TEXT NOT NULL,      -- new | changed | gone
    before   TEXT,               -- payload JSON before (changed/gone)
    after    TEXT                -- payload JSON after  (new/changed)
);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
"""


@dataclass
class Diff:
    run_id: int
    new: List[str] = field(default_factory=list)
    changed: List[Tuple[str, dict, dict]] = field(default_factory=list)   # key, before, after
    gone: List[Tuple[str, dict]] = field(default_factory=list)            # key, last payload
    unchanged: int = 0
    is_first_run: bool = False

    @property
    def summary(self) -> str:
        if self.is_first_run:
            return f"first run: {len(self.new)} items stored (no baseline to compare against)"
        return (f"+{len(self.new)} new, ~{len(self.changed)} changed, "
                f"-{len(self.gone)} gone, {self.unchanged} unchanged")


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _previous_run(self, source: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE source=? AND finished IS NOT NULL ORDER BY id DESC LIMIT 1",
            (source,)).fetchone()
        return row["id"] if row else None

    def record_run(self, source: str, items: Iterable[Tuple[str, dict]], *,
                   n_failures: int = 0, started: Optional[datetime] = None,
                   crawl_complete: bool = True) -> Diff:
        """
        items: iterable of (stable_key, payload_dict).
        crawl_complete=False (e.g. a listing page failed) suppresses "gone"
        detection — an item missing because we could not fetch its page is not
        an item that disappeared from the site. That distinction matters.
        """
        started = started or datetime.now()
        prev = self._previous_run(source)
        cur = self.conn.execute("INSERT INTO runs(source, started) VALUES (?, ?)",
                                (source, started.isoformat(timespec="seconds")))
        run_id = cur.lastrowid
        diff = Diff(run_id=run_id, is_first_run=prev is None)

        existing: Dict[str, sqlite3.Row] = {
            r["key"]: r for r in self.conn.execute(
                "SELECT key, content_hash, payload, last_seen_run FROM items WHERE source=?", (source,))}

        seen_keys = set()
        for key, payload in items:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            h = _hash(payload)
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            row = existing.get(key)
            if row is None:
                diff.new.append(key)
                self.conn.execute(
                    "INSERT INTO items VALUES (?,?,?,?,?,?)", (source, key, h, text, run_id, run_id))
                self.conn.execute("INSERT INTO changes VALUES (?,?,?,?,?,?)",
                                  (run_id, source, key, "new", None, text))
            elif prev is not None and row["last_seen_run"] != prev:
                # was gone in the previous run and is back -> report as new again
                diff.new.append(key)
                self.conn.execute(
                    "UPDATE items SET content_hash=?, payload=?, last_seen_run=? WHERE source=? AND key=?",
                    (h, text, run_id, source, key))
                self.conn.execute("INSERT INTO changes VALUES (?,?,?,?,?,?)",
                                  (run_id, source, key, "new", row["payload"], text))
            elif row["content_hash"] != h:
                diff.changed.append((key, json.loads(row["payload"]), payload))
                self.conn.execute(
                    "UPDATE items SET content_hash=?, payload=?, last_seen_run=? WHERE source=? AND key=?",
                    (h, text, run_id, source, key))
                self.conn.execute("INSERT INTO changes VALUES (?,?,?,?,?,?)",
                                  (run_id, source, key, "changed", row["payload"], text))
            else:
                diff.unchanged += 1
                self.conn.execute("UPDATE items SET last_seen_run=? WHERE source=? AND key=?",
                                  (run_id, source, key))

        if prev is not None and crawl_complete:
            for key, row in existing.items():
                if key not in seen_keys and row["last_seen_run"] == prev:
                    diff.gone.append((key, json.loads(row["payload"])))
                    self.conn.execute("INSERT INTO changes VALUES (?,?,?,?,?,?)",
                                      (run_id, source, key, "gone", row["payload"], None))

        self.conn.execute(
            "UPDATE runs SET finished=?, n_new=?, n_changed=?, n_unchanged=?, n_gone=?, n_failures=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), len(diff.new), len(diff.changed),
             diff.unchanged, len(diff.gone), n_failures, run_id))
        self.conn.commit()
        return diff

    def history(self, source: str, limit: int = 10) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE source=? ORDER BY id DESC LIMIT ?", (source, limit)).fetchall()

    def current_items(self, source: str) -> List[dict]:
        """Items present in the latest completed run (gone items are kept in the table for history)."""
        last = self._previous_run(source)
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM items WHERE source=? AND last_seen_run=? ORDER BY key", (source, last))]


def diff_markdown(diff: Diff, source: str, label_field: str = "title", max_rows: int = 20) -> List[str]:
    lines = [f"## Changes since last run ({source})", "", f"- {diff.summary}", ""]
    if diff.is_first_run:
        return lines
    if diff.new:
        lines += ["### New", ""] + [f"- {k}" for k in diff.new[:max_rows]] + [""]
    if diff.changed:
        lines += ["### Changed", "", "| Key | Field | Before | After |", "|---|---|---|---|"]
        for key, before, after in diff.changed[:max_rows]:
            for f in sorted(set(before) | set(after)):
                if before.get(f) != after.get(f):
                    lines.append(f"| {key} | {f} | {before.get(f)} | {after.get(f)} |")
        lines.append("")
    if diff.gone:
        lines += ["### Gone", ""] + [f"- {k} ({p.get(label_field, '')})" for k, p in diff.gone[:max_rows]] + [""]
    return lines
