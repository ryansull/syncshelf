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
| Live write tests (test account only, never in CI) | `SYNCSHELF_LIVE=1 SYNCSHELF_LIVE_WRITE=1 uv run pytest --live tests/live/test_writes_live.py` |
| Record fixtures (live reads) | `uv run python tests/live/record.py hardcover --token-var HARDCOVER_TOKEN_ALICE` |
| Record Goodreads RSS fixtures (live reads) | `uv run python tests/live/record.py goodreads_rss --user-var GOODREADS_USER_ID_ALICE` |
| Record the Goodreads CSV fixture (from a downloaded export) | `uv run python tests/live/record.py goodreads_csv --file local/goodreads_library_export.csv` |
| Calibrate the matcher (live reads; pairs to label in `local/`) | `uv run python tests/live/calibrate_matcher.py --token-var HARDCOVER_TOKEN_ALICE` |
| Attended Goodreads sign-in findings (phase 4b opening test) | `uv run python tests/live/goodreads_signin.py --email-var GOODREADS_EMAIL_ALICE --password-var GOODREADS_PASSWORD_ALICE --ws ws://localhost:3000/` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |
| Check changelog fragments | `uv run python scripts/changelog.py check` |
| Assemble the changelog (release-prep PR only) | `uv run python scripts/changelog.py build --version X.Y.Z --date YYYY-MM-DD --title "Phase N: …"` |

Tests block outbound network access. Live tests read credentials from the git-ignored
repo-root `.env` (or the file in `SYNCSHELF_LIVE_ENV`, for a worktree); see `.env.example`.
Live write tests use only `HARDCOVER_TOKEN_TEST`, a throwaway Hardcover account. They refuse
to run if it's the same account as a profile's token, and take off whatever they shelve.

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
required. `cadence.hardcover_daily_reserve` (default 1000) is how many of each profile's 5000
daily Hardcover requests the syncs leave for Claude, and each profile's `hardcover_privacy`
(public, followers or private, the default) is who sees books SyncShelf adds to its shelves.
From phase 4a, a profile with a `goodreads` section is read from its Goodreads shelf RSS feed:
`goodreads.user_id` (the number in `goodreads.com/user/show/<id>`, quoted) and
`goodreads.shelf_map` (each exclusive shelf's Hardcover status, 1 to 5) are both required in
it, and `rss_key` is needed only if the profile isn't public. A profile without the section has
no Goodreads sync. From phase 4b, `goodreads.email` and `goodreads.password` let SyncShelf sign
in and run the library export itself. They come as a pair, and need the browser sidecar at
`services.browser_ws`, so startup refuses credentials without it. With no `browser_ws`
configured the session export is off for the whole stack, which startup says once, and
Goodreads syncs through RSS and the upload endpoint only. A profile with no password has session
state `disabled`; giving it credentials moves it to `needs_sign_in` at the next start, and
taking them away moves it back. A profile's optional `ingest_secret` lets it upload a Goodreads library
export by hand (below); each profile needs its own, since the secret says whose export it is. The library export runs on its own cadence: `cadence.csv_interval_days` (default 5) after the
last CSV success, in the next `cadence.csv_window` (default `02:00-05:00`) in that profile's own
timezone, so the slowest Goodreads call happens overnight. A dated upload to the ingest endpoint
counts as a success and pushes the date out. Each profile's feed is read every
`cadence.rss_interval` (default `60m`),
the profiles half an interval apart; a failed read backs off from 1 hour to at most 6, and a
restart picks the schedule up where it left off. The file is the only source of profiles: at
each start, a profile removed from it is marked removed and its Claude connections are revoked,
and a name added back is restored.

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
  Claude Code or MCP Inspector can connect by sending `Authorization: Bearer <token>`. The
  token must be at least 32 characters (`openssl rand -hex 32`). Dev mode refuses to start
  unless `server.public_url` is on `localhost` or `127.0.0.1`, serves no OAuth routes, and
  logs a warning at every start. That guards only the configured name, so keep a dev
  instance off the network: publish the port on loopback only (e.g. `127.0.0.1:8000:8000`),
  or set `server.listen` to `127.0.0.1:8000`. `profiles.example.yaml` shows how to switch to
  it.
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
- **`/healthz`** (keep it on the LAN) answers 200 whenever the database does, with
  `"status": "ok"` or `"attention"` and each profile's attention codes (`quarantined`,
  `hardcover_bug`, `source_stale`, and from phase 4b `goodreads_session` when a profile needs
  to sign in to Goodreads again), for Uptime Kuma or Home Assistant to alert on. It's 503
  only when the database fails, so use it for the container's liveness: a Goodreads CAPTCHA
  never restarts the service. SyncShelf sends no notifications itself.
- **`/ingest/goodreads-csv`** (keep it on the LAN too) takes a Goodreads library export
  ("Import and export" on Goodreads) for the profile whose `ingest_secret` it's sent with, and
  answers 202 at once; the profile's next sync applies it. Send `generated_at`, when the export
  was made, or the upload only fills in what SyncShelf hasn't seen yet:

  ```sh
  curl -H "Authorization: Bearer <ingest secret>" \
    -F file=@goodreads_library_export.csv -F generated_at=2026-10-15T08:00:00Z \
    http://<lan-host>:8000/ingest/goodreads-csv
  ```
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
