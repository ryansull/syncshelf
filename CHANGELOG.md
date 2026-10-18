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
- JSON-lines logging on stdout, uvicorn's included, with `profile`/`run_id`/`tool` and other
  context fields, configured hosts rewritten to their alias, and every configured secret
  replaced with `***`, tracebacks included.
