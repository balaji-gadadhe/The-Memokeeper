# Discord → Hermes message logger
#
# Captures messages you send in target channels, classifies them by channel,
# and persists them to data/messages.json (full metadata) + data/state.json
# (processing state / last-seen cursors) so Hermes can tell new from old.
#
# Confidential config lives in .env (see .env.example). Do not commit real values.

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# Load .env from project root if present (lightweight, no external dep)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ[_k.strip()] = _v.strip()

import discord
from discord.ext import commands

# ----------------------------------------------------------------------------
# Paths (shared month-sharded store; see store.py)
# ----------------------------------------------------------------------------
import store  # resolves MEMOKEEPER_DATA_DIR (loads .env itself too)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = store.resolve_data_dir()
MESSAGES_PATH = store.active_path()
STATE_PATH = store.state_path()

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hermes-discord")

# ----------------------------------------------------------------------------
# Config (from env)
# ----------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USERS")

# Channel id -> category mapping. Add/edit here as you like.
CHANNEL_CATEGORY_MAP = {
    int(os.environ.get("DISCORD_CHANNEL_JOURNAL", 0)): "journal",
    int(os.environ.get("DISCORD_CHANNEL_COLLEGE", 0)): "college",
    int(os.environ.get("DISCORD_CHANNEL_IDEAS_PROJECTS", 0)): "ideas-projects",
    int(os.environ.get("DISCORD_CHANNEL_TASKS", 0)): "tasks",
    int(os.environ.get("DISCORD_CHANNEL_MISCELLANEOUS", 0)): "miscellaneous",
}

SYS_LOG_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_LOG", 0))
SYS_ALERTS_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ALERTS", 0))

TARGET_SERVER_ID = int(os.environ.get("DISCORD_SERVER_ID", 0))  # optional filter

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Corrupt %s, starting fresh: %s", path.name, e)
        return default


def save_json(path: Path, data):
    store.save_json(path, data)


def slugify_category(cat: str) -> str:
    return cat.replace(" ", "-").lower()


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory state cache we refresh from disk periodically
state = {"last_message_id_by_channel": {}, "last_export_at": None, "processed_ids": set()}
messages = []  # in-memory buffer; persisted periodically + on export


def reload_state():
    global state, messages
    st = store.load_state()
    state = {
        "last_message_id_by_channel": st["last_message_id_by_channel"],
        "last_export_at": st["last_export_at"],
        "processed_ids": st["processed_ids"],
    }
    # Memory holds ALL records across every month shard, so edits and stats
    # keep working on older messages even after rotation splits files.
    messages = store.load_messages()


def persist_now():
    # Sharded write: newest month -> messages.json, older months -> months/.
    store.save_all(messages)
    store.save_state({
        "last_message_id_by_channel": state["last_message_id_by_channel"],
        "last_export_at": state["last_export_at"],
        "processed_ids": state["processed_ids"],
    })


async def send_sys(message: str, channel_id: int):
    if not channel_id:
        return
    try:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(message)
    except Exception as e:
        log.error("Failed to send sys message to %s: %s", channel_id, e)


# ----------------------------------------------------------------------------
# Reaction reconciliation (👀 = recorded, ✅ = Hermes processed it)
# ----------------------------------------------------------------------------
PROCESSED_EMOJI = "✅"


async def reconcile_reactions(scan_limit: int = 100):
    """Catch up on ✅ marks for messages Hermes already processed.

    hermes_read.py has no Discord session, so it cannot react at process
    time. Instead the bot mirrors state.json onto the channel: any tracked
    message whose id is in processed_ids gets ✅ added (👀 remains --
    recorded). Reactions may drift (deleted messages, offline weeks),
    which is fine: state.json is the source of truth, reactions are only
    the visual layer.
    """
    processed = state.get("processed_ids", set())
    if not processed:
        return
    marked = 0
    for ch_id in CHANNEL_CATEGORY_MAP:
        ch = bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            continue
        if TARGET_SERVER_ID and ch.guild and ch.guild.id != TARGET_SERVER_ID:
            continue
        try:
            history = ch.history(limit=scan_limit)
            async for msg in history:
                if msg.author.bot:
                    continue
                if ALLOWED_USER_ID and str(msg.author.id) != ALLOWED_USER_ID.strip():
                    continue
                if str(msg.id) not in processed:
                    continue
                already = any(r.emoji == PROCESSED_EMOJI and r.me for r in msg.reactions)
                if already:
                    continue
                try:
                    await msg.add_reaction(PROCESSED_EMOJI)
                    marked += 1
                except Exception as e:
                    log.warning("Could not react ✅ to %s in #%s: %s", msg.id, ch.name, e)
        except Exception as e:
            log.warning("Reaction sync failed for %s: %s", ch, e)
    if marked:
        log.info("Marked ✅ on %d processed message(s)", marked)


# ----------------------------------------------------------------------------
# Event handlers
# ----------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s (id %s)", bot.user.name, bot.user.id)
    reload_state()
    log.info("Loaded state: %d processed ids, %d messages in store", len(state["processed_ids"]), len(messages))
    await send_sys(f"Reconnected. Tracking {len(CHANNEL_CATEGORY_MAP)} category channels.", SYS_LOG_CHANNEL_ID)
    # ✅ reconciliation: reactions are a visual mirror of state.json's
    # processed_ids, not the source of truth. Anything Hermes already handled
    # gets its ✅ here, including backfilled gaps from previous offline runs.
    await reconcile_reactions()


