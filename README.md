# The Memokeeper

A Discord message logger for **asynchronous agent workflows** — capture your
messages whenever you can be online, process them whenever your agent can be.

## The problem it solves

AI agent setups like **OpenClaw / Hermes-style assistants** usually rely on a
Discord gateway that must stay online around the clock: the gateway is listening
live to your channels, and every message is captured as it happens. That works
fine on a dedicated server — but most of us run our agents on a **laptop that
can't stay open 24/7**.

Close the lid, and everything sent while your machine was offline is simply
gone from your agent's memory. To catch a full day of messages, you'd have to
leave the laptop powered and connected all day.

**The Memokeeper changes that.** Discord itself persists your message
history — so instead of keeping a gateway alive constantly, you:

1. **Fire off messages through the day** from anywhere (phone, web, whatever)
   into your tracked channels — whether or not the laptop is on.
2. **When you're ready for your agent to work through everything**, open the
   laptop and run the catch-up script once. It reads Discord's own history API
   and pulls in everything that arrived while the machine was off.
3. **Your agent then processes the full batch** through the reader, marks it
   done, and you're caught up — with zero background processes running while
   you don't need them.

In short: **run one script and get your entire message history**, without ever
needing your agent's gateway to be always-on. Nothing is lost because the
gateway wasn't listening — Discord already remembers; Memokeeper just fetches
what's yours.

The live bot is still there (it captures in real time whenever you *do* have
the laptop running), but it's an optimization, not a requirement.

---

## How it works

```text
                ┌────────────────────┐
                │      Discord       │
                └──────┬──────┬──────┘
            live msgs  │      │ history API
                       ▼      ▼
              ┌─────────┐ ┌───────────┐
              │ bot.py  │ │backfill.py│
              │ (opt.)  │ │ (on boot) │
              └────┬────┘ └─────┬─────┘
                   │            │
                   ▼            ▼
        ┌───────────────────────────────┐
        │            data/              │
        │  messages.json  (all records) │
        │  state.json  (cursor + marker)│
        └───────────────┬───────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │   hermes_read.py    │
              │  new → mark → done  │
              └─────────────────────┘
```

- `bot.py` — **optional live capture.** Listens in your tracked channels and
  stores your messages the moment you send them. If the laptop is open anyway,
  this just keeps `messages.json` continuously fresh.
- `backfill.py` — **the essential piece.** Whenever your laptop boots, it reads
  each tracked channel's Discord history **newest-first** and stops at the
  last message already stored (the **frontier**, from `state.json`). Only the
  gap gets fetched — seconds, not minutes — and it's fully idempotent: run it
  ten times, get zero duplicates.
- `hermes_read.py` — **the reader your agent talks to.** Returns only records
  newer than an export marker, hands them to the agent, and marks the batch
  processed when the agent is done.

Everything dedups on Discord's own `message_id` — never timestamps — so live
capture and history backfill can't collide or double-store the same message.

### Visual trackers in chat

The bot also leaves breadcrumbs right where the messages live:

- 👀 — added the moment a tracked message is recorded in the store
  (live capture or backfill).
- ✅ — added when your agent has processed it. The reader has no Discord
  session, so state.json stays the source of truth and the *bot* mirrors
  `processed_ids` onto the channel next time it's online (on startup).
- 🧭 Caught up — **only in channels that actually caught messages** that run,
  the bot posts a short checkpoint right under your newest notes:
  `🧭 Caught up on the last 3 notes in #journal · 2026-10-14 21:57 IST`
  Times are IST (Asia/Kolkata) and a **fresh** line is sent every run —
  the previous checkpoint is removed first, so a channel always has at
  most one, always at the bottom, always current.
  Idle channels keep no checkpoint; a stale one there gets removed.
  Next run can use the checkpoint as a bonus frontier (everything older
  than it was already walked). The stored cursor in state.json remains
  the primary frontier; the marker is the belt-and-braces fallback that
  survives even a wiped state.json.

### Month rotation

The store is sharded by month so the reader never loads an unbounded file:

```text
data/
  messages.json        current (newest) month only
  months/
    2026-09.json       sealed archive — written once, never touched again
  state.json           cursors + processed ids + export marker (tiny)
```

The active file is promoted automatically the first time a message from a
new month is stored. The reader skips whole month shards that provably
predate the export marker, so reads stay fast no matter how the archive
grows. Dedup is enforced at every layer (`message_id`), so shard boundaries
can't create duplicates.

## What gets stored

`data/messages.json` — the source of truth. Each record looks like:
```json
{
  "message_id": "123456789012345678",
  "author_id": "100000000000000001",
  "author_name": "you",
  "channel_id": 100000000000000002,
  "channel_name": "journal",
  "category": "journal",
  "timestamp_utc": "2026-01-01T12:00:00+00:00",
  "timestamp_epoch": 1735723200,
  "content": "something you typed",
  "attachments": [],
  "referenced_message_id": null,
  "is_edit": false,
  "edit_timestamp_utc": null
}
```

`data/state.json` — processing state: which message ids have been seen, the
per-channel last-seen cursor, and a `last_export_at` epoch marker. The reader
uses the marker to return only *new* records since the last time it ran.

---

## Setup

1. Install the dependency:

```
pip install discord.py
```

2. Copy the example env and fill in real values:

```
cp .env.example .env
```

Edit `.env`:

- `DISCORD_TOKEN` — your bot token.
- `DISCORD_ALLOWED_USERS` — your user id (only your messages are captured).
- `DISCORD_SERVER_ID` — (optional) restrict to one server.
- `MEMOKEEPER_DATA_DIR` — (optional) where to store `messages.json` and
  `state.json`. Defaults to `./data` next to the scripts. Point it at a
  synced/nested folder if you prefer, e.g. `MEMOKEEPER_DATA_DIR=D:\logs\memokeeper`.
