-- 0002_auth (phase 2): SPEC Appendix A.
CREATE TABLE profiles (
  name       TEXT PRIMARY KEY CHECK (name GLOB '[a-z]*' AND length(name) <= 32),
  created_at INTEGER NOT NULL,
  removed_at INTEGER
);

CREATE TABLE oauth_grant (
  id                 INTEGER PRIMARY KEY,
  profile            TEXT NOT NULL REFERENCES profiles(name),
  email              TEXT NOT NULL,                 -- lowercased, from the Access JWT
  client_id          TEXT NOT NULL,                 -- one of the two allowlisted CIMD URLs
  client_label       TEXT NOT NULL,                 -- 'claude.ai' | 'Claude Code'
  resource           TEXT NOT NULL,
  scopes             TEXT NOT NULL,                 -- space-separated
  created_at         INTEGER NOT NULL,
  last_used_at       INTEGER NOT NULL,
  refresh_expires_at INTEGER NOT NULL,              -- sliding 90 days, no absolute cap
  revoked_at         INTEGER,
  revoked_reason     TEXT CHECK (revoked_reason IN
                       ('user','admin','code_replay','refresh_reuse','profile_deleted')),
  CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
);
CREATE INDEX oauth_grant_profile ON oauth_grant(profile);

CREATE TABLE oauth_code (
  code_hash      TEXT PRIMARY KEY,                  -- sha256 hex; raw never stored
  client_id      TEXT NOT NULL,
  redirect_uri   TEXT NOT NULL,
  code_challenge TEXT NOT NULL,                     -- S256 challenge as sent
  resource       TEXT NOT NULL,
  scopes         TEXT NOT NULL,
  profile        TEXT NOT NULL REFERENCES profiles(name),
  email          TEXT NOT NULL,
  created_at     INTEGER NOT NULL,
  expires_at     INTEGER NOT NULL,                  -- +120 s
  used_at        INTEGER,
  grant_id       INTEGER REFERENCES oauth_grant(id) -- set on redemption; replay revokes it
);
CREATE INDEX oauth_code_expires ON oauth_code(expires_at);   -- purge after ~1 day

CREATE TABLE oauth_refresh_token (
  token_hash     TEXT PRIMARY KEY,
  grant_id       INTEGER NOT NULL REFERENCES oauth_grant(id) ON DELETE CASCADE,
  created_at     INTEGER NOT NULL,
  rotated_at     INTEGER,                           -- start of the 60 s grace window
  successor_hash TEXT REFERENCES oauth_refresh_token(token_hash),
  revoked_at     INTEGER
);
CREATE INDEX oauth_refresh_token_grant ON oauth_refresh_token(grant_id);
-- rotated rows kept until the grant's refresh_expires_at, so reuse stays detectable

CREATE TABLE oauth_access_token (
  token_hash TEXT PRIMARY KEY,
  grant_id   INTEGER NOT NULL REFERENCES oauth_grant(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,                      -- +3600 s
  revoked_at INTEGER
);
CREATE INDEX oauth_access_token_grant   ON oauth_access_token(grant_id);
CREATE INDEX oauth_access_token_expires ON oauth_access_token(expires_at);

CREATE TABLE oauth_client_cache (
  client_id     TEXT PRIMARY KEY,
  document_json TEXT NOT NULL CHECK (length(document_json) <= 5120),
  fetched_at    INTEGER NOT NULL,
  fresh_until   INTEGER NOT NULL,                   -- stale-on-error up to 7 days
  etag          TEXT
);
