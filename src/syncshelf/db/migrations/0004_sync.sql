-- 0004_sync (phase 4a; ABS reuses it in phase 5): SPEC Appendix A. The identity catalog
-- (shared links, per-profile source rows and the unmatched queue), per-field baselines with
-- their pending changes, the run log, schedules, stored ingest batches and backfill stamps.
CREATE TABLE links (                                -- shared across profiles
  source        TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  source_id     TEXT NOT NULL,                      -- GR edition book_id | ABS libraryItemId
  hc_book_id    INTEGER NOT NULL,
  hc_edition_id INTEGER,                            -- informational, never a key
  format        TEXT CHECK (format IN ('audio','ebook','print')),
  method        TEXT NOT NULL CHECK (method IN
                  ('human','gr_mapping','isbn13','isbn10','asin','fuzzy')),
  state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','stale')),
  linked_at     INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  PRIMARY KEY (source, source_id)
);
CREATE INDEX links_book ON links(hc_book_id);       -- merge rewrites
CREATE TRIGGER links_human_sticky
  BEFORE UPDATE OF method ON links
  WHEN OLD.method = 'human' AND NEW.method <> 'human'
  BEGIN SELECT RAISE(ABORT, 'human link is sticky'); END;

CREATE TABLE source_rows (                          -- per profile; the unmatched queue
  profile         TEXT NOT NULL REFERENCES profiles(name),
  source          TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  source_id       TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('linked','queued','ignored')),
  hc_book_id      INTEGER,                          -- target of this profile's writes
  hc_user_book_id INTEGER,
  queue_reason    TEXT CHECK (queue_reason IN
                    ('no_hit','id_title_mismatch','fuzzy_low','fuzzy_ambiguous',
                     'not_in_library','profile_conflict','stale')),
  queued_at       INTEGER,
  match_input     TEXT,                             -- JSON: title, authors, isbns, asin, gr_book_id, format, year
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,                 -- freezes when gone or id swapped
  gone_at         INTEGER,                          -- set only by a complete enumeration
  backfill_queued INTEGER NOT NULL DEFAULT 0 CHECK (backfill_queued IN (0,1)),  -- queued by the backfill; cleared on resolve
  PRIMARY KEY (profile, source, source_id),
  CHECK ((state = 'linked') = (hc_book_id IS NOT NULL)),
  CHECK ((state = 'queued') = (queue_reason IS NOT NULL AND queued_at IS NOT NULL))
);
CREATE INDEX source_rows_queue ON source_rows(profile, state, queued_at);
CREATE INDEX source_rows_book  ON source_rows(hc_book_id);

CREATE TABLE match_candidates (
  profile       TEXT NOT NULL,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  rank          INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 5),
  hc_book_id    INTEGER NOT NULL,
  hc_edition_id INTEGER,
  method        TEXT NOT NULL,
  title_score   REAL,
  author_score  REAL,
  fetched_at    INTEGER NOT NULL,
  PRIMARY KEY (profile, source, source_id, rank),
  FOREIGN KEY (profile, source, source_id)
    REFERENCES source_rows(profile, source, source_id) ON DELETE CASCADE
);

CREATE TABLE baseline_fields (
  id               INTEGER PRIMARY KEY,             -- the change_id for retry_failed
  profile          TEXT NOT NULL,
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  field            TEXT NOT NULL CHECK (field IN
                     ('status','rating','date_read','review','spoiler','read_count',
                      'abs_state','started_on','finished_on')),
  value            TEXT,                            -- normalised; NULL = observed empty / no baseline
  observed_at      INTEGER,                         -- NULL = empty baseline (first seen after backfill)
  prev_value       TEXT,                            -- value before the last advance (re-read cycle)
  -- pending change: set when a source value differs from value, cleared on commit or supersede
  pending_value    TEXT,
  pending_since    INTEGER,                         -- first-seen date for undated finishes
  attempts         INTEGER NOT NULL DEFAULT 0,
  first_failed_at  INTEGER,
  last_error       TEXT,
  last_error_class TEXT CHECK (last_error_class IN ('transient','permanent','bug')),
  quarantined_at   INTEGER,
  UNIQUE (profile, source, source_id, field),
  FOREIGN KEY (profile, source, source_id)
    REFERENCES source_rows(profile, source, source_id) ON DELETE CASCADE
);
CREATE INDEX baseline_fields_quarantined ON baseline_fields(profile, quarantined_at)
  WHERE quarantined_at IS NOT NULL;
