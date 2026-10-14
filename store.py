# Shared storage layer for Memokeeper.
#
# Layout (by month so the reader never loads an unbounded file):
#   <data_dir>/messages.json     -> bucket holding the NEWEST month's records
#   <data_dir>/months/YYYY-MM.json -> sealed buckets for older months
#   <data_dir>/state.json          -> cursors + processed ids + export marker
#
# Which bucket a record belongs to is decided by the record's own
# timestamp (UTC month), so live capture and catch-up backfill can both
# write through the same functions without special-casing.
#
# save_all() rebuckets everything from a single in-memory source of truth:
# the newest month becomes messages.json (the "active" file), every other
# month becomes/exends a sealed shard. load_messages() reads active + all
# shards and dedupes defensively by message_id, so a stale shard file
# can never produce a duplicate.

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Load .env from project root if present (same logic as bot.py) so a value
# set only in .env works for standalone CLI runs too. setdefault: an explicit
# process env var wins.
_env_path = BASE_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())


def resolve_data_dir() -> Path:
    d = Path(os.environ.get("MEMOKEEPER_DATA_DIR", "").strip() or (BASE_DIR / "data")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_path() -> Path:
    return resolve_data_dir() / "messages.json"


def months_dir() -> Path:
    return resolve_data_dir() / "months"


def state_path() -> Path:
    return resolve_data_dir() / "state.json"


def shard_path(month: str) -> Path:
    return months_dir() / f"{month}.json"


def record_month(record: dict) -> str:
    """UTC month ('YYYY-MM') a record belongs to, derived from its timestamp."""
    ts = record.get("timestamp_epoch")
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m")
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m")


def shard_month(path: Path) -> str | None:
    """'YYYY-MM' encoded in a shard filename like months/2026-10.json."""
    stem = path.stem
    if len(stem) == 7 and stem[4] == "-" and stem[:4].isdigit() and stem[5:7].isdigit():
        return stem
    return None


# ---------------------------------------------------------------------------
# JSON IO
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def load_active() -> list:
    return load_json(active_path(), [])


def load_shards() -> list:
    """All records from every month shard (unordered)."""
    out = []
    mdir = months_dir()
    if mdir.exists():
        for p in sorted(mdir.glob("*.json")):
            if shard_month(p):  # skip stray non-month files
                out.extend(load_json(p, []))
    return out


def load_shard(month: str) -> list:
    return load_json(shard_path(month), [])


def load_messages() -> list:
    """ALL records (active + shards), deduped by message_id.

    Order: shards (oldest month first) then active file order. Duplicate
    ids keep their first occurrence -- defensive against stale shard files
    overlapping with the active bucket.
    """
    out: list = []
    seen: set = set()
    for rec in load_shards() + load_active():
        mid = rec.get("message_id") if isinstance(rec, dict) else None
        if mid is None:
            out.append(rec)
            continue
        if mid in seen:
            continue
        seen.add(mid)
        out.append(rec)
    return out


def load_state() -> dict:
    st = load_json(state_path(), {
        "last_message_id_by_channel": {},
        "last_export_at": None,
        "processed_ids": [],
    })
    return {
        "last_message_id_by_channel": dict(st.get("last_message_id_by_channel", {})),
        "last_export_at": st.get("last_export_at"),
        "processed_ids": set(st.get("processed_ids", [])),
    }


def save_state(state: dict) -> None:
    save_json(state_path(), {
        "last_message_id_by_channel": state.get("last_message_id_by_channel", {}),
        "last_export_at": state.get("last_export_at"),
        "processed_ids": sorted(state.get("processed_ids", set())),
    })


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def save_all(records: list) -> dict:
    """Rebucket the full in-memory store to disk (sharded by month).

    Returns {"active_month": YYYY-MM, "shards": {month: count}, "total": n}.

    - The bucket with the NEWEST month becomes messages.json (active file).
    - Every other month goes to months/YYYY-MM.json.
    - Shard files for the active month are deleted so records never exist
      in two places at once.
    """
    if not records:
        # Nothing to store: emergency reset writes an empty active file but
        # NEVER deletes shards (they are the sealed history).
        save_json(active_path(), [])
        return {"active_month": None, "shards": {}, "total": 0}

    buckets: dict[str, list] = {}
    for rec in records:
        buckets.setdefault(record_month(rec), []).append(rec)

    active_month = max(buckets)
    counts: dict[str, int] = {}

    for month, recs in buckets.items():
        if month == active_month:
            save_json(active_path(), recs)
        else:
            save_json(shard_path(month), recs)
            counts[month] = len(recs)  # shards only; active month excluded

    months_dir().mkdir(parents=True, exist_ok=True)
    for p in months_dir().glob("*.json"):
        m = shard_month(p)
        if m == active_month:
            p.unlink()  # active month must not also live in a shard

    return {"active_month": active_month, "shards": counts, "total": len(records)}


def append_records(new_records: list) -> dict:
    """Merge new records into the store (convenience wrapper around load+save)."""
    merged = load_messages()
    existing = {r.get("message_id") for r in merged if isinstance(r, dict)}
    added = 0
    for rec in new_records:
        if not isinstance(rec, dict):
            continue
        mid = rec.get("message_id")
        if mid is None or mid in existing:
            continue
        existing.add(mid)
        merged.append(rec)
        added += 1
    info = save_all(merged)
    info["added"] = added
    return info
