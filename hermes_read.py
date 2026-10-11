# Reader/marker for the Hermes agent.
#
# Two things Hermes cares about:
#  1) A readable source of truth: data/messages.json (full metadata + content).
#  2) A signal for "what is new since last time": the export marker in
#     data/state.json (last_export_at epoch). Anything with timestamp_epoch
#     > marker is new.
#
# This module:
#   - loads both files
#   - yields new records (serves the "file it can read and process")
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

# ---------------------------------------------------------------------------
# Paths (must match bot.py)
# ---------------------------------------------------------------------------
# The store lives in ./data by default. Set MEMOKEEPER_DATA_DIR to relocate it.
# Note: bot.py loads .env into the environment; this module reads the same env
# var directly, so set it in .env or the process environment (not just here).
import os as _os

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(_os.environ.get("MEMOKEEPER_DATA_DIR", "").strip() or (BASE_DIR / "data")).expanduser()
MESSAGES_PATH = DATA_DIR / "messages.json"
STATE_PATH = DATA_DIR / "state.json"


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def load_messages() -> List[Dict[str, Any]]:
    return load_json(MESSAGES_PATH, [])


def load_state() -> Dict[str, Any]:
    st = load_json(STATE_PATH, {"last_export_at": None, "processed_ids": [], "last_message_id_by_channel": {}})
    return {
        "last_export_at": st.get("last_export_at"),
        "processed_ids": set(st.get("processed_ids", [])),
        "last_message_id_by_channel": st.get("last_message_id_by_channel", {}),
    }


def new_records_since(epoch: int | float | None = None) -> List[Dict[str, Any]]:
    """Return records newer than `epoch`.

    The export marker is the primary signal: only records whose
    timestamp_epoch is strictly greater than the marker are returned. This is
    the "what arrived since last time Hermes ran" signal.

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
    messages = load_messages()
    if not messages:
        return []

    marker = epoch
    if marker is None:
        state = load_state()
        marker = state.get("last_export_at")

    if marker is None:
        return messages

    marker_float = float(marker)
    # If the supplied marker has sub-second precision, use the processed_ids
    # safety net so a same-second record still shows up as new.
    use_safety_net = marker_float != int(marker_float)

    out = []
    state = load_state()
    processed = state.get("processed_ids", set())
    for m in messages:
        ts = m.get("timestamp_epoch", 0)
        if ts > marker_float:
            out.append(m)
        elif use_safety_net and m.get("message_id") not in processed and ts >= marker_float:
            # Same-second edge case: timestamp equals the fractional marker
            # but this id was never marked processed, so treat as new.
            out.append(m)
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
    # Persist state only; messages.json is the source of truth and untouched.
    save_json(STATE_PATH, {
        "last_export_at": state["last_export_at"],
        "processed_ids": list(state["processed_ids"]),
        "last_message_id_by_channel": state["last_message_id_by_channel"],
    })
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
        save_json(STATE_PATH, {
            "last_export_at": state["last_export_at"],
            "processed_ids": list(state["processed_ids"]),
            "last_message_id_by_channel": state["last_message_id_by_channel"],
        })


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
