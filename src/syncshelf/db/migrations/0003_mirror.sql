-- 0003_mirror (phase 3): SPEC Appendix A. A per-profile copy of the Hardcover library, fully
-- replaced on each refresh (§3.3.4). No review text and no private notes are kept.
CREATE TABLE mirror_state (
  profile         TEXT PRIMARY KEY REFERENCES profiles(name),
  as_of           INTEGER,                          -- last successful full fetch
  last_attempt_at INTEGER,
  last_error      TEXT                              -- NULL after a success
);

CREATE TABLE mirror_books (
  profile           TEXT NOT NULL REFERENCES profiles(name),
  hc_book_id        INTEGER NOT NULL,
  title             TEXT NOT NULL,
  contributors_json TEXT NOT NULL,                  -- [{name, role}]
  series_id         INTEGER,
  series_name       TEXT,
  series_position   REAL,
  pages             INTEGER,
  audio_seconds     INTEGER,
  tags_json         TEXT NOT NULL,                  -- top tags by category
  PRIMARY KEY (profile, hc_book_id)
);
CREATE INDEX mirror_books_series ON mirror_books(profile, series_id);

CREATE TABLE mirror_user_books (
  profile         TEXT NOT NULL REFERENCES profiles(name),
  hc_user_book_id INTEGER NOT NULL,
  hc_book_id      INTEGER NOT NULL,
  status_id       INTEGER NOT NULL CHECK (status_id BETWEEN 1 AND 6),
  rating          REAL CHECK (rating IS NULL OR (rating BETWEEN 0.5 AND 5 AND rating * 2 = CAST(rating * 2 AS INTEGER))),
  hc_edition_id   INTEGER,
  date_added      TEXT,
  PRIMARY KEY (profile, hc_user_book_id),
  UNIQUE (profile, hc_book_id)
);
CREATE INDEX mirror_user_books_status ON mirror_user_books(profile, status_id);

CREATE TABLE mirror_reads (
  profile           TEXT NOT NULL REFERENCES profiles(name),
  hc_read_id        INTEGER NOT NULL,
  hc_user_book_id   INTEGER NOT NULL,
  started_on        TEXT,
  finished_on       TEXT,
  hc_edition_id     INTEGER,
  reading_format_id INTEGER,                        -- NULL shown as the 'unknown' bucket
  PRIMARY KEY (profile, hc_read_id)
);
CREATE INDEX mirror_reads_finished ON mirror_reads(profile, finished_on);

CREATE TABLE mirror_list_items (
  profile    TEXT NOT NULL REFERENCES profiles(name),
  hc_list_id INTEGER NOT NULL,
  list_name  TEXT NOT NULL,
  hc_book_id INTEGER NOT NULL,
  PRIMARY KEY (profile, hc_list_id, hc_book_id)
);