@bot.event
async def on_message(message: discord.Message):
    # Ignore bot messages and messages from other users (only track you)
    if message.author.bot:
        return
    if ALLOWED_USER_ID and str(message.author.id) != ALLOWED_USER_ID.strip():
        return

    # Optional server filter
    if TARGET_SERVER_ID and message.guild and message.guild.id != TARGET_SERVER_ID:
        return

    channel_id = message.channel.id
    category = CHANNEL_CATEGORY_MAP.get(channel_id)
    if not category:
        return  # not a tracked channel

    # Message identity for dedup / cursor
    msg_id = str(message.id)
    if msg_id in state["processed_ids"]:
        return  # already stored

    created_at = message.created_at
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    record = {
        "message_id": msg_id,
        "author_id": str(message.author.id),
        "author_name": message.author.name,
        "channel_id": channel_id,
        "channel_name": message.channel.name or "",
        "category": category,
        "timestamp_utc": created_at.isoformat(),
        "timestamp_epoch": created_at.timestamp(),
        "content": message.content,
        "attachments": [
            {"filename": a.filename, "url": a.url, "size": a.size}
            for a in message.attachments
        ],
        "referenced_message_id": str(message.reference.message_id) if message.reference else None,
        "is_edit": False,
        "edit_timestamp_utc": None,
    }

    messages.append(record)
    state["processed_ids"].add(msg_id)
    state["last_message_id_by_channel"][str(channel_id)] = msg_id

    # Periodic flush every ~20 saved messages or every 30s (handled below)
    if len(messages) % 20 == 0:
        persist_now()

    # 👀 "noted" reaction: this message was captured in the store.
    try:
        await message.add_reaction("👀")
    except Exception as e:
        log.warning("Could not react 👀 to %s: %s", msg_id, e)

    log.info(
        "Captured [%s] %s #%s: %s",
        category,
        message.author.name,
        message.channel.name or "",
        (message.content or "(attachment/editor)")[:80],
    )

    try:
        await send_sys(
            f"Captured [{category}] {message.author.name}: {message.content[:100]}",
            SYS_LOG_CHANNEL_ID,
        )
    except Exception:
        pass

    return await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    if ALLOWED_USER_ID and str(after.author.id) != ALLOWED_USER_ID.strip():
        return
    channel_id = after.channel.id
    category = CHANNEL_CATEGORY_MAP.get(channel_id)
    if not category:
        return

    msg_id = str(after.id)
    # Update the existing record in place instead of duplicating
    for rec in messages:
        if rec["message_id"] == msg_id and rec["channel_id"] == channel_id:
            rec["content"] = after.content
            rec["attachments"] = [
                {"filename": a.filename, "url": a.url, "size": a.size}
                for a in after.attachments
            ]
            if not rec.get("is_edit"):
                rec["is_edit"] = True
                rec["edit_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
            persist_now()
            log.info("Edited [%s] %s #%s", category, after.author.name, after.channel.name or "")
            await send_sys(f"Edited [{category}] {after.author.name}: {after.content[:100]}", SYS_LOG_CHANNEL_ID)
            break


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
@bot.command(name="dump")
@commands.is_owner()
async def dump(ctx: commands.Context):
    """Flush current buffer to disk and report counts."""
    persist_now()
    n = len(messages)
    cats = {}
    for r in messages:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    await ctx.send(f"Flushed {n} messages.\nBy category: {cats}")
    await send_sys(f"Manual dump: {n} messages stored.", SYS_LOG_CHANNEL_ID)


@bot.command(name="export")
@commands.is_owner()
async def export_file(ctx: commands.Context):
    """Write messages.json and a per-category summary right now."""
    persist_now()
    await ctx.send(f"Exported {len(messages)} messages to {MESSAGES_PATH.name}.")
    await send_sys(f"Export requested by owner.", SYS_LOG_CHANNEL_ID)


@bot.command(name="stats")
@commands.is_owner()
async def stats(ctx: commands.Context):
    n = len(messages)
    new_since_export = sum(
        1 for r in messages if state["last_export_at"] is None or r["timestamp_epoch"] > state["last_export_at"]
    )
    await ctx.send(
        f"Stored messages: {n}\n"
        f"Processed (seen) ids: {len(state['processed_ids'])}\n"
        f"New since last export marker: {new_since_export}\n"
        f"Categories tracked: {list(CHANNEL_CATEGORY_MAP.values())}"
    )


@bot.command(name="mark_exported")
@commands.is_owner()
async def mark_exported(ctx: commands.Context):
    """Update the 'last export' marker so Hermes knows what's new since then."""
    now = int(time.time())
    state["last_export_at"] = now
    persist_now()
    await ctx.send(f"Export marker set to {datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}")
    await send_sys("Export marker updated.", SYS_ALERTS_CHANNEL_ID)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not TOKEN:
        log.error(
            "DISCORD_TOKEN not set in environment. Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

    reload_state()
    try:
        bot.run(TOKEN)
    except discord.LoginFailure as e:
        log.error("Login failed: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("Bot crashed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
