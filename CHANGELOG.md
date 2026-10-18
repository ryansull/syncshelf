# Changelog

All notable changes to SyncShelf are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow SPEC §2.10
(`v0.<phase release>.<patch>`, with `v1.0.0` at phase 7).

## [Unreleased]

### Added

- New required config keys `services.hardcover_url` and `profiles.<name>.hardcover_token` (a
  secret: the Hardcover API token without its `Bearer ` prefix, which is refused). Startup
  stops on a profile without one, naming the key.
- A Hardcover API client: it pages a profile's whole library past Hardcover's silent
  1000-row cap, counts each call's cost, and reads the rate-limit headers.
- Every Hardcover call goes through a per-profile limiter: bursts of 8, then 1 a second, with
  Claude's calls served ahead of syncs. A 429 pauses that profile's calls until Hardcover's
  `Retry-After` (a `hardcover_paused` warning). New config key
  `cadence.hardcover_daily_reserve` (a whole number, default 1000): below that many requests
  left for the day, syncs wait for the daily reset (a `hardcover_reserve` warning), while
  Claude's calls carry on until none are left.
- Migration `0003_mirror`: the `mirror_state`, `mirror_books`, `mirror_user_books`,
  `mirror_reads` and `mirror_list_items` tables, a per-profile copy of the Hardcover library
  (no review text). An upgrade snapshots the database to `pre-migration-0002.db` first.
- MCP tool `get_my_library`: the caller's shelves as compact rows, filtered by shelf, text,
  author, genre, series, format, minimum rating and finish dates, sorted by date added, last
  finished, rating, title or author, with totals and per-shelf counts. It reads the copy, refreshed when
  older than `cadence.mirror_ttl`; when Hardcover can't be reached it answers from the older
  copy flagged `stale`, and refuses only when there is no copy yet. Each refresh logs
  `mirror_refreshed`, and a failed one `mirror_refresh_failed`.

## [0.2.0] - 2026-10-18

Phase 2: Front door and profiles.

### Added

- New config key `server.listen` (`host:port`, an IPv6 address in brackets), default
  `0.0.0.0:8000`: where the service listens. A value that isn't a host and a port from 1 to
  65535 stops startup with a message naming the key.
- `SYNCSHELF_DATA_DIR` overrides the data directory, default `/data`.
- Migration `0002_auth`: the `profiles`, `oauth_grant`, `oauth_code`, `oauth_refresh_token`,
  `oauth_access_token` and `oauth_client_cache` tables. An upgrade snapshots the database to
  `pre-migration-0001.db` first.
- The `profiles` table follows the config file at every start. A profile removed from the file
  is marked removed and its open grants (Claude connections) are revoked; its data is kept.
  Adding the name back restores the profile, but its old connections stay revoked. A start
  that changes anything logs one `profiles_synced` line.
