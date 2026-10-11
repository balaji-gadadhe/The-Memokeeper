# Discord history catch-up importer.
#
# Run this once (manually, or on a schedule) to backfill message history into
# data/messages.json. It reads each tracked channel's history and appends only
# the messages that are not yet in the store, using message_id as the dedup key.
#
# It is designed to run against a bot token with message_content intent (same
# setup as bot.py). It uses the same .env loading and channel->category map.
#
# Semantics:
#  - For each channel we read history newest-first and stop at the first
#    already-known message id (the frontier). Everything older is already
#    captured, so we catch up only to the last saved message, not re-fetching
#    everything.
#  - Newly fetched messages are appended to messages.json with the same record
#    shape the live bot uses (author_id, author_name, content, attachments,
#    referenced_message_id, timestamp_utc/epoch, message_id, category, channel).
#  - After a successful backfill we update state.json:
#      * add the new message_ids to processed_ids
#      * set last_message_id_by_channel for each channel to the newest fetched id
#      * leave last_export_at alone by default (Hermes decides when a batch is
#        processed). Use --mark to also move the export marker.
#
# Idempotency: if you run it again later, any messages already in the store are
# skipped, and any new history that arrived in the gap is appended.

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Paths (must match bot.py / hermes_read.py)
# ---------------------------------------------------------------------------
# The store lives in ./data by default. Set MEMOKEEPER_DATA_DIR to relocate it.
# main() calls load_env() first, then re-resolves the paths via resolve_paths()
# so a value set in .env takes effect for CLI runs too.
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def resolve_paths() -> None:
    """(Re)compute DATA_DIR / MESSAGES_PATH / STATE_PATH from the environment.

    Called once at import (defaults) and again after load_env() so a value in
    .env is picked up. bot.py/hermes_read.py use the same env var directly.
    """
    global DATA_DIR, MESSAGES_PATH, STATE_PATH
    DATA_DIR = Path(
        os.environ.get("MEMOKEEPER_DATA_DIR", "").strip() or (BASE_DIR / "data")
    ).expanduser()
    MESSAGES_PATH = DATA_DIR / "messages.json"
    STATE_PATH = DATA_DIR / "state.json"


resolve_paths()

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hermes-backfill")


# ---------------------------------------------------------------------------
# Env loading (same logic as bot.py)
# ---------------------------------------------------------------------------
def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


# ---------------------------------------------------------------------------
# IO helpers (same shape as bot.py / hermes_read.py)
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Corrupt %s, starting fresh: %s", path.name, e)
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def build_channel_category_map():
    return {
        int(os.environ.get("DISCORD_CHANNEL_JOURNAL", 0)): "journal",
        int(os.environ.get("DISCORD_CHANNEL_COLLEGE", 0)): "college",
        int(os.environ.get("DISCORD_CHANNEL_IDEAS_PROJECTS", 0)): "ideas-projects",
        int(os.environ.get("DISCORD_CHANNEL_TASKS", 0)): "tasks",
        int(os.environ.get("DISCORD_CHANNEL_MISCELLANEOUS", 0)): "miscellaneous",
    }


def record_from_message(message: discord.Message, category: str) -> dict:
    created_at = message.created_at
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    ref_id = None
    if message.reference:
        ref_id = str(message.reference.message_id)

    return {
        "message_id": str(message.id),
        "author_id": str(message.author.id),
        "author_name": message.author.name,
        "channel_id": message.channel.id,
        "channel_name": message.channel.name or "",
        "category": category,
        "timestamp_utc": created_at.isoformat(),
        "timestamp_epoch": created_at.timestamp(),
        "content": message.content,
        "attachments": [
            {"filename": a.filename, "url": a.url, "size": a.size}
            for a in message.attachments
        ],
        "referenced_message_id": ref_id,
        "is_edit": False,
        "edit_timestamp_utc": None,
    }


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def load_state():
    st = load_json(STATE_PATH, {
        "last_message_id_by_channel": {},
        "last_export_at": None,
        "processed_ids": [],
    })
    return {
        "last_message_id_by_channel": st.get("last_message_id_by_channel", {}),
        "last_export_at": st.get("last_export_at"),
        "processed_ids": set(st.get("processed_ids", [])),
    }


def save_state(state):
    save_json(STATE_PATH, {
        "last_message_id_by_channel": state["last_message_id_by_channel"],
        "last_export_at": state["last_export_at"],
        "processed_ids": list(state["processed_ids"]),
    })


def load_messages():
    return load_json(MESSAGES_PATH, [])


