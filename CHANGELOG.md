# Changelog

All notable changes to SyncShelf are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow SPEC §2.10
(`v0.<phase release>.<patch>`, with `v1.0.0` at phase 7).

## [Unreleased]

### Changed

- Log lines and stored error strings now show the `server.public_url` host as `public` and
  the `server.access.team_domain` host as `access`, like the service hosts already are (#49).
  A service alias wins where a service shares one of those hosts. In phase 1 the public host
  is `localhost` or `127.0.0.1`, so those names are logged as `public` when they appear as a
  whole hostname; longer names that contain them are left alone.

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
