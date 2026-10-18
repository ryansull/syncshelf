# AGENTS.md

Instructions for any coding agent working in this repo. `CLAUDE.md` imports this file, so keep everything here.

## What this is

SyncShelf is a self-hosted Python service for a two-person household. It's a remote MCP server for Claude, plus scheduled one-way syncs that keep Hardcover as the single reading log. Goodreads and Audiobookshelf feed it, and Chaptarr acquires books.

**`docs/SPEC.md` is the source of truth.** It's built in phases (1, 2, 3, 4a, 4b, 5, 6, 7), one phase per build session. Read §0 and §2 (shared rules) before any work, then the phase you're building.

## Rules that are never bent

- **Chaptarr grabs from a private tracker.** Never trigger a search, leave a book wanted, or remove or cancel a queue item without the owner's explicit go-ahead for that specific action. Live Chaptarr tests are GET-only. SyncShelf never cancels a download.
- **Public repo.** No secrets or homelab values (real domains, emails, LAN IPs, hostnames) in code, defaults, docs, fixtures, tests, commit messages or PR text. Use the placeholders in SPEC §2.6. Credentials live in the repo-root `.env` and `local/`, both git-ignored. Refer to them by path, never by value.
- **Don't decide in code.** If the spec is silent, or a live API contradicts it, open an issue, leave that point unbuilt, and name the issue in the PR's Discrepancies section.
- **Hardcover writes in tests** go only to the throwaway test account, with `SYNCSHELF_LIVE_WRITE=1`. Never write to a real profile's library.
- **Never merge your own PR.** The owner reviews and merges.

## Commands

<!-- Phase 1 fills these in once pyproject.toml exists. Keep them current: a command that's changed here and not in README.md (or the other way round) is a bug. -->

| What | Command |
|---|---|
| Install | `uv sync` and `uv run pre-commit install` |
| Lint and format | `uv run ruff check .` and `uv run ruff format --check .` |
| Types | `uv run pyright` |
| Tests (offline, as CI runs them) | `uv run pytest` |
| Live tests (opt-in, never in CI) | `SYNCSHELF_LIVE=1 uv run pytest --live` |
| Record fixtures (live reads) | `uv run python tests/live/record.py hardcover --token-var HARDCOVER_TOKEN_ALICE` |
| All pre-commit hooks (gitleaks, private denylist) | `uv run pre-commit run --all-files` |

## How work lands (SPEC §2.10)

- Branch off `main`: `phase-<n>/<topic>`, `fix/<topic>` or `docs/<topic>`.
- **One thing per PR**, under ~400 changed lines excluding `uv.lock`, fixtures and golden files. A phase is several PRs.
- Fill in every section of `.github/pull_request_template.md`. Write "None" in Discrepancies rather than deleting it.
- Commit subjects are imperative, under ~72 characters, with no type prefixes. Use the noreply author.
- Before opening a PR, run lint, types and tests locally, and check the maintenance table below.
- Tag `v0.<n>.0` only when a phase's "Done when" holds (SPEC §2.10 maps phases to versions).

## Maintenance: what to update when

A PR isn't done until the files its change touches are updated in the **same PR**.

| When you change… | Also update |
|---|---|
| Any user- or operator-visible behaviour | `CHANGELOG.md` under `## [Unreleased]` |
| An MCP tool's behaviour, arguments or result | its docstring (Claude reads it as the tool description), its tests, and SPEC Appendix B if the signature changed (which needs a decision, see below) |
| A config key | `profiles.example.yaml`, `.env.example`, config validation and its test, the README configuration pointer, and a `CHANGELOG.md` line |
| The schema | a **new** migration (never edit a shipped one), its SQL-invariant tests, and a `CHANGELOG.md` line |
| An external API contract the code relies on | re-record the fixture (`tests/live/record.py`), update the fake, and keep the contract test green |
| A decision table (diff, matcher, ABS finish, Chaptarr, attention) | the golden YAML file first, then the code |
| Deployment, ops, health or onboarding | `docs/runbook.md` and `deploy/compose.example.yaml`. Phase 7 creates both (SPEC §2.5), so until then update the README's "Configuration and deployment" section instead |
| Dev setup or a command | `README.md` and the Commands table above |
| PR, branch, version or docs conventions | SPEC §2.10, `.github/pull_request_template.md` and this file, all together |
| A new public module, class or function | its docstring. Comments say *why*, not what |

**`docs/SPEC.md` changes only by decision.** A PR that edits it links the issue that decided the change. Never edit the spec to match code you just wrote. If the code and spec disagree, open an issue.

**Keep this file honest.** If you find an instruction here that's wrong or stale, fix it in the PR that found it and say so in the Summary. If it conflicts with SPEC §2.10, §2.10 wins.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, worked with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels, each named for its role (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one root `GLOSSARY.md` and `docs/adr/`, created when first needed. See `docs/agents/domain.md`.
