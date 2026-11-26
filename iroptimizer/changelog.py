"""What changed on the PC between tests: automatic setting diffs plus the user's own notes.

The audit's values are snapshotted at each app start and before each test. When a value differs from the last
snapshot, a "change" entry is logged ("Power plan: Balanced to High performance"). Notes are for what the app
can't see (SimHub capture off, a new cable). Both go into changes.jsonl in the results folder, and the Results
timeline shows them between the tests they came before and after.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

LOG = "changes.jsonl"
LAST = "last_settings.json"
VOLATILE = {"Windows/Timer resolution", "NVIDIA/Driver settings"}  # change on their own; not worth logging
MAX_NOTE = 1000
_lock = threading.Lock()


def snapshot(audit_result: dict) -> dict[str, str]:
    """{"Area/Name": value} for every setting the audit could read."""
    out = {}
    for i in audit_result.get("items") or []:
        k = f"{i['area']}/{i['name']}"
        if i.get("summary") or k in VOLATILE or i["value"] == "Couldn't read" or i["area"] == "App":
            continue
        out[k] = i["value"]
    return out


def diff(old: dict, new: dict) -> list[dict]:
    """Settings whose value changed, or that appeared or vanished, as {"area", "name", "from", "to"}."""
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a != b:
            area, _, name = k.partition("/")
            out.append({"area": area, "name": name, "from": a, "to": b})
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _append(folder: Path, entry: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    return entry


def record(folder: Path, snap: dict, reason: str = "") -> dict | None:
    """Compares snap with the last one, logs the differences, and keeps snap as the new last one.
    The first snapshot only sets the starting point. Returns the logged entry, if any."""
    folder = Path(folder)
    with _lock:
        last_f = folder / LAST
        try:
            old = json.loads(last_f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        entry = None
        if old is not None:
            changes = diff(old, snap)
            if changes:
                entry = _append(folder, {"t": _now(), "kind": "change", "reason": reason, "changes": changes})
        if old != snap:
            folder.mkdir(parents=True, exist_ok=True)
            last_f.write_text(json.dumps(snap, ensure_ascii=True, indent=0), encoding="utf-8")
        return entry


def add_note(folder: Path, text: str) -> dict:
    text = " ".join(str(text or "").split())[:MAX_NOTE]
    if not text:
        raise ValueError("The note is empty.")
    with _lock:
        return _append(Path(folder), {"t": _now(), "kind": "note", "text": text})


def load(folder: Path) -> list[dict]:
    try:
        lines = (Path(folder) / LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("t") and e.get("kind") in ("change", "note"):
            out.append(e)
    return sorted(out, key=lambda e: e["t"])


def describe(e: dict, limit: int = 6) -> str:
    if e["kind"] == "note":
        return e["text"]
    def one(c):
        if c["from"] is None:
            return f"{c['name']}: now {c['to']}"
        if c["to"] is None:
            return f"{c['name']}: no longer read (was {c['from']})"
        return f"{c['name']}: {c['from']} to {c['to']}"
    ch = e.get("changes") or []
    more = f", and {len(ch) - limit} more" if len(ch) > limit else ""
    return "; ".join(one(c) for c in ch[:limit]) + more
