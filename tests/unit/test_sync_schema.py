"""SQL invariants of ``0004_sync`` (SPEC §2.4, §3.4a, Appendix A): the database itself keeps a
human link human, ties a source row's state to the columns it needs, refuses enum values the
engine never writes, and cleans up a source row's candidates and baselines with it."""

import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from syncshelf.db import connect
from syncshelf.db.migrate import MIGRATIONS_DIR, migrate, snapshot_path

from invariants import assert_accepted, assert_rejected

SPEC = Path(__file__).parents[2] / "docs" / "SPEC.md"
NOW = 1_767_225_600
LINK = (
    "INSERT INTO links (source, source_id, hc_book_id, format, method, linked_at, last_seen_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
ROW = (
    "INSERT INTO source_rows (profile, source, source_id, state, hc_book_id, queue_reason,"
    " queued_at, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
BASELINE = (
    "INSERT INTO baseline_fields (profile, source, source_id, field) VALUES ('alice',"
    " 'goodreads', '1', ?)"
)
RUN = "INSERT INTO runs (profile, source, trigger, started_at, state) VALUES ('alice', ?, ?, ?, ?)"


@pytest.fixture
def sync(schema: sqlite3.Connection) -> sqlite3.Connection:
    assert_accepted(schema, "INSERT INTO profiles (name, created_at) VALUES ('alice', ?)", (NOW,))
    return schema


def linked_row(conn: sqlite3.Connection, source_id: str = "1") -> None:
    assert_accepted(
        conn, ROW, ("alice", "goodreads", source_id, "linked", 10, None, None, NOW, NOW)
    )


def test_the_migration_creates_every_sync_table(schema: sqlite3.Connection) -> None:
    tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert {
        "links",
        "source_rows",
        "match_candidates",
        "baseline_fields",
        "runs",
        "change_log",
        "schedules",
        "ingest_batches",
        "backfills",
    } <= tables


def test_0004_sync_matches_appendix_a() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    block = re.search(r"-- 0004_sync .*?\n(.*?)\n-- 0005_", spec, re.DOTALL)
    assert block is not None
    shipped = (MIGRATIONS_DIR / "0004_sync.sql").read_text(encoding="utf-8")
    assert _normalise(shipped) == _normalise(block.group(1))


def test_a_phase_3_database_is_snapshotted_then_migrated(tmp_path: Path) -> None:
    phase3 = tmp_path / "phase3"
    phase3.mkdir()
    for name in ("0001_meta.sql", "0002_auth.sql", "0003_mirror.sql"):
        shutil.copy(MIGRATIONS_DIR / name, phase3 / name)
    with closing(connect(tmp_path / "db.sqlite")) as conn:
        migrate(conn, snapshot_dir=tmp_path, migrations_dir=phase3)
        result = migrate(conn, snapshot_dir=tmp_path)
        # Every migration shipped since phase 3 applies in one go; 0005 arrived with phase 4b.
        assert (result.from_version, result.applied) == (3, [4, 5])
        assert result.snapshot == snapshot_path(tmp_path, 3)
        assert result.snapshot.exists()


def _normalise(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


class TestLinks:
    def test_a_human_link_cannot_become_automatic(self, sync: sqlite3.Connection) -> None:
        assert_accepted(sync, LINK, ("goodreads", "1", 10, "ebook", "human", NOW, NOW))
        assert_rejected(
            sync,
            "UPDATE links SET method = 'gr_mapping' WHERE source_id = '1'",
            match="human link is sticky",
        )

    def test_a_human_link_can_still_move_to_another_book(self, sync: sqlite3.Connection) -> None:
        # Merges rewrite hc_book_id (§3.4a.4 rule 7); only the method is sticky.
        assert_accepted(sync, LINK, ("goodreads", "1", 10, None, "human", NOW, NOW))
        assert_accepted(sync, "UPDATE links SET hc_book_id = 11 WHERE source_id = '1'")

    def test_an_automatic_link_can_be_replaced(self, sync: sqlite3.Connection) -> None:
        assert_accepted(sync, LINK, ("goodreads", "1", 10, None, "fuzzy", NOW, NOW))
        assert_accepted(sync, "UPDATE links SET method = 'isbn13' WHERE source_id = '1'")
        assert_accepted(sync, "UPDATE links SET method = 'human' WHERE source_id = '1'")

    @pytest.mark.parametrize(
        ("column", "value"),
        [("source", "chaptarr"), ("format", "both"), ("method", "title"), ("state", "gone")],
    )
    def test_enum_values_outside_the_spec_are_refused(
        self, sync: sqlite3.Connection, column: str, value: str
    ) -> None:
        values = {"source": "goodreads", "format": "print", "method": "isbn10"}
        values[column] = value
        sql = (
            "INSERT INTO links (source, source_id, hc_book_id, format, method, state, linked_at,"
            " last_seen_at) VALUES (:source, '1', 10, :format, :method, :state, 0, 0)"
        )
        assert_rejected(sync, sql, {"state": "active", **values}, match="CHECK")

    def test_one_link_per_source_id(self, sync: sqlite3.Connection) -> None:
        assert_accepted(sync, LINK, ("abs", "li_1", 10, "audio", "asin", NOW, NOW))
        assert_rejected(sync, LINK, ("abs", "li_1", 11, "audio", "asin", NOW, NOW), match="UNIQUE")


class TestSourceRows:
    def test_a_linked_row_needs_a_book(self, sync: sqlite3.Connection) -> None:
        bad = ("alice", "goodreads", "1", "linked", None, None, None, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="CHECK")
        linked_row(sync)

    def test_only_a_linked_row_has_a_book(self, sync: sqlite3.Connection) -> None:
        bad = ("alice", "goodreads", "1", "ignored", 10, None, None, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="CHECK")

    @pytest.mark.parametrize(("reason", "queued_at"), [(None, NOW), ("no_hit", None)])
    def test_a_queued_row_needs_a_reason_and_a_time(
        self, sync: sqlite3.Connection, reason: str | None, queued_at: int | None
    ) -> None:
        bad = ("alice", "goodreads", "1", "queued", None, reason, queued_at, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="CHECK")

    def test_only_a_queued_row_has_a_reason(self, sync: sqlite3.Connection) -> None:
        bad = ("alice", "goodreads", "1", "linked", 10, "no_hit", NOW, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="CHECK")

    @pytest.mark.parametrize(
        "reason",
        [
            "no_hit",
            "id_title_mismatch",
            "fuzzy_low",
            "fuzzy_ambiguous",
            "not_in_library",
            "profile_conflict",
            "stale",
            "ambiguous_mapping",
        ],
    )
    def test_every_queue_reason_is_accepted(self, sync: sqlite3.Connection, reason: str) -> None:
        assert_accepted(
            sync, ROW, ("alice", "goodreads", "1", "queued", None, reason, NOW, NOW, NOW)
        )

    def test_an_unknown_queue_reason_is_refused(self, sync: sqlite3.Connection) -> None:
        bad = ("alice", "goodreads", "1", "queued", None, "unsure", NOW, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="CHECK")

    def test_backfill_queued_is_a_boolean(self, sync: sqlite3.Connection) -> None:
        sql = (
            "INSERT INTO source_rows (profile, source, source_id, state, first_seen_at,"
            " last_seen_at, backfill_queued) VALUES ('alice', 'goodreads', '1', 'ignored', 0, 0, 2)"
        )
        assert_rejected(sync, sql, match="CHECK")

    def test_a_row_for_an_unknown_profile_is_refused(self, sync: sqlite3.Connection) -> None:
        bad = ("carol", "goodreads", "1", "linked", 10, None, None, NOW, NOW)
        assert_rejected(sync, ROW, bad, match="FOREIGN KEY")


class TestDependentRows:
    def test_baselines_need_their_source_row(self, sync: sqlite3.Connection) -> None:
        assert_rejected(sync, BASELINE, ("rating",), match="FOREIGN KEY")

    def test_one_baseline_per_field(self, sync: sqlite3.Connection) -> None:
        linked_row(sync)
        assert_accepted(sync, BASELINE, ("rating",))
        assert_rejected(sync, BASELINE, ("rating",), match="UNIQUE")

    def test_an_unknown_baseline_field_is_refused(self, sync: sqlite3.Connection) -> None:
        linked_row(sync)
        assert_rejected(sync, BASELINE, ("shelves",), match="CHECK")

    def test_an_unknown_error_class_is_refused(self, sync: sqlite3.Connection) -> None:
        linked_row(sync)
        sql = (
            "INSERT INTO baseline_fields (profile, source, source_id, field, last_error_class)"
            " VALUES ('alice', 'goodreads', '1', 'status', 'fatal')"
        )
        assert_rejected(sync, sql, match="CHECK")

    def test_a_candidate_rank_is_1_to_5(self, sync: sqlite3.Connection) -> None:
        linked_row(sync)
        sql = (
            "INSERT INTO match_candidates (profile, source, source_id, rank, hc_book_id, method,"
            " fetched_at) VALUES ('alice', 'goodreads', '1', ?, 10, 'fuzzy', 0)"
        )
        assert_rejected(sync, sql, (6,), match="CHECK")
        assert_accepted(sync, sql, (5,))

    def test_deleting_a_source_row_removes_its_candidates_and_baselines(
        self, sync: sqlite3.Connection
    ) -> None:
        linked_row(sync)
        assert_accepted(sync, BASELINE, ("status",))
        assert_accepted(
            sync,
            "INSERT INTO match_candidates (profile, source, source_id, rank, hc_book_id, method,"
            " fetched_at) VALUES ('alice', 'goodreads', '1', 1, 10, 'fuzzy', 0)",
        )
        assert_accepted(sync, "DELETE FROM source_rows WHERE source_id = '1'")
        for table in ("baseline_fields", "match_candidates"):
            assert sync.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


class TestRunsAndLogs:
    @pytest.mark.parametrize(
        ("source", "trigger", "state"),
        [
            ("goodreads", "schedule", "ok"),
            ("gr_rss", "manual", "ok"),
            ("gr_rss", "schedule", "done"),
        ],
    )
    def test_run_enums_are_checked(
        self, sync: sqlite3.Connection, source: str, trigger: str, state: str
    ) -> None:
        assert_rejected(sync, RUN, (source, trigger, NOW, state), match="CHECK")

    @pytest.mark.parametrize("source", ["gr_rss", "gr_csv", "abs", "mirror", "chaptarr_watch"])
    def test_every_run_source_is_accepted(self, sync: sqlite3.Connection, source: str) -> None:
        assert_accepted(sync, RUN, (source, "startup", NOW, "aborted"))

    def test_deleting_a_run_keeps_its_change_log(self, sync: sqlite3.Connection) -> None:
        # Runs are purged after 90 days; their change_log rows are kept (§2.4 Retention).
        assert_accepted(sync, RUN, ("gr_rss", "schedule", NOW, "ok"))
        run_id = sync.execute("SELECT max(id) FROM runs").fetchone()[0]
        assert_accepted(
            sync,
            "INSERT INTO change_log (run_id, profile, kind, at) VALUES (?, 'alice', 'push', ?)",
            (run_id, NOW),
        )
        assert_accepted(sync, "DELETE FROM runs WHERE id = ?", (run_id,))
        assert sync.execute("SELECT run_id FROM change_log").fetchall() == [(None,)]

    @pytest.mark.parametrize(
        ("column", "value"),
        [("kind", "delete"), ("trigger", "rating"), ("date_source", "today")],
    )
    def test_change_log_enums_are_checked(
        self, sync: sqlite3.Connection, column: str, value: str
    ) -> None:
        values = {"kind": "push", "trigger": None, "date_source": None, column: value}
        sql = (
            "INSERT INTO change_log (profile, kind, trigger, date_source, at)"
            " VALUES ('alice', :kind, :trigger, :date_source, 0)"
        )
        assert_rejected(sync, sql, values, match="CHECK")

    def test_a_schedule_is_one_per_profile_and_source(self, sync: sqlite3.Connection) -> None:
        sql = "INSERT INTO schedules (profile, source, next_due_at) VALUES ('alice', ?, 0)"
        assert_accepted(sync, sql, ("gr_rss",))
        assert_rejected(sync, sql, ("gr_rss",), match="UNIQUE")
        assert_rejected(sync, sql, ("gr_csv_auto",), match="CHECK")

    def test_an_ingest_batch_source_is_checked(self, sync: sqlite3.Connection) -> None:
        sql = (
            "INSERT INTO ingest_batches (profile, source, received_at, stale, complete, payload)"
            " VALUES ('alice', ?, 0, 0, 1, x'00')"
        )
        assert_rejected(sync, sql, ("gr_rss",), match="CHECK")
        assert_accepted(sync, sql, ("gr_csv_ingest",))

    def test_one_backfill_per_profile_and_source(self, sync: sqlite3.Connection) -> None:
        sql = (
            "INSERT INTO backfills (profile, source, done_at, rows_linked, rows_queued)"
            " VALUES ('alice', 'goodreads', 0, 1, 2)"
        )
        assert_accepted(sync, sql)
        assert_rejected(sync, sql, match="UNIQUE")