def save_messages(messages):
    save_json(MESSAGES_PATH, messages)


# ---------------------------------------------------------------------------
# Per-channel cursor: the newest id already stored for that channel.
# We use this as the primary "already caught up to" signal per channel.
# ---------------------------------------------------------------------------
def channel_frontier(messages, state):
    """Return {channel_id_str: newest_known_message_id_or_None} for each channel.

    Uses state.last_message_id_by_channel when present (the live bot updates
    this every time it stores a message). Otherwise derives the newest stored
    id per channel from messages.json itself, so the importer works even if
    the bot has never run.
    """
    frontier: dict[str, str | None] = {}
    stored_cursor = state.get("last_message_id_by_channel", {})

    # Derive newest-per-channel from the store when the cursor isn't set.
    newest_in_store: dict[str, str] = {}
    for m in messages:
        ch_id_str = str(m.get("channel_id"))
        mid = m.get("message_id")
        if not ch_id_str or not mid:
            continue
        prev = newest_in_store.get(ch_id_str)
        if prev is None or mid > prev:
            newest_in_store[ch_id_str] = mid

    all_channel_ids = set(list(stored_cursor.keys()) + list(newest_in_store.keys()))
    for channel_id_str in all_channel_ids:
        frontier[channel_id_str] = stored_cursor.get(channel_id_str) or newest_in_store.get(channel_id_str)

    return frontier


def already_known_message_ids(messages, state):
    known = set(state.get("processed_ids", set()))
    for m in messages:
        mid = m.get("message_id")
        if mid:
            known.add(mid)
    return known


# ---------------------------------------------------------------------------
# Backfill logic
# ---------------------------------------------------------------------------
async def backfill_channel_history(
    channel: discord.TextChannel,
    category: str,
    known_ids: set[str],
    frontier_id: str | None,
    limit: int | None = None,
) -> tuple[list[dict], str | None]:
    """Read history for one channel and return (missing_records, newest_id_seen).

    Reads newest-first (the Discord.py default). As soon as we encounter a
    message whose id is already known (in known_ids, or equal to frontier_id),
    we stop for that channel, because everything older than that frontier has
    already been captured.

    This is robust to gaps in history: we only take messages newer than the
    frontier, never re-fetching anything already stored, and gaps do not cause
    us to stop early.

    Messages from users other than the allowed user (if configured) are still
    consumed as "seen" (added to known_ids) so we do not re-walk past them, but
    they are not recorded.

    If `limit` is set, we stop after fetching that many *missing* messages.
    """
    missing: list[dict] = []
    newest_recorded_id: str | None = None
    fetched_missing = 0

    # newest_first (default): walk backward from the most recent message.
    # The first already-known message we hit is the frontier; everything after
    # it (older) is already captured, so we stop.
    # channel.history() returns an AsyncIterator in discord.py.
    async for msg in channel.history(oldest_first=False, limit=None):
        mid = str(msg.id)

        # Already captured (globally known, or exactly this channel's frontier)?
        # Everything older is already stored; stop.
        if mid in known_ids or mid == frontier_id:
            break

        # Honor the allowed-users filter: skip other users but still mark the
        # id as seen so we do not re-walk past it next run.
        allowed = os.environ.get("DISCORD_ALLOWED_USERS", "").strip()
        if allowed and str(msg.author.id) != allowed:
            known_ids.add(mid)
            continue

        rec = record_from_message(msg, category)
        missing.append(rec)
        known_ids.add(mid)
        fetched_missing += 1
        # Track the newest recorded id (string comparison of numeric ids works).
        if newest_recorded_id is None or mid > newest_recorded_id:
            newest_recorded_id = mid

        if limit is not None and fetched_missing >= limit:
            break

    # The channel cursor we persist should be the newest *recorded* id for this
    # channel. If we recorded nothing new, fall back to the frontier id (so the
    # cursor stays accurate to where we stopped).
    newest_id: str | None = newest_recorded_id or frontier_id

    return missing, newest_id


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------
TOKEN = None
ALLOWED_USER_ID = None
SYS_LOG_CHANNEL_ID = None


async def run_backfill(args):
    global TOKEN, ALLOWED_USER_ID, SYS_LOG_CHANNEL_ID

    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        log.error("DISCORD_TOKEN not set. Copy .env.example to .env.")
        return 1

    ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USERS")
    SYS_LOG_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_LOG", 0) or 0)

    category_map = build_channel_category_map()
    if not category_map:
        log.error("No tracked channels configured in .env.")
        return 1

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        await do_backfill(bot, category_map, args)
        await bot.close()

    await bot.start(TOKEN)


