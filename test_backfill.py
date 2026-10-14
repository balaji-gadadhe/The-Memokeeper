# Headless tests for the backfill frontier + dedup logic.
#
# We test channel_frontier, already_known_message_ids, and the
# backfill_channel_history traversal against a fake channel that yields a
# controlled message sequence. No Discord connection is required.

from __future__ import annotations

import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import AsyncIterator

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
# Publish the sandbox so backfill (imported below) computes the same paths
# instead of defaulting to the real ./data store.
os.environ.setdefault("MEMOKEEPER_DATA_DIR", str(DATA_DIR))

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DISCORD_ALLOWED_USERS", "100000000000000001")
os.environ.setdefault("DISCORD_CHANNEL_JOURNAL", "200000000000000001")
os.environ.setdefault("DISCORD_CHANNEL_TASKS", "200000000000000002")

sys.path.insert(0, str(BASE_DIR))

from backfill import (
    channel_frontier,
    already_known_message_ids,
    record_from_message,
    backfill_channel_history,
)


# ---------------------------------------------------------------------------
# Fake channel + message for headless tests
# ---------------------------------------------------------------------------
class FakeMessage:
    def __init__(self, msg_id, author_id, author_name, content, created_at, channel_name="test"):
        self.id = msg_id
        self.author = FakeAuthor(author_id, author_name)
        self.content = content
        self.created_at = created_at
        self.channel = FakeChannel(channel_name)
        self.attachments = []
        self.reference = None


class FakeAuthor:
    def __init__(self, author_id, name):
        self.id = author_id
        self.name = name


class FakeChannel:
    def __init__(self, name):
        self.name = name
        self.id = 999


class FakeHistoryChannel(FakeChannel):
    """A channel whose .history(oldest_first=, limit=) yields a controlled seq.

    In discord.py, Channel.history() returns an AsyncIterator directly (not an
    awaitable). We mirror that by returning an async iterator.
    """

    def __init__(self, name, messages):
        super().__init__(name)
        self._messages = list(messages)

    def history(self, oldest_first=False, limit=None):
        # discord.py default (oldest_first=False) returns newest-first.
        seq = list(reversed(self._messages)) if not oldest_first else list(self._messages)
        if limit is not None:
            seq = seq[:limit]
        return _FakeHistoryIterator(seq)


class _FakeHistoryIterator:
    def __init__(self, seq):
        self._seq = seq
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._seq):
            raise StopAsyncIteration
        msg = self._seq[self._i]
        self._i += 1
        return msg


def make_msg(msg_id, author_id="100000000000000001", author_name="you", content="x", epoch=None):
    if epoch is None:
        epoch = int(datetime.now(timezone.utc).timestamp())
    created_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return FakeMessage(msg_id, author_id, author_name, content, created_at)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestChannelFrontier:
    def setup_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()
        md = DATA_DIR / "months"
        if md.exists():
            for q in md.glob("*.json"):
                q.unlink()

    def teardown_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()
        md = DATA_DIR / "months"
        if md.exists():
            for q in md.glob("*.json"):
                q.unlink()

    def _state(self, cursor=None, processed_ids=None):
        return {
            "last_message_id_by_channel": cursor or {},
            "last_export_at": None,
            "processed_ids": list(processed_ids or []),
        }

    def _messages(self, rows):
        return [
            {
                "message_id": r["mid"],
                "channel_id": r["ch"],
                "author_id": "100000000000000001",
            }
            for r in rows
        ]

    def test_empty_store_all_none(self):
        f = channel_frontier([], self._state())
        assert f == {}

    def test_derived_from_messages_when_no_cursor(self):
        msgs = self._messages([
            {"mid": "100", "ch": "200000000000000001"},
            {"mid": "200", "ch": "200000000000000001"},
            {"mid": "50", "ch": "200000000000000002"},
        ])
        f = channel_frontier(msgs, self._state())
        assert f == {
            "200000000000000001": "200",
            "200000000000000002": "50",
        }

    def test_cursor_takes_precedence_over_store(self):
        msgs = self._messages([{"mid": "100", "ch": "200000000000000001"}])
        state = self._state(cursor={"200000000000000001": "999"})
        f = channel_frontier(msgs, state)
        assert f["200000000000000001"] == "999"

    def test_cursor_only_channels_included(self):
        state = self._state(cursor={"200000000000000001": "777"})
        f = channel_frontier([], state)
        assert f == {"200000000000000001": "777"}


