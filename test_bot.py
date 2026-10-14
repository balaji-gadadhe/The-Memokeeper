# Headless integration test for the capture -> persist -> read pipeline.
#
# Reuses the exact same load/save helpers and record shape as bot.py, then
# checks that hermes_read sees new vs already-processed records correctly.

from __future__ import annotations

import json
import sys
import os
import time
import importlib
from pathlib import Path
from datetime import datetime, timezone

import pytest

BASE_DIR = Path(__file__).resolve().parent
# Derive the test store paths exactly like the runtime does. Defaults to an
# ISOLATED data_test/ store so running pytest never deletes the real data/
# files (that used to silently wipe captured messages). A MEMOKEEPER_DATA_DIR
# passed on the pytest command line is still honored.
DATA_DIR = Path(os.environ.get("MEMOKEEPER_DATA_DIR", "").strip() or (BASE_DIR / "data_test")).expanduser()
MESSAGES_PATH = DATA_DIR / "messages.json"
STATE_PATH = DATA_DIR / "state.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Publish the sandbox so hermes_read (imported below) computes the same paths
# instead of defaulting to the real ./data store.
os.environ.setdefault("MEMOKEEPER_DATA_DIR", str(DATA_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DISCORD_ALLOWED_USERS", "100000000000000001")
os.environ.setdefault("DISCORD_CHANNEL_JOURNAL", "100000000000000002")
os.environ.setdefault("DISCORD_CHANNEL_COLLEGE", "100000000000000003")
os.environ.setdefault("DISCORD_CHANNEL_IDEAS_PROJECTS", "100000000000000004")
os.environ.setdefault("DISCORD_CHANNEL_TASKS", "100000000000000005")
os.environ.setdefault("DISCORD_CHANNEL_MISCELLANEOUS", "100000000000000006")

# Ensure we import the project's modules, not stray ones
sys.path.insert(0, str(BASE_DIR))

from hermes_read import (
    load_messages,
    load_state,
    new_records_since,
    bump_export_marker,
    add_processed_ids,
    save_json,
    render_md,
    render_md_by_category,
)


def clean_store():
    for p in (MESSAGES_PATH, STATE_PATH):
        if p.exists():
            p.unlink()
    if DATA_DIR.exists():
        for p in DATA_DIR.iterdir():
            if p.is_file():
                p.unlink()


def make_record(msg_id, category, content, epoch=None, author="you"):
    if epoch is None:
        epoch = datetime.now(timezone.utc).timestamp()
    # Discord.py created_at is timezone-aware and microsecond-precise;
    # store the epoch as a float so it stays comparable to a fractional marker.
    epoch_value: float = float(epoch)
    return {
        "message_id": msg_id,
        "author_id": "100000000000000001",
        "author_name": author,
        "channel_id": 100000000000000002,
        "channel_name": category,
        "category": category,
        "timestamp_utc": datetime.fromtimestamp(epoch_value, tz=timezone.utc).isoformat(),
        "timestamp_epoch": epoch_value,
        "content": content,
        "attachments": [],
        "referenced_message_id": None,
        "is_edit": False,
        "edit_timestamp_utc": None,
    }


class TestPersistenceAndReadPipeline:
    def setup_method(self):
        clean_store()

    def teardown_method(self):
        clean_store()

    def test_empty_store_has_no_new_records(self):
        assert load_messages() == []
        assert new_records_since() == []

    def test_records_persisted_and_loaded(self):
        recs = [make_record("1", "journal", "hello", epoch=1000)]
        save_json(MESSAGES_PATH, recs)
        assert load_messages() == recs

    def test_new_records_since_marker_returns_only_newer(self):
        past = make_record("1", "journal", "old", epoch=1000)
        recent = make_record("2", "tasks", "new", epoch=2000)
        far = make_record("3", "college", "far", epoch=3000)
        save_json(MESSAGES_PATH, [past, recent, far])

        # No marker yet -> everything is new
        assert [r["message_id"] for r in new_records_since()] == ["1", "2", "3"]

        bump_export_marker(epoch=1500)
        assert [r["message_id"] for r in new_records_since()] == ["2", "3"]

        bump_export_marker(epoch=2500)
        assert [r["message_id"] for r in new_records_since()] == ["3"]

        bump_export_marker(epoch=9999)
        assert new_records_since() == []

    def test_new_records_since_explicit_epoch(self):
        save_json(MESSAGES_PATH, [make_record("1", "misc", "a", epoch=100)])
        assert [r["message_id"] for r in new_records_since(epoch=50)] == ["1"]
        assert [r["message_id"] for r in new_records_since(epoch=100)] == []
        assert [r["message_id"] for r in new_records_since(epoch=150)] == []

    def test_bump_marker_persists_across_reload(self):
        bump_export_marker(epoch=4242)
        # reload from disk
        state = load_state()
        assert state["last_export_at"] == 4242
        assert [r["message_id"] for r in new_records_since()] == []

    def test_add_processed_ids_persists(self):
        add_processed_ids(["abc", "def"])
        state = load_state()
        assert state["processed_ids"] == {"abc", "def"}
        add_processed_ids(["def", "ghi"])
        state = load_state()
        assert state["processed_ids"] == {"abc", "def", "ghi"}

    def test_markdown_digest_renders_records(self):
        recs = [make_record("1", "journal", "some thought", epoch=1700000000)]
        md = render_md(recs)
        assert "some thought" in md
        assert "[journal]" in md
        assert "1700000000" not in md  # rendered as human date
        assert "Hermes Discord Log" in md

    def test_markdown_digest_by_category_groups(self):
        recs = [
            make_record("1", "journal", "j", epoch=1000),
            make_record("2", "tasks", "t", epoch=2000),
        ]
        for r in recs:
            save_json(MESSAGES_PATH, load_messages() + [r])
        md = render_md_by_category()
        assert "[journal]" in md
        assert "[tasks]" in md

    def test_full_lifecycle_simulating_bot_and_agent(self):
        # 1) Bot would have persisted these
        recs = [
            make_record("m1", "journal", "morning pages", epoch=1000),
            make_record("m2", "tasks", "buy milk", epoch=2000),
            make_record("m3", "college", "assignment due", epoch=3000),
        ]
        save_json(MESSAGES_PATH, recs)

        # 2) First agent run: marker absent -> sees all
        first = new_records_since()
        assert [r["message_id"] for r in first] == ["m1", "m2", "m3"]

        # 3) Agent finishes, bumps marker to now (fractional, so same-second
        #    arrivals are still distinguishable as new).
        bump_export_marker(epoch=datetime.now(timezone.utc).timestamp())

        # 4) Second run: nothing new (all existing records are before the marker)
        second = new_records_since()
        assert second == []

        # 5) New message arrives (simulate bot appending) with a timestamp
        #    strictly after the marker. Use a fractional epoch so the
        #    comparison stays well-defined.
        new_epoch = datetime.now(timezone.utc).timestamp()
        assert new_epoch > load_state()["last_export_at"]
        new_rec = make_record("m4", "tasks", "call dentist", epoch=new_epoch)
        save_json(MESSAGES_PATH, load_messages() + [new_rec])

        # 6) Agent sees only the new one
        third = new_records_since()
        assert [r["message_id"] for r in third] == ["m4"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
