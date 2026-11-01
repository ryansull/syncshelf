-- 0005_goodreads_session (phase 4b): SPEC Appendix A. One row per profile holding the
-- Goodreads sign-in state machine's current state (§3.4b.2). The session itself is the cookie
-- jar on disk, never here, so a copy of this database carries no live session (§3.4b.3).
CREATE TABLE goodreads_sessions (
  profile              TEXT PRIMARY KEY REFERENCES profiles(name),
  state                TEXT NOT NULL CHECK (state IN
                         ('ok','needs_sign_in','awaiting_approval','awaiting_code',
                          'challenged','bad_credentials','disabled')),
  state_since          INTEGER NOT NULL,
  detail               TEXT,                        -- human-readable reason, no secrets
  last_auto_sign_in_at INTEGER                      -- unattended re-sign-in at most once per 24 h
);
-- the cookie jar is /data/goodreads/<profile>.cookies.json, never SQLite
