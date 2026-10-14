# Reader/marker for the Hermes agent.
#
# Two things Hermes cares about:
#  1) A readable source of truth: the month-sharded store (see store.py).
#     `messages.json` holds the newest month; older months live in
#     data/months/YYYY-MM.json. Reads go through the shared store layer.
#  2) A signal for "what is new since last time": the export marker in
#     state.json (last_export_at epoch). Anything with timestamp_epoch
#     > marker is new.
#
# This module:
#   - loads the store (whole history for md/dump, marker-aware subsets for new)
#   - skips month shards that provably predate the marker (never reads
#     thousands of old rows just to find 2 new ones)
#   - can atomically bump the marker so the next run only sees newer stuff
#   - can render a markdown digest for quick Obsidian-style reading
#
# It is intentionally read-only by default; bumping the marker is an explicit
# opt-in call so Hermes only marks things processed when it actually did.

from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Dict, Any

import store

BASE_DIR = Path(__file__).resolve().parent

# Compatibility paths (single active file + monthly shards via store layer).
DATA_DIR = store.resolve_data_dir()
MESSAGES_PATH = store.active_path()
STATE_PATH = store.state_path()


# ---------------------------------------------------------------------------
# IO (thin wrappers; the real logic lives in store.py)
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    return store.load_json(path, default)


def save_json(path: Path, data):
    store.save_json(path, data)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def load_messages() -> List[Dict[str, Any]]:
    return store.load_messages()


def load_state() -> Dict[str, Any]:
    st = store.load_state()
    return {
        "last_export_at": st["last_export_at"],
        "processed_ids": st["processed_ids"],
        "last_message_id_by_channel": st["last_message_id_by_channel"],
    }


def _month(sr: str) -> str:
    """'YYYY-MM' for an epoch cutoff (UTC)."""
    return datetime.fromtimestamp(float(sr), tz=timezone.utc).strftime("%Y-%m")


def new_records_since(epoch: int | float | None = None) -> List[Dict[str, Any]]:
    """Return records newer than `epoch` (chronologically ascending).

    The export marker is the primary signal: only records whose
    timestamp_epoch is strictly greater than the marker are returned. This is
    the "what arrived since last time Hermes ran" signal.

    Month shards older than the marker's month are skipped entirely: records
    are bucketed by their own UTC month, so a shard for a month strictly
    before the marker's month cannot contain anything newer than the marker.
    This keeps reads bounded no matter how the archive grows.

    As a safety net for same-second collisions (two messages in the same
    wall-clock second, where an integer-second marker would otherwise hide
    the second one), a record is also considered new if its message_id is not
    in processed_ids AND its timestamp_epoch is greater than a *fractional*
    marker. When `epoch` is passed explicitly as an int, we treat it as an
    exact cutoff and do not apply the processed_ids safety net (caller knows
    what it wants).

    If epoch is None, read the export marker from state.json. When there is
    no marker yet, returns every record (first run).
    """
    marker = epoch
    if marker is None:
        state = load_state()
        marker = state.get("last_export_at")

    marker_float = float(marker) if marker is not None else None

    messages: List[Dict[str, Any]] = store.load_active()
    if marker_float is None:
        # First run: everything is new. Include every shard.
        messages = store.load_messages()
    else:
        marker_month = _month(marker_float)
        months_dir = store.months_dir()
        shard_paths = sorted(months_dir.glob("*.json")) if months_dir.exists() else []
        for shelf in shard_paths:
            month = store.shard_month(shelf)
            if month is None or month >= marker_month:
                # This month *could* contain records newer than the marker.
                messages = messages + store.load_json(shelf, [])

    if not messages:
        return []

    use_safety_net = marker_float is not None and marker_float != int(marker_float)

    out = []
    processed = load_state().get("processed_ids", set()) if use_safety_net else set()
    for msg in messages:
        ts = msg.get("timestamp_epoch", 0)
        if marker_float is None or ts > marker_float:
            out.append(msg)
        elif use_safety_net and msg.get("message_id") not in processed and ts >= marker_float:
            # Same-second edge case: timestamp equals the fractional marker
            # but this id was never marked processed, so treat as new.
            out.append(msg)
    out.sort(key=lambda record: (record.get("timestamp_epoch", 0), str(record.get("message_id", ""))))
    return out


def all_by_category() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for m in load_messages():
        out.setdefault(m.get("category", "unknown"), []).append(m)
    return out


# ---------------------------------------------------------------------------
# Mutations (opt-in, only when Hermes is done with a batch)
# ---------------------------------------------------------------------------
def bump_export_marker(epoch: int | float | None = None) -> float:
    """Atomically set last_export_at to `epoch` (defaults to now, sub-second).

    Uses a fractional epoch so two messages arriving in the same wall-clock
    second can still be distinguished as "new" after a bump. Returns the new
    marker epoch.
    """
    if epoch is None:
        epoch = datetime.now(timezone.utc).timestamp()
    elif isinstance(epoch, int):
        epoch = float(epoch)
    state = load_state()
    state["last_export_at"] = epoch
    # Persist state only; the message store is the source of truth and untouched.
    store.save_state(state)
    return epoch