- Channel ids — set one `DISCORD_CHANNEL_<NAME>` per channel you want tracked;
  the value becomes the category. Edit the mapping in `bot.py` if you want
  custom names.

Do **not** commit the real `.env`.

3. Run it:

```bash
run.bat            # Windows: catch-up + live bot in one click
```
or

```bash
python backfill.py # catch up offline gap
python bot.py      # live capture (leave running, optional)
```

On startup everything reloads existing state from disk, so a restart won't
duplicate messages. The bot flushes to disk periodically and on command.

## What the bot listens to

Only messages from the user in `DISCORD_ALLOWED_USERS` in the channels listed
in `CHANNEL_CATEGORY_MAP`. Everything else is ignored.

## Owner commands (in any tracked channel, prefix `!`)

```text
!dump            flush the in-memory buffer to disk, report counts by category
!export          force an immediate write of messages.json
!stats           total stored, processed ids, new-since-last-export count
!mark_exported   set the export marker to now ("everything up to now is handled")
```

## How your agent uses it

The reader is `hermes_read.py`. Your agent can call it via CLI or import it as
a module:

```bash
python hermes_read.py new                    # only new records since the marker
python hermes_read.py new --json             # machine-readable output
python hermes_read.py new --mark             # return new + mark batch processed
python hermes_read.py dump                   # everything as JSON
python hermes_read.py md                     # markdown digest (Obsidian-friendly)
python hermes_read.py md --category tasks    # one category only
python hermes_read.py categories             # list categories and counts
python hermes_read.py bump                   # move marker to now manually
python hermes_read.py bump --epoch 1735723200
```

The intended loop:

1. Call `hermes_read.py new` (JSON mode if your agent parses it).
2. Process the records (make notes, update tasks, etc.).
3. Call `hermes_read.py new --mark` when the batch is done, so the next run
   only sees newer messages.

If your agent processes records out of order or in arbitrary batches, it can
import `hermes_read` and call `add_processed_ids(...)` to mark specific ids
handled without moving the time marker.

## Catch-up script (the reason this exists)

The live bot only sees messages while the laptop is running. The catch-up
script fills the offline gap — it reads each tracked channel's Discord history
and appends only messages not already in the store, deduping on `message_id`.

### One-click run (Windows)

Double-click `run.bat` (or run it from a terminal). It does both steps for you:

1. `backfill.py` — catches up anything that arrived while you were offline.
2. `bot.py` — starts the live bot (Ctrl+C to stop).

If you prefer to run the steps yourself:

```bash
python backfill.py
```

What it does:

- For each tracked channel, reads message history **newest-first**.
- Stops at the first already-known message id (the per-channel frontier). The
  frontier comes from `state.json`'s `last_message_id_by_channel` when present,
  otherwise it is derived from the newest id already in `messages.json`. So it
  catches up **only to the last saved message** — it never re-fetches the
  whole history every time.
- Appends only the missing messages, in the same record shape the live bot
  uses.
- Honors `DISCORD_ALLOWED_USERS`: only your messages are recorded, and only
  your messages can stop the walk — bot replies and other people's messages
  are passed over, so they can never mask a newer note below them.
- Leaves `last_export_at` alone by default — the agent decides when a batch is
  processed.

The script is **idempotent**: run it again later and it adds nothing if there's
no new history, and any messages that arrived in the gap are appended.

### Options

```text
--limit N   fetch at most N missing messages per channel (batch large histories)
--mark      after backfill, move the export marker to now so the agent
            treats the whole backfilled batch as processed
--dry-run   load state and report what would be caught up, write nothing
```

### Typical workflow

```bash
# 1) Boot the laptop.
# 2) Catch up everything that arrived while you were offline:
python backfill.py
# 3) (Optional) mark that whole batch as processed for the agent:
python backfill.py --mark
# 4) Agent works through the batch:
python hermes_read.py new        # consume
python hermes_read.py new --mark # done
```

### Why this is safe

- Dedup is by `message_id`, exactly — never by timestamp. If the same message
  appears in history again (e.g. re-scan), it's skipped.
- The frontier is the newest already-stored id per channel, so a gap in
  history (deleted messages, never-run-before, etc.) does not cause early
  stopping or re-fetching of everything.
- Records from the catch-up script use the same shape and paths as the live
  bot, so the reader sees them identically.

## Edits

If you edit a message, the bot updates the existing record in place (same
`message_id`) rather than creating a duplicate. The record gets
`is_edit: true` and an `edit_timestamp_utc`.

## Adding more channels / categories

Set one `DISCORD_CHANNEL_<NAME>` env var per channel you want tracked (the
part after `CHANNEL_` becomes the category). Add the matching entry to
`CHANNEL_CATEGORY_MAP` in `bot.py` and restart. The store location can be
moved anywhere with `MEMOKEEPER_DATA_DIR`.## File layout

```text
bot.py           Discord bot (optional live capture)
backfill.py      Catch-up importer (the essential piece)
hermes_read.py   Reader/marker for your agent
store.py         Shared month-sharded storage layer (used by all three)
run.bat          One-click: catch-up + live bot (Windows)
.env             confidential config (never committed)
.env.example     template with placeholders
data/            default store location (override with MEMOKEEPER_DATA_DIR)
  messages.json    active month's records (auto-promoted at month rollover)
  months/          sealed YYYY-MM.json shards, older months
  state.json       processing state + export marker
test_bot.py      pipeline tests
test_backfill.py backfill frontier/dedup tests
test_store.py    month-sharding/promotion/reader tests
```

## Requirements

```text
Python 3.10+   discord.py   pytest (for the test suite)
```
