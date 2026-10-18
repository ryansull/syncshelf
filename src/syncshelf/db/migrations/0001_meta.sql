-- 0001_meta (phase 1): SPEC Appendix A.
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,          -- 'last_started_version'
  value TEXT NOT NULL
);
