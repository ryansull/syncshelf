# SyncShelf

SyncShelf is a self-hosted Python service for a two-person household. It is:

- a **remote MCP server** that Claude connects to over OAuth, with tools that answer
  reading-history questions, edit the reading log, run and inspect the syncs, and request
  books from Chaptarr;
- a set of **scheduled one-way syncs** that keep [Hardcover](https://hardcover.app) as the single
  reading log, fed by Goodreads and Audiobookshelf;
- a **Chaptarr client** that adds and searches one book in one format on request.

The build spec is [`docs/SPEC.md`](docs/SPEC.md).

## Status

**Phase 1 (skeleton), in progress.** The package, CI and test harness exist.
`uv run python -m syncshelf` starts the service on `127.0.0.1:8000`, with an MCP endpoint
that has no tools yet and a `/healthz` liveness check. Its data lives in `/data`. Phases are
listed in SPEC §3.

## Development

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.14 for you.

```sh
uv sync                     # create .venv with the locked dependencies
uv run pre-commit install   # gitleaks and the private denylist on every commit
```

| What | Command |
|---|---|
| Lint and format | `uv run ruff check .` and `uv run ruff format --check .` |
| Types | `uv run pyright` |
| Tests (offline, as CI runs them) | `uv run pytest` |
| Live tests (opt-in, never in CI) | `SYNCSHELF_LIVE=1 uv run pytest --live` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |

Tests block outbound network access. Live tests read credentials from the git-ignored
repo-root `.env`; see `.env.example`.

To stop real values (your domain, emails, LAN hostnames) ever being committed, list them one per
line in the git-ignored `local/denylist.txt`. The pre-commit hook then refuses any commit that
contains one.

Contributing rules, for people and agents alike, are in [`AGENTS.md`](AGENTS.md) and SPEC §2.10.

## Configuration and deployment

Configuration goes in `profiles.yaml` (git-ignored), modelled on
[`profiles.example.yaml`](profiles.example.yaml). SyncShelf reads it from the path in
`SYNCSHELF_CONFIG`, default `/config/profiles.yaml`. Secrets never go in the file: each is a
`${VAR}` reference to the environment (see `.env.example`) or a `*_file` path. Startup stops on
bad config with a message naming the key. Deployment is covered in `docs/runbook.md`, which
arrives in phase 7.

## Licence

[Apache-2.0](LICENSE).