- New config key `profiles.<name>.permissions`: a list of permissions, of which `chaptarr` is
  the only one. An absent key grants none for now (#71).
- New config key `profiles.<name>.household_visible`: `true` or `false`, default `false`.
- `/mcp` requires a bearer token. A request without an accepted one gets 401 with
  `WWW-Authenticate: Bearer resource_metadata="<public_url>/.well-known/oauth-protected-resource/mcp"`,
  and that protected-resource metadata is served.
- Dev mode (`server.auth_mode: dev`): `/mcp` accepts the static token in the new config key
  `server.dev.token` (a secret, e.g. `${SYNCSHELF_DEV_TOKEN}`) as the profile named by the new
  key `server.dev.profile`, for Claude Code or MCP Inspector against a local instance. Both
  keys are required in dev mode, and the profile must be in `profiles`. Startup refuses dev
  mode unless `server.public_url` is on `localhost` or `127.0.0.1`, and every dev-mode start
  logs a `dev_mode` warning. With `server.auth_mode` absent, no token is accepted (#73).
- Each caller sees only the tools its profile's permissions allow, and a call to any other
  tool is refused as a tool error.
- With `server.auth_mode: oauth`, `/mcp` accepts SyncShelf's own access tokens (`ssat_…`),
  checked against the token store: a token is refused with 401 once it has expired (after an
  hour), when it or its connection is revoked, or when its profile is removed. Only token
  hashes are stored and raw tokens are never logged. A refused token logs a DEBUG
  `token_refused` line with the `grant_id` and reason.
- With `server.auth_mode: oauth`, OAuth discovery works: the authorization-server metadata
  is served at `/.well-known/oauth-authorization-server`, built from `server.public_url`. It
  offers client ID metadata documents and no dynamic client registration, so `/register` is
  404. In dev mode, or with `server.auth_mode` absent, it isn't served.
- The protected-resource metadata advertises `scopes_supported: ["syncshelf", "offline_access"]`
  in every mode, so Claude asks for a refresh token.
- Tools `my_connections` and `revoke_connection`, for every profile: list the caller's own
  Claude connections (which client, when connected and last used, and which one is making
  the call), and revoke one by its `grant_id`, `"all_others"`, or `"all"`, which also ends the
  current session. Only the caller's own connections can be revoked. Each revocation logs an
  INFO `grant_revoked` line with the `grant_id`. In dev mode no connection is current, and
  the dev token keeps working (#97).
- With `server.auth_mode: oauth`, `GET /authorize` checks the client and its redirect. Only
  the two Claude client IDs (the Claude apps and Claude Code) pass, and nothing is fetched
  for any other. Their client documents are fetched from claude.ai (200 and
  `application/json` only, uncompressed, no redirects followed, at most 5 KB), and only valid
  ones are cached in `oauth_client_cache`, fresh for their `max-age` clamped to 5 minutes–24 hours.
  When a fetch fails, the last good copy is used for up to 7 days past its freshness, and a
  `fetch_failed` warning is logged. The `redirect_uri` must be one the document registers
  (a loopback one on any port) and one SyncShelf allows. A request that fails any check gets
  an HTML error page, never a redirect, and logs an `authorize_refused` line with the
  reason.
- With `server.auth_mode: oauth`, a `GET /authorize` whose client and redirect pass then has
  the rest of the request checked (no repeated parameter, `response_type=code`, PKCE `S256`
  with a 43-character challenge, and `resource` equal to `<public_url>/mcp` once its scheme
  and host are lowercased and one trailing slash dropped), then the Cloudflare Access login:
  the `Cf-Access-Jwt-Assertion` JWT must be RS256-signed by a key from
  `<team_domain>/cdn-cgi/access/certs` (cached, and fetched again for an unknown key id),
  with `iss` the team domain, `aud` containing the AUD tag, `exp` and `iat` within 30
  seconds, `type` `app` and an `email`. A refusal redirects back to Claude with `error`,
  `state` and `iss` (`invalid_request`, `unsupported_response_type`, `invalid_target`,
  `access_denied`, or `temporarily_unavailable` when the Access keys can't be fetched) and
  logs an `authorize_refused` line with the reason. An email that matches no profile gets a
  "no SyncShelf profile" page, and nothing is issued. Otherwise the person sees a consent page,
  "Connect claude.ai to SyncShelf as <email>?", with a warning when the code goes to a
  program on their computer (Claude Code).
- With `server.auth_mode: oauth`, Claude can finish signing in. **Allow** (`POST /authorize`)
  checks the signed consent form (an expired or altered one gets an error page), that the
  post came from the consent page (`Origin` is `server.public_url`), and a fresh Access login
  for the same email; a refusal redirects back to Claude with `access_denied`. It then issues
  a single-use code, valid for 120 seconds, and redirects with `code`, `state` and `iss`.
  `POST /token` exchanges the code (form-encoded bodies only, PKCE S256, the same client,
  redirect and resource) for an hour-long access token and, when Claude asked for
  `offline_access`, a refresh token, which creates the connection `my_connections` lists.
  Every `/token` response carries `Cache-Control: no-store` and `Pragma: no-cache`. A code
  used twice is refused and the connection it created is revoked (`code_replay`). New log
  events: `code_issued`, `grant_created`, `token_request_refused`, and `grant_revoked` for a
  replay.
- With `server.auth_mode: oauth`, Claude keeps a connection alive by refreshing
  (`grant_type=refresh_token` at `POST /token`, with the connection's own `client_id` and,
  if sent, `resource`). Each refresh replaces the refresh token, issues a new access token,
  and pushes the connection's 90-day expiry forward, so a connection used at least every 90
  days never expires. A retry with the old refresh token within 60 seconds gets the same new
  pair back, in case the first response was lost; reuse after that revokes the whole
  connection (`refresh_reuse`). A retry after a restart within those 60 seconds is refused
  without revoking, and Claude signs in again. An expired, revoked or unknown refresh token,
  or one whose profile was removed, gets `invalid_grant`. Revoking a connection
  (`revoke_connection`, or a replayed code) also forgets its held pairs. New log events:
  `refresh_rotated`, `refresh_retried`, and `grant_revoked` for a reuse.
- New config keys, required with `server.auth_mode: oauth`: `server.access.aud` (a secret,
  e.g. `${ACCESS_AUD}`: the AUD tag of the Access app guarding `/authorize`) and
  `server.consent_hmac_key` (a secret, e.g. `${CONSENT_HMAC_KEY}`: signs the consent form,
  whose request is valid for 10 minutes). `server.access.team_domain` is required in oauth
  mode too, and must be just `https://` and a host. Startup names a missing key, never a
  value.

### Changed

- With `server.auth_mode: oauth`, `server.public_url` may name any host, such as
  `https://shelf.example.com`, so Claude can connect to a deployed instance. Dev mode still
  refuses any host but `localhost` or `127.0.0.1`, and so does a config without
  `server.auth_mode` (#73). `profiles.example.yaml` now shows oauth mode as the production
  setup, with the dev section commented out for local use.
- The `profiles` section of the config is now required, since the `profiles` table follows
  it: a file without it stops startup (`profiles: required`) rather than revoking every
  connection. `profiles: {}` is still accepted.
- The service listens on `0.0.0.0:8000` by default instead of uvicorn's `127.0.0.1:8000`, so
  a published port (`docker run -p`) or Traefik reaches it. The image's health check still
  probes `127.0.0.1:8000`.
- uvicorn's access log line no longer includes the client address (IP and port), so it reads
  `"GET /healthz HTTP/1.1" 200`. Traefik and Cloudflare already log callers (#46).
- Log lines and stored error strings now show the `server.public_url` host as `public` and
  the `server.access.team_domain` host as `access`, like the service hosts already are (#49).
  A service alias wins where a service shares one of those hosts. In phase 1 the public host
  is `localhost` or `127.0.0.1`, so those names are logged as `public` when they appear as a
  whole hostname; longer names that contain them are left alone.
- A fresh install (a database at version 0) no longer writes a `pre-migration-0000.db`
  snapshot, as there is nothing to roll back to. Upgrades still snapshot first (#48).

### Fixed

- Startup now stops when the database's `user_version` is above the newest migration the
  release ships (an older image started on a newer database), with an error naming both
  versions. Before, it started on a schema it didn't know (#48).

## [0.1.0] - 2026-10-17

Phase 1: Skeleton.

### Added

- The `syncshelf` package (Python 3.14, `src/` layout) with a locked dependency set.
- CI on every push and PR: ruff, pyright, offline tests with outbound sockets blocked and a
  coverage report, and a gitleaks history scan.
- pre-commit hooks: gitleaks, and a private denylist read from the git-ignored
  `local/denylist.txt`. gitleaks (here and in CI) allowlists a SPEC §2.6 placeholder only when
  it is the whole detected secret, so a credential beside or around one is still caught.
- Weekly dependabot updates for `uv` and GitHub Actions.
- Config loading from `SYNCSHELF_CONFIG` (default `/config/profiles.yaml`), with `${VAR}` and
  `*_file` secret references and fail-fast validation whose errors name the key, never a
  secret (a config or secret file that isn't UTF-8 included). New config keys:
  `server.public_url` (required; phase 1 accepts only `localhost` or `127.0.0.1`), `server.forwarded_allow_ips` (required), `server.log_level` (default `INFO`),
  `server.auth_mode`, the `cadence` durations (`rss_interval`, `abs_interval`,
  `chaptarr_pending_interval`, `mirror_ttl`), the `services` URLs, and `profiles.<name>.email`.
  See `profiles.example.yaml`.
- JSON-lines logging on stdout, uvicorn's included, with `profile`/`run_id`/`tool` and other
  context fields, configured hosts rewritten to their alias, and every configured secret
  replaced with `***`, tracebacks included.
- The database layer: one writer thread, per-thread read connections, and a forward-only
  migration runner that writes a `pre-migration-NNNN.db` snapshot before migrating. If that
  snapshot already exists (an earlier start's first pending migration failed), it is kept and
  reused, never overwritten, and the reuse is logged. Ships migration `0001_meta` (the `meta`
  table).
- `python -m syncshelf` serves the app with one uvicorn worker, proxy headers trusted from
  `server.forwarded_allow_ips`, and uvicorn's lines as JSON with query strings dropped from
  the access log. It listens on uvicorn's default `127.0.0.1:8000` for now.
- Startup checks that `/data` and `/data/goodreads/` are writable (creating `goodreads/`),
  opens and migrates `/data/syncshelf.db`, and logs the version, the SQLite version and the
  auth mode.
- An MCP endpoint at `/mcp` with no tools yet: stateless Streamable HTTP that answers 421 to
  any Host other than the public host or loopback. A default port written in
  `server.public_url` (`:80` for http, `:443` for https) is ignored, as clients omit it.
  `/register` is 404.
- `GET /healthz`: 200 `{"status":"ok","version":…}` while the database answers, 503 otherwise.
- A container image (`Dockerfile`): `python:3.14-slim`, running as `10001:10001`, with a
  `/data` volume, config read from `/config/profiles.yaml`, and a `HEALTHCHECK` on
  `/healthz`. CI builds it on every PR, starts it against an empty `/data`, and waits for it
  to report healthy with JSON logs that give the version and SQLite version.
- `release.yml`: a `v*.*.*` tag publishes `ghcr.io/<owner>/syncshelf:<version>` and `:latest`
  for amd64 and arm64. It refuses a tag that doesn't match the package version.
