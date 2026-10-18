# Changelog

All notable changes to SyncShelf are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow SPEC §2.10
(`v0.<phase release>.<patch>`, with `v1.0.0` at phase 7).

## [Unreleased]

### Added

- The `syncshelf` package (Python 3.14, `src/` layout) with a locked dependency set.
- CI on every push and PR: ruff, pyright, offline tests with outbound sockets blocked and a
  coverage report, and a gitleaks history scan.
- pre-commit hooks: gitleaks, and a private denylist read from the git-ignored
  `local/denylist.txt`.
- Weekly dependabot updates for `uv` and GitHub Actions.
- Config loading from `SYNCSHELF_CONFIG` (default `/config/profiles.yaml`), with `${VAR}` and
  `*_file` secret references and fail-fast validation whose errors name the key, never a
  secret. New config keys: `server.public_url` (required; phase 1 accepts only `localhost` or
  `127.0.0.1`), `server.forwarded_allow_ips` (required), `server.log_level` (default `INFO`),
  `server.auth_mode`, the `cadence` durations (`rss_interval`, `abs_interval`,
  `chaptarr_pending_interval`, `mirror_ttl`), the `services` URLs, and `profiles.<name>.email`.
  See `profiles.example.yaml`.