def add_processed_ids(ids: Iterable[str]) -> None:
    """Record that these message ids have been handled by Hermes.

    Useful if Hermes processes records out of order or batches them and you
    want the bot to stop re-sending them next time it catches up. Safe to
    call even if some ids are already present.
    """
    state = load_state()
    before = len(state["processed_ids"])
    state["processed_ids"] |= set(ids)
    if len(state["processed_ids"]) != before:
        store.save_state(state)


# ---------------------------------------------------------------------------
# Markdown digest (for browsing / Obsidian)
# ---------------------------------------------------------------------------
def render_md(records: List[Dict[str, Any]]) -> str:
    if not records:
        return "*No records.*"

    lines: List[str] = []
    lines.append(f"# Hermes Discord Log — {len(records)} record(s)\n")

    for r in records:
        cat = r.get("category", "unknown")
        author = r.get("author_name", "?")
        ts = r.get("timestamp_utc", "")
        try:
            pretty = datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            pretty = ts
        lines.append(f"## [{cat}] {author}  —  {pretty}\n")
        content = r.get("content") or ""
        if content:
            lines.append(f"\n{content}\n")
        atts = r.get("attachments") or []
        if atts:
            lines.append("\n**Attachments:**\n")
            for a in atts:
                lines.append(f"- {a.get('filename')} — {a.get('url')}\n")
        ref = r.get("referenced_message_id")
        if ref:
            lines.append(f"\n*Reply to message id:* `{ref}`\n")
        if r.get("is_edit"):
            lines.append(f"*Edited:* {r.get('edit_timestamp_utc')}\n")
        lines.append("---\n")

    return "".join(lines)


def render_md_by_category() -> str:
    by_cat = all_by_category()
    if not by_cat:
        return "*No records.*"
    parts = []
    for cat in sorted(by_cat.keys()):
        parts.append(f"\n\n# {cat.upper()}\n")
        parts.append(render_md(by_cat[cat]))
    return "# Hermes Discord Log — by category\n\n" + "".join(parts)


# ---------------------------------------------------------------------------
# CLI (for Hermes to call directly or for manual checks)
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hermes reader for the Discord log")
    sp = p.add_subparsers(dest="command")

    np_ = sp.add_parser("new", help="print new records since last export marker")
    np_.add_argument("--json", action="store_true", help="emit JSON instead of table")
    np_.add_argument("--mark", action="store_true", help="after printing, bump export marker to now")

    sp.add_parser("dump", help="print all stored records as JSON")

    npall_ = sp.add_parser("new-all", help="print all records (ignore marker)")
    npall_.add_argument("--json", action="store_true")

    md_ = sp.add_parser("md", help="render markdown digest (all)")
    md_.add_argument("--category", help="render only this category")

    cat_ = sp.add_parser("categories", help="list categories and counts")

    bump_ = sp.add_parser("bump", help="set export marker to now (or given epoch)")
    bump_.add_argument("--epoch", type=int, help="epoch to set (default: now)")

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)

    if args.command == "dump":
        print(json.dumps(load_messages(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "new":
        recs = new_records_since()
        if args.json:
            print(json.dumps(recs, indent=2, ensure_ascii=False))
        else:
            if not recs:
                print("No new records since last export marker.")
            else:
                print(f"{len(recs)} new record(s) since last marker:\n")
                for r in recs:
                    ts = r.get("timestamp_utc", "?")
                    print(f"[{r.get('category')}] {r.get('author_name')}  {ts}")
                    print(f"  {r.get('content') or '(attachment/editor)'}")
                    print()
        if args.mark:
            bump_export_marker()
            print("Export marker bumped to now.")
        return 0

    if args.command == "new-all":
        recs = load_messages()
        if args.json:
            print(json.dumps(recs, indent=2, ensure_ascii=False))
        else:
            print(f"{len(recs)} total record(s).\n")
            for r in recs:
                ts = r.get("timestamp_utc", "?")
                print(f"[{r.get('category')}] {r.get('author_name')}  {ts}")
                print(f"  {r.get('content') or '(attachment/editor)'}")
                print()
        return 0

    if args.command == "md":
        by_cat = all_by_category()
        if args.category:
            recs = by_cat.get(args.category, [])
            print(render_md(recs))
        else:
            print(render_md_by_category())
        return 0

    if args.command == "categories":
        by_cat = all_by_category()
        for cat in sorted(by_cat.keys()):
            print(f"{cat}: {len(by_cat[cat])}")
        return 0

    if args.command == "bump":
        epoch = args.epoch if args.epoch else datetime.now(timezone.utc).timestamp()
        new_epoch = bump_export_marker(epoch)
        print(f"Export marker set to {new_epoch} ({datetime.fromtimestamp(new_epoch, tz=timezone.utc).isoformat()})")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
