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

**Phase 2 (front door and profiles), released as v0.2.0.** `uv run python -m syncshelf`
starts the service on `0.0.0.0:8000` (set by `server.listen`), with an MCP endpoint and a
`/healthz` liveness check. `/mcp` needs a bearer token, which Claude gets by signing in
through SyncShelf's own OAuth server in `oauth` mode, or which a local client sends as the
static dev token in `dev` mode. Its data lives in `/data`, or the existing directory named by
`SYNCSHELF_DATA_DIR`. Phases are listed in SPEC §3.

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
| Tests while iterating | `uv run pytest <path> --no-cov -x` |
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
required. The file is the only source of profiles: at each
start, a profile removed from it is marked removed and its Claude connections are revoked, and
a name added back is restored.

Every `/mcp` request needs a bearer token; without one it gets 401. `server.auth_mode` says
where tokens come from:

- **`oauth`, the production mode.** Claude connects to `server.public_url` (for example
  `https://shelf.example.com`, served through the reverse proxy) and signs in with SyncShelf
  as its OAuth server: discovery, the Claude client documents (CIMD), PKCE, and a consent page
  at `/authorize`. Cloudflare Access does the login there, so an Access app must cover
  `/authorize`, both GET and POST, and pass on its `Cf-Access-Jwt-Assertion` header. The mode
  needs `server.access.team_domain` (just `https://` and the team's host),
  `server.access.aud` (`ACCESS_AUD`, the Access app's AUD tag) and `server.consent_hmac_key`
  (`CONSENT_HMAC_KEY`), and startup names whichever is missing. Each person signs in with the
  email of their profile; any other email is refused. `my_connections` and
  `revoke_connection` list and end a profile's Claude connections. Known issue: browsers may
  send `Origin: null` when **Allow** is pressed, which the consent check refuses (#114).
- **`dev`, for a local instance.** `/mcp` accepts the single static token in
  `server.dev.token` (from `SYNCSHELF_DEV_TOKEN`) as the profile `server.dev.profile`, so
  Claude Code or MCP Inspector can connect by sending `Authorization: Bearer <token>`. Dev
  mode refuses to start unless `server.public_url` is on `localhost` or `127.0.0.1`, serves
  no OAuth routes, and logs a warning at every start. `profiles.example.yaml` shows how to
  switch to it.
- **Absent:** no token is accepted, and `server.public_url` must be on `localhost` or
  `127.0.0.1` (#73).

The full deployment guide is `docs/runbook.md`, which arrives in phase 7. Until then, this
section carries the deployment notes.

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
  health check probes `127.0.0.1:8000`. In oauth mode, attach the container to the reverse
  proxy's network rather than publishing the port, set `server.forwarded_allow_ips` to that
  network, and pass `ACCESS_AUD` and `CONSENT_HMAC_KEY` (`-e`). In dev mode, publish the port
  on loopback only and pass `SYNCSHELF_DEV_TOKEN`, as in this local example:

```sh
docker run -d --name syncshelf \
  -p 127.0.0.1:8000:8000 \
  -e SYNCSHELF_DEV_TOKEN=<token> \
  -v <data-dir>:/data \
  -v <config-dir>/profiles.yaml:/config/profiles.yaml:ro \
  ghcr.io/<owner>/syncshelf:<version>
```

## Licence

[Apache-2.0](LICENSE).