class TestKnownIds:
    def setup_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()

    def test_combines_store_and_processed_ids(self):
        msgs = [
            {"message_id": "a", "channel_id": "1"},
            {"message_id": "b", "channel_id": "1"},
        ]
        state = {
            "last_message_id_by_channel": {},
            "last_export_at": None,
            "processed_ids": ["c", "d"],
        }
        known = already_known_message_ids(msgs, state)
        assert known == {"a", "b", "c", "d"}


class TestBackfillTraversal:
    def setup_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()
        md = DATA_DIR / "months"
        if md.exists():
            for q in md.glob("*.json"):
                q.unlink()

    def test_newest_first_stops_at_frontier(self):
        # Channel has messages [oldest=100, 200, 300=frontier, 400, 500=newest].
        # Frontier is 300 (already stored). We should fetch 500, 400 and stop at 300.
        msgs = [
            make_msg("100", epoch=1000),
            make_msg("200", epoch=2000),
            make_msg("300", epoch=3000),  # frontier
            make_msg("400", epoch=4000),
            make_msg("500", epoch=5000),
        ]
        fake_chan = FakeHistoryChannel("journal", msgs)
        known = {"300"}
        fetched, newest = asyncio.run(
            backfill_channel_history(fake_chan, "journal", known, frontier_id="300")
        )
        ids = [r["message_id"] for r in fetched]
        assert ids == ["500", "400"]
        assert newest == "500"

    def test_gap_in_history_does_not_stop_early(self):
        # History: [100(gap), 200(frontier), 300, 400, 500].
        # Frontier/known includes 200. Even though 100 is unseen, it's older than
        # the frontier so we stop at 200 and do NOT fetch 100.
        msgs = [
            make_msg("100", epoch=1000),
            make_msg("200", epoch=2000),  # frontier (already known)
            make_msg("300", epoch=3000),
            make_msg("400", epoch=4000),
            make_msg("500", epoch=5000),
        ]
        fake_chan = FakeHistoryChannel("journal", msgs)
        known = {"200"}
        fetched, newest = asyncio.run(
            backfill_channel_history(fake_chan, "journal", known, frontier_id="200")
        )
        ids = [r["message_id"] for r in fetched]
        assert ids == ["500", "400", "300"]
        assert "100" not in ids

    def test_all_messages_new_when_no_frontier(self):
        msgs = [make_msg("100", epoch=1000), make_msg("200", epoch=2000)]
        fake_chan = FakeHistoryChannel("journal", msgs)
        fetched, newest = asyncio.run(
            backfill_channel_history(fake_chan, "journal", known_ids=set(), frontier_id=None)
        )
        assert [r["message_id"] for r in fetched] == ["200", "100"]
        assert newest == "200"

    def test_skips_other_users_but_marks_seen(self):
        msgs = [
            make_msg("100", author_id="other", author_name="other", epoch=1000),
            make_msg("200", epoch=2000),
            make_msg("300", epoch=3000),
        ]
        fake_chan = FakeHistoryChannel("journal", msgs)
        os.environ["DISCORD_ALLOWED_USERS"] = "100000000000000001"
        known: set[str] = set()
        fetched, newest = asyncio.run(
            backfill_channel_history(fake_chan, "journal", known, frontier_id=None)
        )
        ids = [r["message_id"] for r in fetched]
        # Other-user message 100 is walked and skipped (not recorded).
        assert ids == ["300", "200"]
        assert "100" not in ids
        assert "100" in known  # still marked seen so we do not re-walk it
        # newest is the newest *recorded* id (300), since 100 was skipped.
        assert newest == "300"

    def test_limit_stops_after_N_missing(self):
        msgs = [
            make_msg("100", epoch=1000),
            make_msg("200", epoch=2000),
            make_msg("300", epoch=3000),
            make_msg("400", epoch=4000),
            make_msg("500", epoch=5000),
        ]
        fake_chan = FakeHistoryChannel("journal", msgs)
        fetched, newest = asyncio.run(
            backfill_channel_history(fake_chan, "journal", known_ids=set(), frontier_id=None, limit=2)
        )
        assert [r["message_id"] for r in fetched] == ["500", "400"]

    def test_record_shape_matches_bot(self):
        msg = make_msg("abc123", content="hello world", epoch=1700000000)
        rec = record_from_message(msg, "journal")
        assert rec["message_id"] == "abc123"
        assert rec["author_id"] == "100000000000000001"
        assert rec["author_name"] == "you"
        assert rec["content"] == "hello world"
        assert rec["category"] == "journal"
        assert rec["referenced_message_id"] is None
        assert rec["is_edit"] is False
        assert isinstance(rec["timestamp_epoch"], float)


