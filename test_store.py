# Tests for the shared month-sharded store (store.py) + backfill chat markers.
#
# Uses the same MEMOKEEPER_DATA_DIR sandbox pattern as the other suites:
# pytest never touches the real data/ store.

from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

BASE_DIR = Path(__file__).resolve().parent

os.environ.setdefault(
    "MEMOKEEPER_DATA_DIR", str((BASE_DIR / "data_test").resolve())
)

sys.path.insert(0, str(BASE_DIR))

import store
from backfill import is_marker_message, marker_text


def month_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")


def make_record(msg_id: str, epoch: float, content: str = "x") -> dict:
    return {
        "message_id": msg_id,
        "author_id": "100000000000000001",
        "author_name": "you",
        "channel_id": 100000000000000002,
        "channel_name": "journal",
        "category": "journal",
        "timestamp_utc": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
        "timestamp_epoch": float(epoch),
        "content": content,
        "attachments": [],
        "referenced_message_id": None,
        "is_edit": False,
        "edit_timestamp_utc": None,
    }


class FakeAuthor:
    def __init__(self, author_id):
        self.id = author_id


class FakeMsg:
    def __init__(self, mid, author_id, content):
        self.id = mid
        self.author = FakeAuthor(author_id)
        self.content = content


class TestRecordMonth:
    def test_month_from_epoch(self):
        epoch = datetime(2024, 5, 17, tzinfo=timezone.utc).timestamp()
        assert store.record_month(make_record("a", epoch)) == "2024-05"

    def test_bad_record_falls_back_to_now(self):
        rec = {"message_id": "x"}  # no timestamp
        m = store.record_month(rec)
        assert len(m) == 7 and m[4] == "-"


class TestSaveAll:
    def setup_method(self):
        # Fresh sandbox for every test.
        d = store.resolve_data_dir()
        for p in d.glob("*.json"):
            p.unlink()
        md = store.months_dir()
        if md.exists():
            for p in md.glob("*.json"):
                p.unlink()

    def test_single_month_stays_active(self):
        e1 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp()
        e2 = datetime(2026, 10, 2, 10, tzinfo=timezone.utc).timestamp()
        info = store.save_all([make_record("a", e1), make_record("b", e2)])
        assert info["active_month"] == "2026-10"
        assert info["shards"] == {}  # everything newest-month -> active file
        recs = store.load_messages()
        assert {r["message_id"] for r in recs} == {"a", "b"}

    def test_newer_month_promotes_active_to_shard(self):
        # October records first...
        e_oct = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        store.save_all([make_record("oct1", e_oct)])
        assert store.active_path().exists()
        assert not (store.months_dir() / "2026-10.json").exists()

        # ...then a November record arrives: Oct must be sealed into a shard
        # and the active file must now hold only Nov.
        e_nov = datetime(2026, 11, 1, 9, tzinfo=timezone.utc).timestamp()
        info = store.save_all([make_record("oct1", e_oct), make_record("nov1", e_nov)])
        assert info["active_month"] == "2026-11"
        oct_recs = store.load_shard("2026-10")
        assert [r["message_id"] for r in oct_recs] == ["oct1"]
        act = store.load_active()
        assert [r["message_id"] for r in act] == ["nov1"]
        # No duplicate storage of the active month.
        assert not (store.months_dir() / "2026-11.json").exists()

        # Full history still readable, deduped, ordered oldest-first.
        all_recs = store.load_messages()
        assert [r["message_id"] for r in all_recs] == ["oct1", "nov1"]

    def test_stale_active_month_shard_gets_deleted(self):
        # Simulate a corrupted past state: Nov record living BOTH in a shard
        # and in the active file. save_all must remove the shard copy.
        e_nov = datetime(2026, 11, 5, 8, tzinfo=timezone.utc).timestamp()
        store.months_dir().mkdir(parents=True, exist_ok=True)
        (store.months_dir() / "2026-11.json").write_text('[{"message_id": "nov1", "channel_id": 1, "timestamp_epoch": %f}]' % e_nov)
        store.save_all([make_record("nov1", e_nov), make_record("nov2", e_nov + 1)])
        assert not (store.months_dir() / "2026-11.json").exists()
        ids = {r["message_id"] for r in store.load_messages()}
        assert ids == {"nov1", "nov2"}

    def test_append_records_dedupes(self):
        e = datetime(2026, 10, 3, 9, tzinfo=timezone.utc).timestamp()
        store.append_records([make_record("a", e)])
        store.append_records([make_record("a", e), make_record("b", e + 1)])
        recs = store.load_messages()
        assert sorted(r["message_id"] for r in recs) == ["a", "b"]

    def test_old_month_record_routed_to_shard(self):
        # Backfill catching a straggler from June while the store is current:
        # it must land in a June shard, not the active file.
        e_now = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()
        e_old = datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp()
        store.append_records([make_record("now1", e_now), make_record("old1", e_old)])
        assert [r["message_id"] for r in store.load_shard("2026-06")] == ["old1"]
        assert [r["message_id"] for r in store.load_active()] == ["now1"]
        assert {r["message_id"] for r in store.load_messages()} == {"now1", "old1"}


class TestReaderAcrossShards:
    def setup_method(self):
        d = store.resolve_data_dir()
        for p in d.glob("*.json"):
            p.unlink()
        md = store.months_dir()
        if md.exists():
            for p in md.glob("*.json"):
                p.unlink()

    def test_new_records_since_spans_shards(self):
        t2 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp()
        t3 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
        t4 = datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp()
        store.save_all([
            make_record("sep", t2, "old month"),
            make_record("oct1", t3),
            make_record("oct2", t4),
        ])

        import hermes_read
        # No marker: everything, ascending.
        recs = hermes_read.new_records_since(None)
        assert [r["message_id"] for r in recs] == ["sep", "oct1", "oct2"]
        # Marker within October: only the later October record.
        recs = hermes_read.new_records_since(t3)
        assert [r["message_id"] for r in recs] == ["oct2"]
        # Marker before September: everything.
        recs = hermes_read.new_records_since(0)
        assert len(recs) == 3


class TestMarkerHelpers:
    BOT_ID = 999888777666555444

    def test_is_marker_message(self):
        assert is_marker_message(FakeMsg(1, self.BOT_ID, "🧭 Caught up — gogogo"), self.BOT_ID)
        # Wrong author: not a marker even if content matches.
        assert not is_marker_message(FakeMsg(2, 1, "🧭 Caught up"), self.BOT_ID)
        # Right author, different content: not a marker.
        assert not is_marker_message(FakeMsg(3, self.BOT_ID, "hello"), self.BOT_ID)
        # None author handled.
        assert not is_marker_message(FakeMsg(4, None, "🧭 Caught up"), self.BOT_ID)

    def test_marker_text_contains_status(self):
        text = marker_text("journal", 3)
        assert text.startswith("🧭 Caught up")
        assert "last 3 notes" in text
        assert "#journal" in text
        # Keeps it short: no cursor hashes, no store totals, no boilerplate.
        assert "cursor" not in text
        assert "records" not in text
        assert text.count("\n") == 0

    def test_marker_text_singular_note(self):
        text = marker_text("journal", 1)
        assert "last 1 note" in text
        assert "notes" not in text
