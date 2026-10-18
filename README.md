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

**Phase 1 (skeleton), released as v0.1.0.** The package, CI and test harness exist.
`uv run python -m syncshelf` starts the service on `0.0.0.0:8000` (set by `server.listen`),
with an MCP endpoint and a `/healthz` liveness check. Phase 2 (front door and profiles) is in
progress: `/mcp` now needs a bearer token, and so far only dev mode issues one. Its data lives in
`/data`, or the existing directory named by `SYNCSHELF_DATA_DIR`. Phases are listed in SPEC §3.

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
| Record fixtures (live reads) | `uv run python tests/live/record.py hardcover --token-var HARDCOVER_TOKEN_ALICE` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |

Tests block outbound network access. Live tests read credentials from the git-ignored
repo-root `.env` (or the file in `SYNCSHELF_LIVE_ENV`, for a worktree); see `.env.example`.

To stop real values (your domain, emails, LAN hostnames) ever being committed, list them one per
line in the git-ignored `local/denylist.txt`. The pre-commit hook then refuses any commit that
contains one.

Contributing rules, for people and agents alike, are in [`AGENTS.md`](AGENTS.md) and SPEC §2.10.

## Configuration and deployment

Configuration goes in `profiles.yaml` (git-ignored), modelled on
[`profiles.example.yaml`](profiles.example.yaml). SyncShelf reads it from the path in
`SYNCSHELF_CONFIG`, default `/config/profiles.yaml`. Secrets never go in the file: each is a
`${VAR}` reference to the environment (see `.env.example`) or a `*_file` path. Startup stops on
bad config with a message naming the key. From phase 3, `services.hardcover_url` and each
profile's `hardcover_token` (the Hardcover API token, without its `Bearer ` prefix) are
required. `cadence.hardcover_daily_reserve` (default 1000) is how many of
each profile's 5000 daily Hardcover requests the syncs leave for Claude. The file is the only source of profiles: at each
start, a profile removed from it is marked removed and its Claude connections are revoked, and
a name added back is restored.

Every `/mcp` request needs a bearer token; without one it gets 401. With
`server.auth_mode: dev`, `/mcp` accepts the single static token in `server.dev.token` (from
`SYNCSHELF_DEV_TOKEN`) as the profile `server.dev.profile`, so Claude Code or MCP Inspector
can connect to a local instance by sending `Authorization: Bearer <token>`. Dev mode refuses
to start unless `server.public_url` is on `localhost` or `127.0.0.1`, and logs a warning at
every start. OAuth sign-in for Claude is being built: `server.auth_mode: oauth` needs
`server.access.team_domain`, `server.access.aud` (`ACCESS_AUD`) and `server.consent_hmac_key`
(`CONSENT_HMAC_KEY`), and with `server.auth_mode` absent no
token is accepted. The full deployment guide is `docs/runbook.md`, which arrives in
phase 7. Until then, this section carries the deployment notes.

Each release tag publishes the image `ghcr.io/<owner>/syncshelf:<version>` (and `:latest`)
for amd64 and arm64, built from the [`Dockerfile`](Dockerfile). To run it:

- **`/data`** holds the database, its `pre-migration-NNNN.db` snapshots (taken before each
  upgrade, none on a fresh install) and `goodreads/`. `SYNCSHELF_DATA_DIR` names a different
  directory, which is useful outside a container. The container runs as `10001:10001`, so a
  bind-mounted host directory must exist and be owned by that user
  (`chown 10001:10001 <dir>`), or startup stops naming the unwritable path. A named volume
  gets the right owner from the image. Startup also stops if the database is newer than the
  image (an older release started on it).
- **`/config/profiles.yaml`** is the config. Mount it read-only (`:ro`); SyncShelf never
  writes it. Pass the secrets it references as environment variables or `*_file` paths.
- **Listening.** The service listens on `server.listen`, default `0.0.0.0:8000`, so Traefik or
  a published port (`-p 8000:8000`) reaches it. Keep port 8000 in the container: the image's
  health check probes `127.0.0.1:8000`. Until OAuth sign-in is complete, publish the port on
  loopback only (`-p 127.0.0.1:8000:8000`), as below.

```sh
docker run -d --name syncshelf \
  -p 127.0.0.1:8000:8000 \
  -v <data-dir>:/data \
  -v <config-dir>/profiles.yaml:/config/profiles.yaml:ro \
  ghcr.io/<owner>/syncshelf:<version>
```

## Licence

[Apache-2.0](LICENSE).