class TestFullBackfillFlowHeadless:
    """Simulate a full backfill run by faking the channel loop + persistence."""

    def setup_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()
        md = DATA_DIR / "months"
        if md.exists():
            for q in md.glob("*.json"):
                q.unlink()

    def teardown_method(self):
        for p in (MESSAGES_PATH, STATE_PATH):
            if p.exists():
                p.unlink()

    def fake_channel(self, name, messages):
        return FakeHistoryChannel(name, messages)

    def fake_state(self, cursor=None, processed_ids=None):
        return {
            "last_message_id_by_channel": cursor or {},
            "last_export_at": None,
            "processed_ids": list(processed_ids or []),
        }

    async def catchup_one(self, channel, category, messages, state, frontier_id, known_ids):
        from backfill import backfill_channel_history
        new_recs, newest = await backfill_channel_history(
            channel, category, known_ids, frontier_id
        )
        messages.extend(new_recs)
        known_ids |= {r["message_id"] for r in new_recs}
        return new_recs, newest, messages, known_ids

    def test_initial_backfill_then_rerun_adds_nothing(self):
        # Two channels. Channel A has history [100, 200, 300], channel B has
        # [10, 20]. First run: nothing stored yet -> fetch all.
        chan_a = self.fake_channel("journal", [
            make_msg("100", epoch=1000),
            make_msg("200", epoch=2000),
            make_msg("300", epoch=3000),
        ])
        chan_b = self.fake_channel("tasks", [
            make_msg("10", epoch=100),
            make_msg("20", epoch=200),
        ])

        messages: list[dict] = []
        state = self.fake_state()
        known: set[str] = set()

        a_new, a_newest, messages, known = asyncio.run(
            self.catchup_one(chan_a, "journal", messages, state, None, known)
        )
        b_new, b_newest, messages, known = asyncio.run(
            self.catchup_one(chan_b, "tasks", messages, state, None, known)
        )

        assert {r["message_id"] for r in a_new} == {"300", "200", "100"}
        assert {r["message_id"] for r in b_new} == {"20", "10"}
        assert len(messages) == 5

        # Simulate persisting state after first run.
        state["last_message_id_by_channel"] = {
            "200000000000000001": a_newest,
            "200000000000000002": b_newest,
        }
        state["processed_ids"] = list(known)

        # Second run: nothing should be added.
        known2: set[str] = set(state["processed_ids"])
        a_new2, _, messages2, _ = asyncio.run(
            self.catchup_one(chan_a, "journal", messages, state, a_newest, known2)
        )
        b_new2, _, messages2, _ = asyncio.run(
            self.catchup_one(chan_b, "tasks", messages, state, b_newest, known2)
        )
        assert a_new2 == []
        assert b_new2 == []
        assert len(messages2) == 5

    def test_backfill_after_gap_catches_only_newer(self):
        # First run stored [100, 200] for channel A; frontier = 200.
        # Later, more history appears [300, 400, 500] (in Discord's view,
        # these are newer messages that arrived in the gap).
        chan_a = self.fake_channel("journal", [
            make_msg("100", epoch=1000),
            make_msg("200", epoch=2000),
            make_msg("300", epoch=3000),
            make_msg("400", epoch=4000),
            make_msg("500", epoch=5000),
        ])

        messages: list[dict] = [
            {"message_id": "100", "channel_id": "200000000000000001"},
            {"message_id": "200", "channel_id": "200000000000000001"},
        ]
        state = self.fake_state(cursor={"200000000000000001": "200"})
        known = already_known_message_ids(messages, state)

        new_recs, newest, messages, known = asyncio.run(
            self.catchup_one(chan_a, "journal", messages, state, "200", known)
        )
        assert {r["message_id"] for r in new_recs} == {"500", "400", "300"}
        assert newest == "500"
        assert len(messages) == 5