CREATE INDEX baseline_fields_pending ON baseline_fields(profile, pending_since)
  WHERE pending_since IS NOT NULL;

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  profile     TEXT NOT NULL REFERENCES profiles(name),
  source      TEXT NOT NULL CHECK (source IN ('gr_rss','gr_csv','abs','mirror','chaptarr_watch')),
  trigger     TEXT NOT NULL CHECK (trigger IN ('schedule','sync_now','ingest','startup')),
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  state       TEXT NOT NULL CHECK (state IN
                ('running','ok','partial','fetch_failed','deferred','aborted')),
  applied     INTEGER NOT NULL DEFAULT 0,
  failed      INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0,
  first_error TEXT
);
CREATE INDEX runs_recent ON runs(profile, source, started_at);
-- startup: UPDATE runs SET state='aborted', ended_at=now WHERE state='running'
-- purge: runs older than 90 days

CREATE TABLE change_log (
  id              INTEGER PRIMARY KEY,
  run_id          INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  profile         TEXT NOT NULL REFERENCES profiles(name),
  source          TEXT,                             -- NULL for Claude-initiated identity edits
  source_id       TEXT,
  hc_book_id      INTEGER,
  hc_user_book_id INTEGER,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('push','read_insert','read_adopt','link','resolve_match','ignore',
                     'unignore','merge','gone','stale')),
  field           TEXT,
  old_value       TEXT,
  new_value       TEXT,
  trigger         TEXT CHECK (trigger IN ('read_count','reread_cycle','status','isFinished','threshold')),
  date_source     TEXT CHECK (date_source IN
                    ('goodreads_date_read','user_date_added','first_seen','abs_finished_at','abs_last_update')),
  at              INTEGER NOT NULL
);
CREATE INDEX change_log_row  ON change_log(profile, source, source_id);
CREATE INDEX change_log_book ON change_log(hc_book_id);

CREATE TABLE schedules (
  profile              TEXT NOT NULL REFERENCES profiles(name),
  source               TEXT NOT NULL CHECK (source IN ('gr_rss','gr_csv','abs')),
  last_run_at          INTEGER,
  last_success_at      INTEGER,
  last_observed_at     INTEGER,                     -- shown in sync_status
  next_due_at          INTEGER NOT NULL,            -- CSV due date lives here
  last_result          TEXT,                        -- the last run's state
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  backoff_until        INTEGER,
  PRIMARY KEY (profile, source)
);

CREATE TABLE ingest_batches (
  id           INTEGER PRIMARY KEY,
  profile      TEXT NOT NULL REFERENCES profiles(name),
  source       TEXT NOT NULL CHECK (source IN ('gr_csv_auto','gr_csv_ingest')),
  received_at  INTEGER NOT NULL,
  observed_at  INTEGER,                             -- NULL when /ingest sent no generated_at
  stale        INTEGER NOT NULL CHECK (stale IN (0,1)),
  complete     INTEGER NOT NULL CHECK (complete IN (0,1)),
  payload      BLOB NOT NULL,                       -- the CSV as received
  row_count    INTEGER,
  processed_at INTEGER,
  run_id       INTEGER REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX ingest_batches_profile ON ingest_batches(profile, received_at);
-- retention: keep the last 3 per profile

CREATE TABLE backfills (                            -- one row per completed backfill (#32)
  profile     TEXT NOT NULL REFERENCES profiles(name),
  source      TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  done_at     INTEGER NOT NULL,                     -- rewritten by rebaseline()
  run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  rows_linked INTEGER NOT NULL,
  rows_queued INTEGER NOT NULL,
  PRIMARY KEY (profile, source)
);
