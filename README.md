# Hermes Discord Logger

A small Discord bot that captures the messages **you** send in specified
channels of a server and writes them to a JSON file with full metadata, plus
keeps a processing-state file so your Hermes agent can tell new messages from
ones it already handled.

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
per-channel last-seen cursor, and a `last_export_at` epoch marker. Hermes
uses the marker to ask for only *new* records since the last time it ran.

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

3. Run the bot:

```
python bot.py
```

On startup it reloads existing state from disk, so a restart won't duplicate
messages. It flushes to disk periodically and on command.

## What the bot listens to

Only messages from the user in `DISCORD_ALLOWED_USERS` in the channels listed
in `CHANNEL_CATEGORY_MAP`. Everything else is ignored.

## Owner commands (in any tracked channel, prefix `!`)

- `!dump` — flush the in-memory buffer to disk and report counts by category.
- `!export` — force an immediate write of messages.json.
- `!stats` — show total stored, processed ids, and new-since-last-export count.
- `!mark_exported` — set the `last_export_at` marker to now (tell Hermes
  "everything up to now is handled").

## How Hermes uses it

The reader is `hermes_read.py`. The Hermes agent can call it in a few ways:

- **Get only new records since last marker:**
  `python hermes_read.py new`
  This returns records whose `timestamp_epoch` is greater than the
  `last_export_at` marker in `state.json`. On the first run (no marker),
  it returns everything.

- **Get new records, and mark them processed in one shot:**
  `python hermes_read.py new --mark`
  Use this when Hermes has actually finished working through the batch.

- **Dump everything as JSON:**
  `python hermes_read.py dump`

- **Markdown digest (good for Obsidian-style reading):**
  `python hermes_read.py md`
  Or one category: `python hermes_read.py md --category tasks`

- **List categories and counts:**
  `python hermes_read.py categories`

- **Manually move the marker:**
  `python hermes_read.py bump` (to now)
  `python hermes_read.py bump --epoch 1735723200`

The intended loop for Hermes:

1. Call `hermes_read.py new` (JSON mode if you parse it).
2. Process the returned records (make notes, update tasks, etc.).
3. When the batch is done, call `hermes_read.py new --mark` so the next run
   only sees newer messages. Or call `bump` with an explicit epoch if Hermes
   processes up to a certain point.

If Hermes processes records out of order or in arbitrary batches, it can also
call `add_processed_ids` from its own code (import `hermes_read`) to mark
specific message ids as handled without moving the time marker.

## Edits

If you edit a message, the bot updates the existing record in place (same
`message_id`) rather than creating a duplicate. The record gets
`is_edit: true` and an `edit_timestamp_utc`.

## Adding more channels / categories

Add entries to `CHANNEL_CATEGORY_MAP` in `bot.py` (or add more
`DISCORD_CHANNEL_*` env vars and wire them in). Restart the bot after.

## Catch-up script (for when your laptop isn't running 24/7)

The live bot only captures messages as they happen. If your laptop is off for
a while, messages sent in the meantime are missing from `messages.json`.

Run the catch-up script to fill in the gap. It reads Discord message history
for each tracked channel and appends only the messages that aren't already in
the store, using `message_id` as the dedup key.

### One-click run (Windows)

Double-click `run.bat` (or run it from a terminal). It does both steps for you:

1. `backfill.py` — catches up anything that arrived while you were offline.
2. `bot.py` — starts the live bot (Ctrl+C to stop).

If you prefer to run the steps yourself:

```
python backfill.py
```

What it does:

- For each tracked channel, reads message history **newest-first**.
- Stops at the first already-known message id (the per-channel frontier). The
  frontier is taken from `state.json`'s `last_message_id_by_channel` when
  present, otherwise derived from the newest id already in `messages.json` for
  that channel. This means it catches up **only to the last saved message**, not
  the entire history every time.
- Appends only the missing messages, with the same record shape the live bot
  uses (author_id, author_name, content, attachments, referenced_message_id,
  timestamp_utc/epoch, message_id, category, channel_id).
- Honors `DISCORD_ALLOWED_USERS`: only your messages are recorded; other users'
  messages are walked past and marked seen so they aren't re-scanned next run.
- After a successful backfill it updates `state.json`:
  - adds every newly seen message id to `processed_ids`
  - sets `last_message_id_by_channel` for each channel to the newest fetched id
  - leaves `last_export_at` alone by default (Hermes decides when a batch is
    processed)

The script is **idempotent**: run it again later and it adds nothing if there's
no new history, and any messages that arrived in the gap are appended.

### Options

- `--limit N` — fetch at most N missing messages per channel. Useful to avoid
  hitting very large histories all at once; run it a few times to finish.
- `--mark` — after backfill, also move the `last_export_at` marker to now so
  Hermes treats the whole backfilled batch as processed on its next run.
- `--dry-run` — load state and report what would be caught up without writing
  anything.

### Typical workflow

1. Boot the laptop, run the bot briefly or just run the catch-up script.
2. `python backfill.py` — brings `messages.json` up to date with whatever
   arrived while the bot was offline.
3. If you also want Hermes to treat that whole batch as done immediately:
   `python backfill.py --mark`.
4. Then Hermes runs `python hermes_read.py new` to get anything new since the
   marker, processes it, and calls `python hermes_read.py new --mark` when done.

### Why this is safe

- Dedup is by `message_id`, exactly — never by timestamp. If the same message
  appears in history again (e.g. re-scan), it's skipped.
- The frontier is the newest already-stored id per channel, so a gap in history
  (deleted messages, never-run-before, etc.) does not cause early stopping or
  re-fetching of everything.
- Records from the catch-up script use the same shape and paths as the live bot,
  so `hermes_read.py` sees them identically.

## Edits

If you edit a message, the bot updates the existing record in place (same
`message_id`) rather than creating a duplicate. The record gets
`is_edit: true` and an `edit_timestamp_utc`.

## Adding more channels / categories

Add entries to `CHANNEL_CATEGORY_MAP` in `bot.py` (or add more
`DISCORD_CHANNEL_*` env vars and wire them in). Restart the bot after.

## File layout

```
./bot.py              # Discord bot
./hermes_read.py      # Reader/marker for the Hermes agent
./backfill.py         # Catch-up importer (reads Discord history into messages.json)
./.env                # confidential config (do not commit)
./.env.example        # template
./data/              # default store location (override with MEMOKEEPER_DATA_DIR)
  messages.json       # source of truth: all captured messages
  state.json          # processing state + export marker
```