async def do_backfill(bot, category_map, args):
    log.info("Connected as %s. Starting backfill for %d channels.", bot.user.name, len(category_map))

    state = load_state()
    messages = load_messages()
    frontier = channel_frontier(messages, state)

    # Build the full set of known ids from the store + state, so we skip
    # anything already captured across all channels.
    known_ids = already_known_message_ids(messages, state)

    total_new = 0
    per_channel_result = {}

    for channel_id, category in category_map.items():
        channel = bot.get_channel(channel_id)
        if channel is None:
            log.warning("Cannot resolve channel %s (%s) — skipping.", channel_id, category)
            continue
        if not isinstance(channel, discord.TextChannel):
            log.warning("Channel %s (%s) is not a text channel — skipping.", channel_id, category)
            continue

        ch_frontier_id = frontier.get(str(channel_id))
        new_recs, newest_id = await backfill_channel_history(
            channel,
            category,
            known_ids,
            ch_frontier_id,
            limit=args.limit,
        )

        if new_recs:
            messages.extend(new_recs)
            total_new += len(new_recs)
            per_channel_result[str(channel_id)] = {
                "category": category,
                "new_count": len(new_recs),
                "newest_message_id": newest_id,
            }
            log.info(
                "Channel #%s [%s]: added %d new messages, newest=%s",
                channel.name or channel_id,
                category,
                len(new_recs),
                newest_id,
            )
        else:
            per_channel_result[str(channel_id)] = {
                "category": category,
                "new_count": 0,
                "newest_message_id": frontier.get(str(channel_id)),
            }
            log.info(
                "Channel #%s [%s]: already up to date (frontier=%s).",
                channel.name or channel_id,
                category,
                frontier.get(str(channel_id)),
            )

    # Persist everything atomically.
    save_messages(messages)

    # Update state: seed processed_ids with every id we now know about and
    # update the per-channel cursor to the newest id for each channel we touched.
    state["processed_ids"] |= known_ids
    for ch_id_str, res in per_channel_result.items():
        # Only write a cursor when we actually have a message id. A channel
        # with no history (newest_message_id is None) must NOT get a null
        # cursor — a null cursor is indistinguishable from "never run".
        if res["newest_message_id"]:
            state["last_message_id_by_channel"][ch_id_str] = res["newest_message_id"]
    save_state(state)

    log.info("Backfill complete. Total new messages added: %d", total_new)
    if SYS_LOG_CHANNEL_ID:
        try:
            ch = bot.get_channel(SYS_LOG_CHANNEL_ID)
            if ch:
                await ch.send(f"Backfill complete. Added {total_new} new messages.")
        except Exception as e:
            log.error("Failed to send backfill confirmation: %s", e)

    if args.mark and total_new:
        # Move the export marker to now so Hermes treats this whole batch as
        # "processed" on its next run (only if you want that default).
        from hermes_read import bump_export_marker
        marker = bump_export_marker()
        log.info("Export marker bumped to %s", marker)
        try:
            if SYS_LOG_CHANNEL_ID:
                ch = bot.get_channel(SYS_LOG_CHANNEL_ID)
                if ch:
                    await ch.send(f"Backfill complete. Added {total_new} messages. Export marker bumped.")
        except Exception as e:
            log.error("Failed to send marker-confirmation: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Catch up Discord message history into data/messages.json")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max missing messages to fetch per channel (None = all missing history).",
    )
    p.add_argument(
        "--mark",
        action="store_true",
        help="after backfill, also move the last_export_at marker to now so Hermes treats the batch as processed.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="load state and report what would be caught up without writing anything.",
    )
    return p


def main():
    load_env()
    resolve_paths()  # pick up MEMOKEEPER_DATA_DIR from .env if set
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parser = build_parser()
    args = parser.parse_args()

    if args.dry_run:
        state = load_state()
        messages = load_messages()
        known = already_known_message_ids(messages, state)
        total_stored = len(messages)
        known_count = len(known)
        log.info("Dry run. Stored messages: %d, known ids: %d", total_stored, known_count)
        for channel_id, category in build_channel_category_map().items():
            cursor_id = state.get("last_message_id_by_channel", {}).get(str(channel_id))
            log.info("Channel %s [%s]: cursor=%s", channel_id, category, cursor_id)
        return 0

    try:
        import asyncio
        return asyncio.run(run_backfill(args))
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130
    except Exception as e:
        log.error("Backfill failed: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
