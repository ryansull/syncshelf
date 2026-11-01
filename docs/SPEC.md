# SyncShelf build spec

**Status:** assembled 2026-10-17 from the planning map [Map: SyncShelf — MCP server + sync service](https://github.com/ryansull/syncshelf/issues/1) by [Assemble the SyncShelf build spec](https://github.com/ryansull/syncshelf/issues/21). Every map decision is folded in, the last being [Goodreads backfill: when it runs, from which feed, and what a field's first observation means](https://github.com/ryansull/syncshelf/issues/32) (the Goodreads backfill, §3.4a.12). Nothing is pending. Every phase can be built in order.

---

## 0. How to use this spec

- **One phase per build session.** Hand a session §1–§2 (shared rules), its phase section in §3, and the appendices. Each phase ships alone and is tested alone. No phase depends on a later one.
- **Decisions live in their tickets.** This spec states each decision and links the ticket that holds the reasoning. If the spec and a ticket disagree, the ticket wins, read with its later amendments. Report the discrepancy back on the map rather than picking quietly.
- **Evidence markers** carried over from research:
  - **verified live**: observed against the real service on 2026-10-17;
  - **source only**: read in the service's source code, not observed;
  - **docs only**: taken from vendor docs.

  A build session re-checks a `source only` or `docs only` contract before relying on it, and records what it saw in the fixtures (§2.9).
- **Assembly choices.** Where the tickets were silent on a small point, this spec picks the boring option and marks it **[assembly choice]**. All of them are listed in [Appendix D](#appendix-d-assembly-choices), and the owner accepted all of them as written.
- **Don't decide in code.** A build session that finds something undecided, or a contract that no longer holds, opens an issue and leaves that point unbuilt rather than settling it in code.
- **How work lands** (branches, PR shape, versions, docs) is in §2.10, and is restated for agents in `AGENTS.md`.
- **Placeholders.** Hostnames, IPs and emails in this document follow the hygiene rules (§2.6): `shelf.example.com`, `*.home.arpa`, `192.0.2.x`, `alice@example.com` / `bob@example.com`. Real values never enter the repo.

---

## 1. What SyncShelf is

One self-hosted Python service for a two-person household. It is:

1. **A remote MCP server** that Claude (claude.ai web, Desktop, mobile, and Claude Code) connects to through OAuth. Its tools answer reading-history questions, edit the reading log, run and inspect the syncs, and request audiobooks and ebooks from Chaptarr.
2. **A set of scheduled one-way syncs** that keep **Hardcover** as the single reading log:
   - **Goodreads → Hardcover.** Kindle auto-shelving and manual entries land in Goodreads. SyncShelf reads them from the shelf RSS feed hourly and from the library CSV export every few days. Goodreads is input only, permanently. Audible does **not** push to Goodreads.
   - **Audiobookshelf (ABS) → Hardcover.** Finished and started audiobooks and ebooks, from each person's ABS progress.
3. **A Chaptarr client** that adds and searches one book in one format on request. **Chaptarr grabs from a private tracker:** a search is a committed download, and SyncShelf never cancels one.

It replaces an n8n → Airtable flow entirely. n8n is retired, and SyncShelf fetches Goodreads data itself.

```
                 ┌──────────────── Claude (web · desktop · mobile · Claude Code)
                 │  MCP over Streamable HTTP, OAuth 2.1 (SyncShelf is its own AS)
                 ▼
 Cloudflare edge → cloudflared → Traefik → ┌───────────── SyncShelf (one process) ─────────────┐
   (Access guards /authorize only)         │ MCP tools · OAuth AS · sync loops · SQLite (WAL)  │
                                           └──┬──────────┬─────────────┬──────────────┬────────┘
             LAN: /ingest, /healthz ──────────┘          │             │              │
                                   Goodreads RSS + CSV ──┘   ABS (LAN) ┘   Chaptarr (LAN)
                                   (read only)               (read only)   (add · monitor · search)
                                                     │
                                                     ▼
                                         Hardcover GraphQL (read + write; source of truth)
```

**Principles that every phase obeys:**

- **Hardcover is the source of truth.** A sync pushes a field only when the **source changed it**, never because Hardcover differs. Hardcover's current value is read only to skip a write that would change nothing and to find existing reads ([Goodreads to Hardcover diff: what 'Goodreads changed it' means](https://github.com/ryansull/syncshelf/issues/13)).
- **Reads are history.** SyncShelf never deletes a read it didn't just create by side effect, never moves a book out of Currently Reading, and never un-finishes anything.
- **Never cancel a download.** No SyncShelf code path removes a Chaptarr queue item, deletes a Chaptarr author or row, or cancels a `pendingauthorimport`.
- **No bot evasion.** SyncShelf never solves, evades or retries past a CAPTCHA or a WAF challenge. It stops and flags.
- **Boring and legible.** Two users and one process. No framework is added where a hundred lines of plain code will do.

### 1.1 Glossary

Use these words, and only these, in code, logs and tool output.

| Term | Meaning |
|---|---|
| **profile** | A SyncShelf user, one per household member. Named by `^[a-z][a-z0-9_]{0,31}$`. |
| **book** | A Hardcover book (the work), keyed by `book_id`. **The canonical entity.** SyncShelf mints no ids of its own. |
| **edition** | A Hardcover edition (format/printing). Informational, never a key. |
| **format** | `audio` \| `ebook` \| `print`. Carried on links and reads. Hardcover's `reading_format_id`: 1 physical, 2 listened, 3 both, 4 ebook. |
| **work** | Not a SyncShelf term. It appears only in external ids (Chaptarr's `gr:` = Goodreads *work* id). |
| **user_book** | Hardcover's per-user shelf entry (status, rating, review). One per (user, book). |
| **read** | One Hardcover `user_book_read`: a single reading, with dates and an edition. |
| **item** | An ABS library item. Each copy is its own item with one format. |
| **source** | `goodreads` or `abs` on identity tables. Observation sources are `gr_rss`, `gr_csv_auto` and `gr_csv_ingest`. |
| **source row** | One profile's entry in a source: a Goodreads shelf row keyed by the Goodreads **edition** `book_id`, or an ABS progress row keyed by `libraryItemId`. |
| **link** | A shared fact for both profiles: `(source, source_id) → book`, with a `method`, an optional edition, and a format. |
| **observation** | One fetch of a source row. It carries only the fields its feed has, plus an `observed_at`. |
| **baseline** | Per source row and field: the last source value SyncShelf accepted, with its `observed_at`. |
| **change** | A field whose observation is newer than its baseline and differs from it after normalisation. A status change plus the read edits that follow it is **one** change. |
| **pending change** | A change that is not quarantined and whose baseline lags the observation. |
| **quarantined** | A change that has failed for ≥ 24 h on ≥ 3 attempts, and is excluded from pending work until released. |
| **complete enumeration** | An observation set that lists the whole library: a CSV whose header validates, or an RSS `#ALL#` pass where every page was usable and paging ended on an empty page. |
| **run** | One execution of one source loop for one profile: fetch the source → diff against baselines → fetch Hardcover (only if there's work) → apply. |
| **adoptable read** | An existing read of the same book whose `finished_at` is within **±14 days** of an incoming finish date, **or** an open read (`started_at` set, `finished_at` null) started on or before it. Undated reads are never adoptable. |
| **mirror** | Per profile, a local SQLite copy of that profile's Hardcover library, without reviews or notes. |
| **grant** | One OAuth consent: a token family for one profile and one client. A "connection" in tool output. |
| **attention item** | `{code, message, action, id?}`: one thing a person must act on. |
| **side** (Chaptarr) | An author's per-format settings (`author.<fmt>Monitored` and friends). |
| **effectively wanted** (Chaptarr) | The row flag **and** the author's side for that format are both on. |
| **dormant row** (Chaptarr) | A row that exists but isn't effectively wanted. |
| **request** (Chaptarr) | Making exactly one row effectively wanted for one book in one format, then searching it once. |

---

## 2. Shared rules (every phase)

### 2.1 Stack and versions

Decided in [Hosting the MCP server, ingest endpoint and scheduler in one process](https://github.com/ryansull/syncshelf/issues/4), and versions observed on PyPI 2026-10-17. Lock exact versions with `uv` (`uv.lock` is committed).

| Piece | Choice | Constraint |
|---|---|---|
| Python | **3.14** (not 3.12, which is security-only; not 3.15.0, too new) | `requires-python = ">=3.14,<3.15"` |
| MCP | official SDK **v2**, `from mcp.server import MCPServer` (FastMCP renamed; `mcp.server.fastmcp` no longer exists) | `mcp>=2.3,<3` |
| ASGI | plain **Starlette** (not FastAPI) | `starlette>=1.8,<2` |
| Server | **uvicorn, exactly one worker**, `--proxy-headers`, `--forwarded-allow-ips` = the Traefik network | `uvicorn>=0.54` |
| HTTP client | **`httpx2`** (the same stack `mcp` already depends on) | `httpx2>=2.13` |
| SQLite | stdlib `sqlite3`, WAL | — |
| JWT | PyJWT with `cryptography` (already an `mcp` dependency) | — |
| Fuzzy matching | `rapidfuzz` (phase 4a) | — |
| YAML | `pyyaml` | — |
| Browser client | `playwright` (phase 4b, imported lazily), pinned in lockstep with the sidecar image tag | — |
| Scheduler | **plain asyncio loops**, not APScheduler | — |
| Tests | `pytest>=9.1` with the **anyio** plugin (not pytest-asyncio), `pytest-socket`, `pytest-cov`, `pytest-timeout` (60 s per test, #124), `ruff`, `pyright` | dev dependencies |

**Not used:** FastAPI, APScheduler, aiosqlite, SQLAlchemy, Alembic, Authlib, `fastmcp` (its `cimd.py`, `ssrf.py` and `redirect_validation.py` are Apache-2.0 *reference code* only; keep their notices if any code is borrowed).

### 2.2 Process architecture

One process serves the MCP endpoint, the OAuth routes, `/ingest`, `/healthz` and every sync loop.

**Routes, in this order on the outer Starlette app.** `Mount("/")` must be last, because routes after it are unreachable.

| Path | Methods | Router | Auth | Phase |
|---|---|---|---|---|
| `/healthz` | GET | **LAN only** | none | 1 (liveness), 4a (status body) |
| `/ingest/goodreads-csv` | POST | **LAN only** | per-profile ingest secret | 4a |
| `/.well-known/oauth-authorization-server` | GET | public | none | 2 |
| `/authorize` | GET, POST | public, **behind Cloudflare Access** | Access JWT | 2 |
| `/token` | POST | public | client gate + grant | 2 |
| `Mount("/", mcp.streamable_http_app(...))`, serving `/mcp` and `/.well-known/oauth-protected-resource/mcp` | — | public | bearer (SDK resource server) | 1 (no tools), 2 (auth) |

There is no `/register` route (DCR isn't offered), so it returns 404.

**Listening and health.** uvicorn binds `server.listen` (§2.3, default `0.0.0.0:8000`). A failing `/healthz` returns 503 `{"status":"unavailable","version":…}` and logs a WARNING line with event `healthz_failed`. Decided in [Phase 1 serving: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/51).

**MCP server settings:**
- `MCPServer("syncshelf", token_verifier=..., auth=AuthSettings(issuer_url=ISSUER, resource_server_url=MCP_URL, validate_token_resource=True))`. Set `validate_token_resource=True` explicitly.
- `mcp.streamable_http_app(stateless_http=True, transport_security=TransportSecuritySettings(allowed_hosts=[<public host>, "localhost:*", "127.0.0.1:*"]))`. Without the public host in the allowlist, every request gets **421**.
- **Stateless** Streamable HTTP, with `json_response` left at its default. Claude can't be assumed to speak the 2026-07-28 revision, and stateful legacy sessions die on restart or after the 30-minute idle expiry. No SyncShelf tool needs the server-to-client channel that stateless mode gives up.
- The MCP request body cap is 4 MiB (413). `/ingest` is a plain Starlette route and isn't subject to it.
- v2.1+ returns a generic error for an unexpected handler exception. Log it server-side (§2.7).

**Lifespan** (a mounted sub-app's own lifespan never runs, so everything goes in the top-level one):

1. Config loads and validates in `__main__`, before the lifespan (§2.3). uvicorn needs `server.forwarded_allow_ips`, `server.listen` and the log level before the app starts, and the MCP mount's host allowlist needs `server.public_url` when the app is built. A bad config fails fast with a line naming the key and exit 1. The lifespan starts at step 2. Decided in [Phase 1 serving: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/51).
2. Check that `/data` and `/data/goodreads/` are writable. `/data/goodreads/` is created when absent. `/data` itself is never created: it's the volume, so it must already exist. Fail with a message naming the path.
3. Open the DB (§2.4): snapshot if migrations are pending, migrate, sync the `profiles` table. When migrations apply, a separate INFO line with event `migrated` gives the from and to versions and the snapshot path.
4. Startup housekeeping:
   - mark any `runs` row still `running` as `aborted`;
   - compare the version against `meta.last_started_version`, and if it differs, release every quarantined change once (4a+). "The version" is the package version. A missing row counts as differing, so 4a's first start releases once. The row is written **after** the release succeeds, so a crash mid-release retries on the next start. Phase 4a starts writing it; phases 1–3 neither read nor write it. Decided in [Lifespan: when meta.last_started_version is written and compared](https://github.com/ryansull/syncshelf/issues/50);
   - log the version, `sqlite3.sqlite_version` and the auth mode as configured (`server.auth_mode`, or null when absent), never secrets;
   - purge expired OAuth rows (§2.4 Retention), after the profile sync (2+).
5. `async with mcp.session_manager.run():`. This is required; without it the first `/mcp` request fails with `Task group is not initialized`.
6. Start the sync loops (4a+), resuming schedules from SQLite (§3.4a.9). Until the 4a scheduler takes it over, a lifespan background task repeats the OAuth purge every 24 hours (2+).
7. On shutdown: cancel the loops and the purge task, close the DB.

**Concurrency primitives**, all in-process (one worker is a hard requirement):

| Primitive | Scope | Held by |
|---|---|---|
| DB writer | global: one connection on a `ThreadPoolExecutor(max_workers=1)` | every write transaction |
| Sync lock | per (profile, source) | the source's loop, `sync_now`, `resolve_match` for that source's row, `retry_failed`'s queued run |
| Hardcover apply lock **[assembly choice]** | per profile | each run's stage 3+4 (Hardcover fetch, then apply), `resolve_match`'s apply, and Claude's direct edits. Two sources of one profile can then never act on each other's stale fetch. See Appendix D. |
| Mirror lock | per profile | a read-triggered mirror refresh, or a sync's full fetch writing the mirror. A reader that finds a refresh in progress waits for it. |
| Hardcover limiter | per profile | every Hardcover call for that profile (§3.3.3) |
| Goodreads single flight | per profile, ≥ 2 s between requests | RSS, export and sign-in traffic |
| CIMD single flight | per `client_id` URL | the CIMD fetcher |

Every MCP tool is `async def`. In SDK v2, plain `def` tools run in a worker thread, where an `asyncio.Lock` can't be used.

**Identity in tools.** A tool reads the caller with `get_access_token()`. Its `.subject` is the **profile name**, and its `.claims` carries `email` and `grant_id`. `Context` carries no identity. A scheduled run has no token: syncs act per profile from stored configuration, never from a request.

### 2.3 Configuration

Decided in [Public-repo hygiene: licence, secret scanning, config conventions](https://github.com/ryansull/syncshelf/issues/5) and [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19).

**Where things live**

| Store | Holds |
|---|---|
| `profiles.yaml` (mounted **read-only**, path from env `SYNCSHELF_CONFIG`, default `/config/profiles.yaml`) | Structure: server settings, service URLs, cadence keys, and per-profile identity, permissions and preferences. Secrets appear only as references. |
| Environment or `*_file` | **Every** secret: Hardcover tokens, ABS tokens, ingest secrets, Goodreads email/password, RSS key, the Chaptarr API key, the consent HMAC key, the Access AUD, the dev token. |
| SQLite (`/data/syncshelf.db` + `-wal`/`-shm`) | Sync state, the identity catalog, the mirror, OAuth grants and token **hashes**, and Chaptarr request records. **No third-party secret is ever written to SQLite.** |
| Filesystem | `/data/goodreads/<profile>.cookies.json` (mode 0600, phase 4b) and `/data/pre-migration-NNNN.db` snapshots. |

**Data directory.** `/data` in this spec is the data directory. Env `SYNCSHELF_DATA_DIR` overrides it, default `/data`. Decided in [Phase 1 serving: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/51).

**Secret references.** Any secret key `k` can be written as `k: ${VAR}`, which **SyncShelf** expands from its own environment at load time (not compose interpolation), or as `k_file: /run/secrets/...`. A missing variable or file fails startup with a message naming the **key**, never the value. A literal secret is never inlined.
- `${VAR}` expands only in secret keys. Non-secret keys are taken literally.
- An empty variable counts as missing.
- A `*_file` secret has its trailing newline stripped.

These three are decided in [Phase 1 config: confirm small calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/49).

**File shape.** One file holds everything [assembly choice]. `profiles.example.yaml` is committed; the real `profiles.yaml` is git-ignored.

```yaml
server:
  public_url: https://shelf.example.com     # ISSUER, a bare origin (no path or trailing slash); MCP_URL = public_url + "/mcp"
  auth_mode: oauth                          # oauth | dev; absent = /mcp accepts no token
  dev:                                      # only read when auth_mode: dev
    token: ${SYNCSHELF_DEV_TOKEN}           # at least 32 characters
    profile: alice
  access:                                   # Cloudflare Access app guarding /authorize
    team_domain: https://example.cloudflareaccess.com
    aud: ${ACCESS_AUD}
  consent_hmac_key: ${CONSENT_HMAC_KEY}     # signs the consent form blob
  forwarded_allow_ips: 192.0.2.0/24         # the Traefik network
  listen: 0.0.0.0:8000                      # host:port uvicorn binds; default shown
  log_level: INFO

services:
  hardcover_url: https://api.hardcover.app/v1/graphql
  abs_url: https://abs.home.arpa            # HTTPS form; http 302s
  chaptarr_url: http://chaptarr.home.arpa
  chaptarr_api_key: ${CHAPTARR_API_KEY}
  browser_ws: ws://browser:3000/            # playwright run-server sidecar (4b); absent = 4b disabled

cadence:                                    # all optional; defaults shown
  rss_interval: 60m
  abs_interval: 15m
  csv_interval_days: 5
  csv_window: "02:00-05:00"                 # in each profile's timezone
  chaptarr_pending_interval: 2m
  mirror_ttl: 10m
  hardcover_daily_reserve: 1000

profiles:
  alice:
    email: alice@example.com                # lowercased; unique across profiles
    timezone: Etc/UTC                       # IANA; default = the container's TZ
    permissions: [chaptarr]                 # the only permission today
    household_visible: false                # opt in to appearing in the other profile's answers
    hardcover_token: ${HARDCOVER_TOKEN_ALICE}   # PAT without "Bearer "; the client adds the prefix
    hardcover_privacy: private              # public | followers | private; applies to inserts only
    ingest_secret: ${INGEST_SECRET_ALICE}
    goodreads:
      user_id: "00000000"                   # numeric, as a string
      rss_key_file: /run/secrets/gr_rss_key_alice   # optional; only if the profile goes private
      email: ${GOODREADS_EMAIL_ALICE}       # 4b; absent → session export disabled for this profile
      password_file: /run/secrets/gr_password_alice
      shelf_map:                            # exclusive shelf → Hardcover status_id
        read: 3
        currently-reading: 2
        to-read: 1
        # did-not-finish: 5                 # custom exclusive shelves, if any
    abs:
      token: ${ABS_TOKEN_ALICE}             # an API key on this person's own non-root ABS user
```

**Validation.**
- A key becomes required when the phase that reads it ships. Sections for features not yet built may be absent. A key isn't validated until the phase that reads it ships.
- Profile names must match `^[a-z][a-z0-9_]{0,31}$`.
- Emails are lowercased and must be unique.
- `shelf_map` values must be in 1–5. Status 6 (Ignored) is never written by sync.
- Durations are written as `<n>m` / `<n>h`.
- `server.forwarded_allow_ips` is required, with no default, and `*` is refused.
- `server.listen` is `host:port`, default `0.0.0.0:8000`.
- Goodreads has no configured URL, so its hosts are fixed in code for host aliasing (§2.7).

The not-yet-validated rule and the last three rules are decided in [Phase 1 config: confirm small calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/49) and, for `server.listen`, [Phase 1 serving: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/51).

From phase 2:
- `server.public_url` is a bare origin in canonical form: `http` or `https` and a host, with an optional port. A lowercase host only; no path (not even `/`), query or fragment; and no explicit default port (`:443` for https, `:80` for http). Anything else is refused, never canonicalised, so the issuer, the PRM `resource` and a browser's `Origin` compare equal as strings. Decided in [Config: may server.public_url carry a path, an uppercase host or a default port?](https://github.com/ryansull/syncshelf/issues/98).
- In oauth mode, an `http` `server.public_url` is refused unless its host is `localhost` or `127.0.0.1` (the MCP SDK's `validate_issuer_url` rule). Decided in [Config: must an oauth-mode public_url be https when its host isn't loopback?](https://github.com/ryansull/syncshelf/issues/122).
- `server.auth_mode` is optional. When it's absent, `/mcp` accepts no token at all (every request gets 401) and the startup log shows null. Decided in [Config: is server.auth_mode required from phase 2, and what does an absent one mean?](https://github.com/ryansull/syncshelf/issues/73).
- In oauth mode, `server.access.team_domain`, `server.access.aud` and `server.consent_hmac_key` are required. `team_domain` is `https://` and a host only, with no path (not even a trailing slash), because it's compared with the JWT's `iss` exactly and the JWKS URL is built under it ([Phase 2 /authorize: confirm calls the spec doesn't make (error codes, JWKS lifetime)](https://github.com/ryansull/syncshelf/issues/110)).
- In dev mode, `server.dev.token` must be at least 32 characters ([Dev mode hardening: a minimum dev-token length, and binding loopback only](https://github.com/ryansull/syncshelf/issues/75)).
- The profile names `me` and `household` are refused, since they're the `profile` argument's keywords (§2.8). Decided in [Profile names "me" and "household" collide with the profile argument's keywords](https://github.com/ryansull/syncshelf/issues/74).
- `profiles.<name>.permissions` is optional, and absent means no permissions. When present it's a list of known permissions (today only `chaptarr`). Decided in [Config: is `profiles.<name>.permissions` required from phase 2, or does absence mean none?](https://github.com/ryansull/syncshelf/issues/71).

**Profiles table sync** (phase 2):
- At startup every profile name is upserted into `profiles`.
- A name that disappeared from the file gets `removed_at`, and its live grants are revoked with `profile_deleted`. Its data is kept and its schedules stop.
- Re-adding the name clears `removed_at`.
- A rename is a documented manual SQL step (phase 7 runbook).

### 2.4 Database conventions

From [Hosting the MCP server, ingest endpoint and scheduler in one process](https://github.com/ryansull/syncshelf/issues/4) and [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19). The full DDL is in [Appendix A](#appendix-a-full-ddl).

- **Connections.**
  - One **writer** connection on a single-thread executor. Each unit of work is one plain sync function and one `BEGIN IMMEDIATE … COMMIT`. Writes run as `await loop.run_in_executor(writer, fn, *args)`.
  - Reads use per-thread read connections via `asyncio.to_thread`.
  - Open with `sqlite3.connect(path, autocommit=True)` and issue `BEGIN IMMEDIATE` yourself.
- **PRAGMAs on every connection:** `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.
- **Values.** Timestamps are epoch-second `INTEGER` (UTC) everywhere; don't use the deprecated `datetime` adapters. Calendar dates (`date_read`, `started_on`, `finished_on`, `date_added`) are `YYYY-MM-DD` `TEXT` in the profile's timezone. Booleans are `INTEGER` 0/1.
- **Single writer is a safety property.** The base image ships SQLite 3.46.1, which is inside the WAL-reset bug range (3.7.0–3.51.2). The bug needs two connections writing or checkpointing at the same instant, which the single writer rules out. Log `sqlite3.sqlite_version` at startup.
- **Per-profile tables** reference `profiles(name)` through a column named `profile`.
- **Invariants enforced in SQL**, not just in code:
  - the trigger that keeps `human` links sticky;
  - `CHECK`s tying `state` to required columns;
  - the partial unique index allowing at most one pending Chaptarr request per (book, format);
  - single-use OAuth codes, by `UPDATE … WHERE used_at IS NULL` plus a row-count check.

  Method *ranking* (only a strictly stronger method replaces a link) stays in code.
- **Enums are complete from the start.** Enum `CHECK`s list every future value from the migration that creates the table, so later phases add rows, never table rebuilds.
- **Migrations.**
  - Forward-only numbered files in `src/syncshelf/db/migrations/NNNN_<name>.sql`, tracked by `PRAGMA user_version`.
  - Before applying any pending migration, take `VACUUM INTO /data/pre-migration-NNNN.db`, where `NNNN` is the database's `user_version` before migrating.
    - The snapshot is written as `pre-migration-NNNN.db.tmp` and renamed once complete. A leftover `.tmp` is deleted and retaken.
    - An existing snapshot is reused, never overwritten. The runner logs `snapshot_reused` and migrates.
    - A database at version 0 (a fresh install) gets no snapshot.
  - Each pending file runs in its own `BEGIN IMMEDIATE` transaction together with its `user_version` bump.
  - A failed migration aborts startup.
  - Startup refuses when `user_version` is above the highest shipped migration, with an error naming both versions.
  - Table rebuilds follow SQLite's 12-step procedure. There are no down-migrations. Handling a rebuild that needs `foreign_keys=OFF` is deferred until a migration first needs one.

  The snapshot, newer-database and rebuild rules are decided in [Migration runner: snapshot naming, reruns, fresh installs and a newer DB](https://github.com/ryansull/syncshelf/issues/48).
- **Migrations map one-to-one onto phases:**

  | Phase | Migration | Tables |
  |---|---|---|
  | 1 | `0001_meta` | `meta` |
  | 2 | `0002_auth` | `profiles`, `oauth_grant`, `oauth_code`, `oauth_refresh_token`, `oauth_access_token`, `oauth_client_cache` |
  | 3 | `0003_mirror` | `mirror_state`, `mirror_books`, `mirror_user_books`, `mirror_reads`, `mirror_list_items` |
  | 4a | `0004_sync` | `links`, `source_rows`, `match_candidates`, `baseline_fields`, `runs`, `change_log`, `schedules`, `ingest_batches`, `backfills` |
  | 4b | `0005_goodreads_session` | `goodreads_sessions` |
  | 5 | none | reuses the 4a tables with `source = 'abs'` |
  | 6 | `0006_chaptarr` | `chaptarr_requests` |
  | 7 | none | none |
- **Retention:**
  - `runs` older than 90 days are purged (their `change_log` rows keep `run_id = NULL`);
  - `change_log` is kept indefinitely;
  - the last 3 `ingest_batches` per profile are kept;
  - OAuth rows (phase 2), purged at startup and then every 24 hours (§2.2 steps 4 and 6):
    - `oauth_code` rows whose `expires_at` is more than 1 day ago;
    - `oauth_access_token` rows past their `expires_at`;
    - `oauth_refresh_token` rows (rotated or not) whose grant's `refresh_expires_at` has passed, or whose grant was revoked more than 90 days ago;
    - `oauth_grant` rows are never deleted;
    - when anything was deleted, one INFO line with event `oauth_purged` gives the counts.

  The OAuth purge is decided in [Retention: what purges expired OAuth codes and tokens, and when?](https://github.com/ryansull/syncshelf/issues/77).
- **Container user:** fixed non-root `USER 10001:10001` (`syncshelf`), with no PUID/PGID. The host does `chown -R 10001:10001 /data`, and `profiles.yaml` must be readable by 10001.
- **Volume:** `/data` is a **bind mount on local disk**, never NFS or SMB (WAL doesn't work over a network filesystem). Back up with `sqlite3 .backup` or `VACUUM INTO`, never a bare copy of a live `.db`.

### 2.5 Repo layout

From [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19), as amended by [Testing strategy: fixtures, golden sets and live tests](https://github.com/ryansull/syncshelf/issues/29) (`fakes/`, `golden/`) and [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30) (`logging.py`, `attention.py`). `[n]` is the phase that introduces the module.

```
LICENSE  pyproject.toml  uv.lock  Dockerfile  .gitattributes  .pre-commit-config.yaml
.gitignore  profiles.example.yaml  .env.example  docs/SPEC.md
README.md  CHANGELOG.md  AGENTS.md  CLAUDE.md                                   [1]
changelog.d/README.md  scripts/changelog.py                    [#139, after 2]
.github/workflows/ci.yml  .github/workflows/release.yml                          [1]
.github/pull_request_template.md  .github/dependabot.yml                        [1]
docs/runbook.md  deploy/compose.example.yaml                                    [7]
src/syncshelf/
  __main__.py            uvicorn entry: one worker, proxy headers               [1]
  app.py                 Starlette app, route order, lifespan                   [1]
  config.py              profiles.yaml + ${VAR}/*_file expansion, validation    [1]
  logging.py             JSON formatter, contextvars, scrub filter, aliasing    [1]
  attention.py           assembles attention items from state                   [4a]
  ingest.py              POST /ingest/goodreads-csv                             [4a]
  db/
    __init__.py          writer executor, read connections, PRAGMAs             [1]
    migrate.py           user_version runner, pre-migration snapshot            [1]
    migrations/NNNN_*.sql                                                       [1..6]
  auth/
    oauth.py             AS metadata, GET/POST /authorize, POST /token          [2]
    tokens.py            grant/token store, TokenVerifier, 60 s grace cache     [2]
    access.py            Cloudflare Access JWT verification                     [2]
    cimd.py              client-document gate, fetch, cache                     [2]
    templates/consent.html                                                      [2]
  clients/               one thin HTTP client per service; no sync logic
    hardcover.py [3]   goodreads_rss.py [4a]   goodreads_csv.py [4a]
    goodreads_export.py [4b]   abs.py [5]   chaptarr.py [6]
  sync/
    limiter.py           per-profile Hardcover token bucket + daily reserve     [3]
    mirror.py            full fetch, replace, TTL, write-through                [3]
    apply.py             "make Hardcover match" write procedure (§3.3.6)        [3]  [assembly choice]
    scheduler.py         per-source loops, locks, jitter, schedules table       [4a]
    runs.py              run lifecycle, failure classes, quarantine             [4a]
    identity.py          links, merges, stale, backfill map-only mode           [4a]
    matcher.py           chain, rapidfuzz scoring, candidates                   [4a]
    observations.py      the observation contract                               [4a]
    goodreads.py         Goodreads diff engine                                  [4a]
    goodreads_session.py sign-in state machine, jar, sidecar                    [4b]
    abs.py               ABS diff engine                                        [5]
    chaptarr_watch.py    pending watcher                                        [6]
  tools/
    __init__.py          registry, permission filter, profile resolution        [2]
    connections.py       my_connections, revoke_connection                      [2]
    library.py           the six read tools                                     [3]
    writes.py            update_book, correct_read, remove_book                 [3]
    confirm.py           preview + confirm-token helper                         [3]
    sync.py              sync_now, sync_status, retry_failed [4a]; dismiss_attention [6]
    matching.py          unmatched_books, resolve_match, rebaseline             [4a]
    goodreads.py         goodreads_sign_in, goodreads_verify                    [4b]
    chaptarr.py          chaptarr_request, library_status, chaptarr_queue       [6]
tests/
  conftest.py      clock, temp SQLite, fakes wiring, authed MCP client, live gating
  fakes/           hardcover.py  chaptarr.py  abs.py  goodreads.py
  fixtures/<source>/   recorded + scrubbed responses, each with recorded_at and server version
  golden/          diff.yaml  abs_finish.yaml  chaptarr_request.yaml  matcher.yaml  attention.yaml
  unit/
  live/            gated tests, plus record.py and calibrate_matcher.py
```

### 2.6 Public-repo hygiene

From [Public-repo hygiene: licence, secret scanning, config conventions](https://github.com/ryansull/syncshelf/issues/5). The repo is public from day one.

- **Licence:** Apache-2.0.
- **No secrets and no homelab values** (real domains, emails, LAN IPs, hostnames) anywhere in code, defaults, docs, fixtures or commit messages. Use these placeholders:

  | Kind | Placeholder |
  |---|---|
  | Public hostnames | `shelf.example.com` (RFC 2606) |
  | LAN hostnames | `*.home.arpa` (RFC 8375), e.g. `chaptarr.home.arpa` |
  | IPs | `192.0.2.x` (RFC 5737) |
  | Emails | `alice@example.com`, `bob@example.com` |
- **pre-commit** (`.pre-commit-config.yaml`):
  - **gitleaks** with default rules, plus an allowlist for the placeholders above;
  - a **private denylist hook** that reads the git-ignored `local/denylist.txt` (one real value per line) and blocks any commit containing one. It passes silently when the file is absent (contributors, CI).
- **GitHub secret scanning and push protection** stay on.
- **`.gitattributes`:** `* text=auto eol=lf`.
- **Commits** use the GitHub noreply author address.
- **Git-ignored:** `profiles.yaml`, `.env*` (except `.env.example`), `local/`, and any compose override.
- **Real deployment values** live in a Portainer stack (the domain, Traefik labels, LAN URLs, and secrets as stack env). `profiles.yaml` lives on the host volume.
- **Research findings** go on their ticket as a collapsed comment, never on a branch.

### 2.7 Logging and redaction

From [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30). **The goal:** any log line, and any error string in `sync_status`, can be pasted into a public GitHub issue.

- **Format.** stdlib `logging` with a ~40-line JSON-lines formatter on stdout, and no new dependency. Uvicorn's loggers use it too. Rotation is Docker's `json-file` `max-size`/`max-file` (runbook).
- **Every line** carries `ts` (ISO-8601 UTC), `level`, `logger`, `event` (a stable snake_case name) and `msg`. When set, `contextvars` add `profile`, `source`, `run_id`, `change_id`, `tool` and `request_id`.
- **Default `event`.** A third-party logger's line without an explicit `event` gets the logger name in snake_case (`uvicorn.access` → `uvicorn_access`). Every SyncShelf (`syncshelf.*`) logger call passes an explicit `event`, and a test fails on one that doesn't. Decided in [Logging: what 'event' do lines without one carry?](https://github.com/ryansull/syncshelf/issues/47).
- **Level** from `server.log_level` (default `INFO`).

| Level | What |
|---|---|
| DEBUG | every outbound HTTP call: `service` alias, method, path without query, status, `ms` |
| INFO | run start and end (state, counts, duration; an idle run emits only the end line); one line per applied change (`change_id`, `hc_book_id`, `field`, `trigger`, `date_source`); each MCP tool call (`tool`, `ms`, outcome); auth events (`grant_id`, `client_label`, event); `/ingest` received; startup and migration |
| WARNING | a failed change attempt (`error_class`, scrubbed error), a transient retry, `fetch_failed`, a 429 pause, entering the daily reserve, a session state change, a Chaptarr pending timeout |
| ERROR | quarantine, a `bug`-class error, an unhandled exception (scrubbed traceback), a failed migration |

**Redaction, in three layers:**

1. **Allowlist.** Lines carry ids (`hc_book_id`, `source_id`, `grant_id`), never:
   - titles, authors, or review or note text;
   - emails;
   - tokens, cookies, CSRF values or passwords.

   Request and response bodies and headers are never logged. Uvicorn's access log drops query strings and the client address, since Traefik and Cloudflare already log callers ([Logging: should the uvicorn access log keep the client IP?](https://github.com/ryansull/syncshelf/issues/46)). Change lines carry only status, rating and date values. A `review` value is logged as its length plus an 8-character sha256 prefix.
2. **Host aliasing.** Configured service URLs are logged as `hardcover`, `abs`, `chaptarr`, `goodreads` or `browser`. The `server.public_url` host is logged as `public` and the `server.access.team_domain` host as `access` ([Phase 1 config: confirm small calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/49)). Exception text has those hosts, and any other configured host, rewritten to the alias before it's logged or stored.
3. **Secret scrub.** A final logging filter replaces every configured secret value and every live in-memory token with `***`. The same function scrubs error strings **before they're stored** in `runs.first_error`, `baseline_fields.last_error`, `mirror_state.last_error` and `goodreads_sessions.detail`.

**Service-specific never-log rules:**
- raw ABS `/api/me` and `/api/users` bodies (they contain the user's legacy token);
- ABS item `path`/`relPath`;
- Chaptarr queue `indexer`, `downloadClient`, `downloadId` and `outputPath`;
- the Goodreads RSS `key`;
- the Goodreads `user_name`.

### 2.8 MCP tool conventions

- **Signatures** are `async def` with typed parameters. Optional parameters have defaults. Results are structured JSON.
- **Permissions** are per profile (`profiles.<p>.permissions`). A tool needing a permission the caller lacks is **filtered out of `tools/list`** for that identity and **refused** if called directly. The refusal is a tool error (`isError: true`, HTTP 200), never a 403, which Claude would treat as terminal. The only permission today is `chaptarr`, and both household profiles have it.
- **Errors** are tool errors with one human line and, where it helps, an `action` naming the tool or step that fixes it.
- **`profile` parameter on read tools:** `"me"` (default) | `"<name>"` | `"household"`. No profile can be named `me` or `household` (§2.3), so the argument is never ambiguous.
  - Asking for a profile that hasn't set `household_visible: true` is **refused**, not silently emptied.
  - Household rows carry a `profile` field.
  - **Shared:** status, rating, read dates and format. **Never shared:** review text and private notes.
  - No tool ever writes to another profile.
- **`attention`** (from 4a) is a list of `{code, message, action, id?}` items, present on every read tool's result for the caller's **own** profile, even on `household` queries. The field is absent when empty.
- **Output conventions:** ISO dates; numeric half-star ratings; audio length in hours; `as_of` (epoch → ISO) on anything served from the mirror.
- **Annotations:** read tools set `readOnlyHint: true`; `chaptarr_request` sets `destructiveHint: true` so the client asks for approval.
- **Context budget:** compact rows and fixed caps (§3.3.8). There's no `fields=` parameter anywhere.

### 2.9 Testing conventions

From [Testing strategy: fixtures, golden sets and live tests](https://github.com/ryansull/syncshelf/issues/29). Each phase's test list is in its own section.

- **Stateful in-memory fakes.** `FakeHardcover`, `FakeChaptarr`, `FakeABS` and `FakeGoodreads` sit behind `httpx2` `MockTransport`, routed by host, and answer only the operations SyncShelf sends, keyed by GraphQL operation name or route. Each fake reproduces its API's known traps (listed per phase).
- **Fault injection lives in the fakes.** `fake.fail_next(op, error_class, landed=False)` covers every failure-policy case, including a timeout that **landed**: the write applied, but the client saw an error.
- **One injectable clock** drives the limiter, schedules, TTLs, quarantine, the Chaptarr 7-day watch and OAuth expiry. **No test sleeps.**
- **Contract tests** check each fake's response for each operation against the recorded fixture's shape: keys, types and nullability. When a live API changes, re-record; the contract test fails; fix the fake.
- **Fixtures** are recorded with `tests/live/record.py <source>`.
  - The scrubber **keeps** public catalog fields: titles, authors, series, ISBN/ASIN, and Hardcover book/edition/author ids.
  - It **replaces** everything personal with placeholders: user ids and names, ratings, every date, statuses, reviews, notes, tokens, URLs and hosts.
  - A unit test re-runs the checker over every committed fixture.
  - Fixtures from source rather than a live instance say `source-only` in their metadata.
  - **Goodreads RSS and CSV fixtures are kept in their native format** (XML and CSV), so the parsers are tested against real bytes. The scrubber has **per-source** rules: Goodreads catalog elements and columns (title, author, ISBN/ISBN13, Goodreads `book_id`, Binding, publication years, page counts) are kept. Personal ones get placeholders that **still parse**: RFC 822 dates in RSS, `YYYY/MM/DD` in the CSV, `0` ratings, a placeholder shelf, and empty reviews and notes. The RSS `key` and `user_name` are replaced, and URLs and hosts are replaced as for JSON. The checker accepts `.xml` and `.csv` only under `tests/fixtures/goodreads/`. Decided in [Fixture scrubbing for non-JSON responses (Goodreads RSS XML and CSV)](https://github.com/ryansull/syncshelf/issues/44).
- **Golden sets** are YAML case files. Each gives the starting state (baseline, observation, service state, clock) and the expected result (writes issued, resulting baseline, queue and attention). One parametrized test per file runs each case through the real code against the fakes. These files **are** the decision tables. When a case and a ticket disagree, fix the case.
- **MCP tools** are tested through the real ASGI app. `authed_client(profile)` inserts a grant and token hash straight into the store, then drives `/mcp` through `httpx2.ASGITransport` with the SDK client. Tests use the production verifier, and production code has no test-only seam.
- **Live tests are opt-in.** The `live` marker is collected only with `pytest --live` **and** `SYNCSHELF_LIVE=1`. Credentials come from the repo-root `.env`. Per service:
  - **Hardcover reads** may use the real profile tokens.
  - **Hardcover writes** run only with `SYNCSHELF_LIVE_WRITE=1`, using `HARDCOVER_TOKEN_TEST` for a **throwaway account**. The harness checks `me` first and refuses if the username matches a real profile. Write tests restore what they change.
  - **Chaptarr live tests can only GET.** The harness client raises before sending any POST, PUT or DELETE.
  - **ABS** is GET-only with each profile's own key. **Goodreads RSS** is read-only. **Goodreads sign-in** is never automated in tests.
- **CI** (GitHub Actions, on every push and PR) runs `ruff check`, `ruff format --check`, `pyright`, the unit/fake/golden suites with outbound sockets **blocked** (`pytest-socket`) and a coverage report (`pytest-cov`, reported, never a gate), and gitleaks. Actions holds **no secrets**, and live tests never run there.

### 2.10 Contributing, PRs and docs

[Assembly choices D22–D29, proposed after the map closed. No ticket covered them.] The same rules are restated for agents in `AGENTS.md`, which also says which files to update for each kind of change. `CLAUDE.md` only imports `AGENTS.md`. If this section and `AGENTS.md` disagree, this section wins, and the PR that notices fixes both.

**Branches and PRs**
- `main` is always green and deployable. Work happens on short-lived branches off `main`, named `phase-<n>/<topic>` (e.g. `phase-2/token-store`), or `fix/<topic>` and `docs/<topic>` outside a phase.
- **A PR does one thing.** Aim for under ~400 changed lines, not counting `uv.lock`, fixtures and golden files. A phase is several PRs, never one. A PR that has to be large says why in its description.
- **Every PR follows `.github/pull_request_template.md`:**
  - **Summary:** what changed and why, in a few lines.
  - **Spec:** the § sections it implements, or "none".
  - **Tests:** what was added, what ran locally, and which live tests ran (if any), with their gates.
  - **Discrepancies:** any place the spec turned out wrong or silent, each linking the issue opened for it. "None" is a valid answer and must be written.
  - **Checklist:** CI green; no secrets or homelab values; fixtures scrubbed; docs updated per `AGENTS.md`; a `changelog.d/` fragment, or the `no-changelog` label for no user-visible change.
- **Review and merge.** The owner reviews and merges. Agents open PRs and never merge their own. Merges are **squash merges**, with the PR title as the commit subject.
- **Branch protection on `main`** (the owner turns it on in phase 1): PRs required, the CI check required, no force pushes, no deletion.
- **Commit messages:** an imperative subject under ~72 characters, no type prefixes, with a body when the why isn't obvious. Commits use the noreply author (§2.6).

**Versions and releases**
- Tags are `v0.<n>.<patch>`, where `<n>` counts phase releases in order: phase 1 → `v0.1.0`, 2 → `v0.2.0`, 3 → `v0.3.0`, 4a → `v0.4.0`, 4b → `v0.5.0`, 5 → `v0.6.0`, 6 → `v0.7.0`. Phase 7's live acceptance tags **`v1.0.0`**. Patches between phases bump `<patch>`.
- A phase is tagged only when its "Done when" holds. The tag triggers `release.yml`.
- The release PR bumps the version in `pyproject.toml` and `uv.lock`, and that commit is tagged. `release.yml` refuses a tag that isn't `v` + the package version.
- Image tags have no `v` (`0.1.0`), plus `:latest`.

  The release-version and image-tag rules are decided in [Release: how the version reaches 0.1.0, and whether image tags keep the v](https://github.com/ryansull/syncshelf/issues/52).
- **`CHANGELOG.md`** follows *Keep a Changelog*, assembled from per-PR fragments ([Changelog: per-PR fragments assembled at release](https://github.com/ryansull/syncshelf/issues/139)):
  - Each PR with a user- or operator-visible change adds a fragment, `changelog.d/<issue-or-pr>.<added|changed|removed|fixed>.md`, holding its bullet(s) as they should read, and never edits `CHANGELOG.md`. Migrations and new config keys are always listed. `changelog.d/README.md` says how to write one.
  - `## [Unreleased]` stays empty on `main`. CI runs `scripts/changelog.py check`, and fails a PR that changes `src/` without adding a fragment unless it's labelled `no-changelog`.
  - The release-prep PR runs `scripts/changelog.py build --version … --date … --title …`, which writes the fragments under the new version heading in section order (Added, Changed, Removed, Fixed) and deletes them, alongside the version bump.

**Docs**
- **`README.md`** (from phase 1): what SyncShelf is, the current phase, dev setup (`uv sync`, `pre-commit install`, the test and lint commands), a pointer to `profiles.example.yaml` for configuration and to `docs/runbook.md` for deployment, and the licence. It follows the §2.6 placeholder rules.
- **Docstrings** on every public module, class and function in `src/syncshelf/`. Comments explain *why*, not what.
- **MCP tool docstrings are the tool descriptions Claude reads**, so they're written for Claude: what the tool does, when to use it and when not to, its arguments and units, and any rule the spec puts on the caller (e.g. "pass today's date explicitly", §3.3.9). A tool's docstring changes in the same PR as its behaviour.
- **`docs/SPEC.md` is changed only by a decision:** a PR touching it links the issue that decided the change. A build PR never edits the spec to match its own code.

**Typing and coverage**
- `pyright` in **basic** mode over `src/`, in CI from phase 1. Public signatures are fully annotated.
- Coverage is reported in CI and never enforced. The golden files and contract tests are the real coverage.

**Dependencies.** `.github/dependabot.yml` opens weekly update PRs for `uv` and GitHub Actions. `mcp`, `starlette` and `playwright` updates also re-run the relevant contract tests, and a `playwright` bump moves the sidecar image tag with it (§2.1).

---

## 3. Phases

The order is **1** skeleton → **2** front door and profiles → **3** Hardcover → **4a** Goodreads RSS and `/ingest` → **4b** Goodreads session export → **5** Audiobookshelf → **6** Chaptarr → **7** deployment runbook and live acceptance. The front door moved up to phase 2 so that every later phase is usable from Claude, on a phone, on the day it ships ([The front door: auth architecture](https://github.com/ryansull/syncshelf/issues/11)). Goodreads splits into 4a and 4b ([Goodreads acquisition design: starting, storing and reusing a session](https://github.com/ryansull/syncshelf/issues/25)).

Each phase section lists what it builds, the tables it introduces, the contracts it depends on, the tools it ships, its tests, and when it's done.

---

### 3.1 Phase 1: Skeleton

**Goal:** a deployable, empty service. It has the process shape, config, DB, migrations, logging, health, CI and image, so that every later phase only adds.

**Builds**
- `pyproject.toml` (uv, package `syncshelf`, `src/` layout), `uv.lock`, and the dependencies from §2.1 that this phase uses.
- `__main__.py`: loads and validates config (§2.2 step 1), then runs uvicorn with **one worker**, `--proxy-headers`, `--forwarded-allow-ips` from `server.forwarded_allow_ips`, and the bind address from `server.listen`.
- `app.py`: the outer Starlette app with the route order from §2.2 and the lifespan steps that apply (data-dir check, DB, migrations, startup log, `mcp.session_manager.run()`).
- The **MCP mount with no tools**, so the lifespan wiring, the 421 allowlist and the route order are proven before auth arrives. Phase 1 has no auth, so it enforces the dev-mode guard: startup **refuses** a `server.public_url` whose host isn't `localhost` or `127.0.0.1` [assembly choice].
- `config.py`: loader, `${VAR}` / `*_file` expansion, and fail-fast validation (§2.3) for the keys phase 1 reads.
- `db/`: writer executor, read connections, PRAGMAs, `migrate.py` (runner + `VACUUM INTO` snapshot), and `0001_meta`.
- `logging.py`: JSON formatter, contextvars, scrub filter and host aliasing (§2.7), wired for uvicorn too.
- `/healthz` (liveness): 200 `{"status":"ok","version":…}` when `SELECT 1` on a read connection succeeds, 503 `{"status":"unavailable","version":…}` otherwise (§2.2).
- `Dockerfile` [assembly choice: in phase 1, so every phase is deployable]:
  - `python:3.14-slim` (Debian trixie, SQLite 3.46.1, accepted);
  - non-root `USER 10001:10001`;
  - `HEALTHCHECK` on `http://127.0.0.1:8000/healthz` ([Phase 1 serving: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/51)), using Python's `urllib` because slim has no curl;
  - `VOLUME /data`; the config at `/config/profiles.yaml` (read-only).
- `.github/workflows/ci.yml` (ruff check and format, pyright, pytest with `pytest-socket` and a coverage report, gitleaks) and `release.yml`, which on version tags builds **multi-arch** (amd64/arm64) images and pushes `ghcr.io/<owner>/syncshelf:<version>` (no `v`, §2.10) and `:latest`.
- The process files from §2.10:
  - `README.md` and `CHANGELOG.md` (with `## [Unreleased]`);
  - `.github/pull_request_template.md` and `.github/dependabot.yml`;
  - the **Commands** section of `AGENTS.md`, filled in with the real commands.
- Repo files:
  - `LICENSE` (Apache-2.0) and `.gitattributes` (`* text=auto eol=lf`);
  - `.pre-commit-config.yaml` (gitleaks + the `local/denylist.txt` hook);
  - `profiles.example.yaml` and an updated `.env.example`;
  - `profiles.yaml` added to `.gitignore`.
- `tests/`: `conftest.py` (clock, temp SQLite, live gating), the fixture scrubber and checker, and empty `fakes/`, `golden/`, `live/`.

**Tables:** `meta` (`0001_meta`).

**Tests**
- The migration runner: empty → each step, the `user_version` bump, the pre-migration snapshot, and a failed migration aborting startup.
- The scrubber and fixture checker.
- Live gating: the `live` marker isn't collected without `--live` + `SYNCSHELF_LIVE=1`.
- Config: expansion, `*_file`, and a missing variable failing with the key name and not the value.
- Redaction:
  - a configured secret in an exception → `***`;
  - a configured host → its alias;
  - the access log has no query string and no client address.
- `/healthz` 200/503, and the localhost-only guard.
- The SQL-invariant tests from the testing ticket (sticky-human trigger, state CHECKs, one-pending index) are **harnessed** here and **written with the migration that creates each table** (4a, 6). Phase 1 has no such tables.

**Done when:** `docker run` starts the image against an empty `/data`, `/healthz` is 200, `docker logs` shows JSON lines with version and SQLite version, CI is green, branch protection is on for `main`, and the `v0.1.0` tag publishes both architectures.

---

### 3.2 Phase 2: Front door and profiles

**Goal:** Claude can connect as a specific profile. SyncShelf is its own OAuth 2.1 authorization server (CIMD-only), Cloudflare Access does the login at `/authorize`, and the MCP SDK acts as the resource server.

Decisions:
- [Does a Claude custom connector work behind Cloudflare Access?](https://github.com/ryansull/syncshelf/issues/3)
- [The front door: auth architecture](https://github.com/ryansull/syncshelf/issues/11)
- [Building SyncShelf's OAuth server: SDK provider, Authlib, or hand-rolled?](https://github.com/ryansull/syncshelf/issues/22) (the full security checklist and test plan; findings comment)
- [OAuth policy leftovers: revocation, refresh reuse, absolute cap, scopes](https://github.com/ryansull/syncshelf/issues/23)

**Builds**
- `auth/oauth.py`:
  - `GET /.well-known/oauth-authorization-server`;
  - `GET /authorize` (validate, then render consent) and `POST /authorize` (approve, then issue a code);
  - `POST /token` (code exchange and refresh).
- `auth/tokens.py`: grant/code/token store, `TokenVerifier`, and the 60 s refresh-grace cache.
- `auth/access.py`: Access JWT verification.
- `auth/cimd.py`: client gate, fetcher and cache.
- `auth/templates/consent.html`.
- `tools/__init__.py`:
  - the tool registry;
  - the **per-identity `tools/list` filter** plus a server-side refusal (§2.8). Verify how SDK v2 exposes per-request tool listing; overriding the low-level list-tools handler is the expected route;
  - profile resolution.
- `tools/connections.py`: `my_connections`, `revoke_connection`.
- Profiles table sync (§2.3) and dev mode.

**Tables:** `profiles`, `oauth_grant`, `oauth_code`, `oauth_refresh_token`, `oauth_access_token`, `oauth_client_cache` (`0002_auth`).

#### 3.2.1 How Claude connects (contract)

**Verified live or from Claude's connector docs, 2026-10-17.** The same infrastructure backs claude.ai, Desktop, mobile and Cowork, and those calls come from Anthropic's servers (IPv4 `160.79.104.0/21`). Claude Code runs OAuth locally with a loopback redirect.

- **401.** Claude starts sign-in only on HTTP **401** with `WWW-Authenticate: Bearer resource_metadata="<PRM URL>"`. A header on a 200 is ignored, and a 403 re-auths only with `error="insufficient_scope"`. The SDK resource server sends this.
- **Discovery:**
  - PRM at `/.well-known/oauth-protected-resource/mcp` (served by the SDK). Its `resource` must **exactly equal** the URL the user typed (`https://shelf.example.com/mcp`), and only the **first** `authorization_servers` entry is used.
  - AS metadata at `/.well-known/oauth-authorization-server`.
  - Discovery is cached about 5 minutes per server URL.
- **CIMD.** Claude uses CIMD only when the AS metadata has `client_id_metadata_document_supported: true` and `"none"` in `token_endpoint_auth_methods_supported`. Otherwise it looks for DCR, which SyncShelf doesn't offer.
- **The two client documents.** SyncShelf allowlists **exactly** these two URLs:

  | Client | `client_id` (CIMD URL) | `redirect_uris` | Source |
  |---|---|---|---|
  | Claude apps (web, Desktop, mobile, Cowork) | `https://claude.ai/oauth/mcp-oauth-client-metadata` | `["https://claude.ai/api/mcp/auth_callback"]` | **observed** by probing; not in Claude's docs |
  | Claude Code | `https://claude.ai/oauth/claude-code-client-metadata` | `["http://localhost/callback","http://127.0.0.1/callback"]`, used with any port | documented |

  Both are served with `Cache-Control: public, max-age=300`. `client_label` is `claude.ai` or `Claude Code`, by URL.
- **Request requirements:**
  - **PKCE S256** on every request.
  - **RFC 8707 `resource`** on authorization and token requests, set to the canonical MCP URL.
  - `/token` is `application/x-www-form-urlencoded`.
  - Refresh tokens are **rotated** (public client), and a dead refresh returns **`invalid_grant`**, never `invalid_request` or a custom code.
- **Timeouts:** 10 s for discovery and token calls, 30 s for refresh. Claude refreshes on a 401 and also up to 5 minutes before expiry.
- **Network:**
  - Bot and WAF rules that return 403/429 break the flow, so they're skipped on the Claude-facing paths (phase 7).
  - **Don't** restrict by IP: Claude Code connects from the user's machine.
  - The hostname needs a public IPv4 `A` record. A proxied Tunnel hostname qualifies.

#### 3.2.2 Authorization-server metadata

```json
{"issuer": "https://shelf.example.com",
 "authorization_endpoint": "https://shelf.example.com/authorize",
 "token_endpoint": "https://shelf.example.com/token",
 "response_types_supported": ["code"],
 "grant_types_supported": ["authorization_code", "refresh_token"],
 "token_endpoint_auth_methods_supported": ["none"],
 "code_challenge_methods_supported": ["S256"],
 "client_id_metadata_document_supported": true,
 "authorization_response_iss_parameter_supported": true,
 "scopes_supported": ["syncshelf", "offline_access"]}
```

- **No `registration_endpoint`**, and `/register` is 404.
- The PRM also advertises `scopes_supported: ["syncshelf", "offline_access"]` ([OAuth policy leftovers](https://github.com/ryansull/syncshelf/issues/23)). Claude appends `offline_access` when it's advertised, which is how it gets a refresh token. If the SDK's PRM route can't carry `scopes_supported`, serve the PRM route on the outer app before the mount.
- Optionally add a root `/.well-known/oauth-protected-resource` alias.
- **Scopes:**
  - every grant carries `syncshelf`, plus `offline_access` when requested;
  - unknown requested scopes are ignored, not failed;
  - there is no read/write split.

#### 3.2.3 `/authorize`

**Client gate, before any network I/O.** `client_id` must be one of the two allowlisted URLs by exact string comparison. Defence in depth also checks `urlsplit`:
- `https`, host `claude.ai`, no explicit port;
- no userinfo, query or fragment;
- path not `""` or `/`, with no `.`/`..` segments;
- length ≤ 255.

Anything else gets an **error page**. Nothing is fetched and nothing is redirected, because `redirect_uri` is still untrusted.

**Fetch the CIMD document** (§3.2.6), then check it:
- a JSON object whose `client_id` equals the URL exactly;
- non-empty `redirect_uris`;
- `token_endpoint_auth_method` absent or `"none"`, and no `client_secret`;
- `grant_types` includes `authorization_code`. A document without `grant_types` is refused, not defaulted to RFC 7591's `["authorization_code"]` ([Client gate and CIMD fetcher: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/102)).

Issue refresh tokens only if `grant_types` also includes `refresh_token`.

**Redirect matching.** The requested `redirect_uri` must pass both of these:
1. **Membership in the document's `redirect_uris`** by exact string. The exception is a registered `http` URI on host `127.0.0.1`, `localhost` or `[::1]`: it matches when scheme, host, path and query are identical and only the port differs (any port 1–65535).
2. **SyncShelf's own policy set:** `https://claude.ai/api/mcp/auth_callback` exactly, or `http://127.0.0.1:<port>/callback` / `http://localhost:<port>/callback`.

Always reject:
- userinfo (`http://localhost@evil.com/callback`);
- `https://localhost…`;
- a trailing slash, extra path or query;
- `localhost.evil.com`;
- percent-encoded or dot-segment paths;
- a missing `redirect_uri`, which is always required and never defaulted.

**Errors.** Errors **before** the client and redirect are validated render an HTML error page. Errors **after** redirect `302` to `redirect_uri` with `error`, `state` and `iss`.
- **Error pages** carry the consent page's hardening headers, with `Referrer-Policy: no-referrer`. A refused client or redirect (including a missing or repeated `client_id` or `redirect_uri`, refused before any fetch) is HTTP 400. A CIMD document that can't be fetched, with nothing usable cached, is HTTP 502 ([Client gate and CIMD fetcher: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/102)).
- **Redirect error codes** (RFC 6749 §4.1.2.1, RFC 7636 §4.4.1, RFC 8707 §2):
  - a repeated parameter, a missing `response_type`, a `code_challenge_method` other than exactly `S256`, or a malformed `code_challenge`: `invalid_request`;
  - a `response_type` other than `code`: `unsupported_response_type`;
  - a missing `resource`, or one that isn't `MCP_URL` once canonicalised: `invalid_target`;
  - a missing or invalid Access JWT: `access_denied`;
  - the Access JWKS can't be fetched and no cached key fits: `temporarily_unavailable`.
- A repeated `state` isn't echoed, since there's no single value to echo, so that redirect carries `error` and `iss` only. A request with no `state` gets none back.
- **Order:** the client gate, the CIMD document and the redirect, then request hygiene, then the Access JWT, so a malformed request costs no JWKS fetch. The no-profile page therefore appears only for an otherwise valid request.

The error codes, `state`, order, no-profile page and Access JWKS rules below are decided in [Phase 2 /authorize: confirm calls the spec doesn't make (error codes, JWKS lifetime)](https://github.com/ryansull/syncshelf/issues/110).

**Request hygiene:**
- reject repeated parameters;
- accept only `response_type=code`;
- `code_challenge_method` must be exactly `S256` (never default to `plain`), with `code_challenge` matching `^[A-Za-z0-9_-]{43}$`;
- `resource` is **required**. Canonicalise it (lowercase scheme and host, strip one trailing slash); it must equal `MCP_URL`, else `invalid_target`;
- echo `state` verbatim.

**Access JWT** (Access guards `/authorize`, GET and POST). Read `Cf-Access-Jwt-Assertion`, which arrives as `cf-access-jwt-assertion` in ASGI headers. Never trust `Cf-Access-Authenticated-User-Email` or any unsigned header.
- **JWKS:** `<team_domain>/cdn-cgi/access/certs`. Cache it and re-fetch on an unknown `kid`. Keys rotate every 6 weeks, and the old key stays valid for 7 days. `PyJWKClient` fetches synchronously, so warm it at startup or run it in a thread.
  - Each fetch replaces the whole set.
  - The cached set expires **1 hour** after it was fetched. The next verification after that refetches it, and if the refetch fails the old set stays in use.
  - Unknown-`kid` refetches are limited to **one per 60 seconds**. A JWT with an unknown `kid` inside that cooldown is refused without a fetch.
  - The response body is capped at **64 KB**. A larger body is a fetch failure.
  - Times come from the app's injectable clock.
- **Checks:**
  - **RS256 only**;
  - `iss == team_domain`;
  - `aud` contains the `/authorize` app's AUD tag (`aud` is an array);
  - require `exp` and `iat`, and check `nbf` when present, all with leeway 30 s;
  - `type == "app"`;
  - non-empty `email`.
- **Lowercase the email**, then look up the profile. **If no profile matches**, render "no SyncShelf profile for <email>" (HTTP 403, an error page) and **issue nothing**.

**Consent page** (`GET` renders it). It reads: "Connect **claude.ai** to SyncShelf as **<email>**?" with one **Allow** button.
- It shows the `client_id` host, never `client_name`.
- For a loopback redirect, it adds a warning: "Code will be sent to a program on this computer (127.0.0.1:<port>). Continue only if you just started this from Claude Code."
- **Headers:**
  - `X-Frame-Options: DENY`;
  - `Content-Security-Policy: frame-ancestors 'none'; default-src 'self'`;
  - `Cache-Control: no-store`;
  - `Referrer-Policy: same-origin`, so the **Allow** POST carries a real `Origin` (a browser sends `Origin: null` under `no-referrer`) while no referrer reaches another site. Error pages and redirects keep `Referrer-Policy: no-referrer`. Decided in [Consent POST: Referrer-Policy no-referrer makes browsers send Origin: null](https://github.com/ryansull/syncshelf/issues/114).
- Escape every echoed value.

**Consent POST** (CSRF and binding).
- `GET` puts the **validated** request in a hidden field, signed with HMAC-SHA256 under `server.consent_hmac_key`. The signed blob holds `client_id`, `redirect_uri`, `code_challenge`, `resource`, `state`, scopes, email, and a 10-minute expiry. It also holds `refresh_allowed` (from the document's `grant_types`), because the POST never fetches CIMD again ([Phase 2 /authorize: confirm calls the spec doesn't make (error codes, JWKS lifetime)](https://github.com/ryansull/syncshelf/issues/110)).
- `POST /authorize` re-verifies the Access JWT and requires:
  - its email equals the signed email;
  - the HMAC and expiry are valid;
  - `Origin == ISSUER`, compared exactly.
- On success it issues a code and redirects to `redirect_uri` with `code`, `state` and `iss`.
- GET and POST share the `/authorize` path, so one Access rule covers both.
- **Order:** the blob, then `Origin`, then the Access JWT and its email.
- **A blob that doesn't verify** (missing, repeated, forged, malformed or past its 10 minutes) gets an HTML error page, HTTP 400, asking the person to start again from Claude. Nothing in it can be trusted, so nothing is redirected.
- **Refusals after the blob verifies** go to the signed `redirect_uri` with `state` and `iss`: `access_denied` for a missing or wrong `Origin`, an email that isn't the signed one, or a missing or bad Access JWT; `temporarily_unavailable` when the Access keys can't be fetched (as for `GET`).
- An email that no longer names a profile (possible only after a restart with a changed config) gets the same 403 no-profile page as `GET`.
- When the client's document doesn't allow refresh, the code is bound to the scopes without `offline_access`, so the grant and the token response's `scope` omit it too. The `oauth_code` row has no column for `refresh_allowed`.

The consent POST rules are decided in [Phase 2 consent POST and /token: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/115).

#### 3.2.4 Codes and tokens

- **Codes:**
  - `secrets.token_urlsafe(32)`, storing only `sha256(code)` as hex;
  - lifetime **120 s**;
  - bound on the row to `client_id`, the exact `redirect_uri`, `code_challenge`, `resource`, scopes, `profile` and `email`.
- **Code exchange** (`grant_type=authorization_code`), in one `BEGIN IMMEDIATE`:
  - `UPDATE oauth_code SET used_at=? WHERE code_hash=? AND used_at IS NULL AND expires_at>?`, then require `rowcount == 1`;
  - `code_verifier` must match `^[A-Za-z0-9._~-]{43,128}$`, and `b64url(sha256(verifier))` must equal the challenge (`hmac.compare_digest`);
  - `client_id` and `redirect_uri` must equal the code's values;
  - `resource`, if sent, must equal the code's (else `invalid_target`).

  A **replayed** code (`used_at` set) returns `invalid_grant` **and revokes the grant it produced** (`code_replay`).
- **Tokens** are opaque: `ssat_` + `token_urlsafe(32)` for access, `ssrt_` + `token_urlsafe(32)` for refresh.
  - Store `sha256` only, as hex, looked up by a unique index. Never log a raw token.
  - Access tokens live **3600 s**. Refresh tokens have a **sliding 90-day** expiry held on the grant (`refresh_expires_at = now + 90 d` on each refresh). **There is no absolute cap.**
- **`TokenVerifier`.** A token is accepted when:
  - its hash is found and not expired;
  - `revoked_at IS NULL` on the token **and** its grant;
  - the profile exists and isn't removed.

  It returns `AccessToken(token=<raw>, client_id, scopes, expires_at, resource=grant.resource, subject=<profile>, claims={"iss": ISSUER, "email": …, "grant_id": …})`. With `validate_token_resource=True`, the SDK also refuses any token whose `resource` isn't `MCP_URL`. `oauth_grant.last_used_at` is updated on every refresh, and on verification at most once a minute per grant [assembly choice].
- **Refresh rotation** (`grant_type=refresh_token`), in one `BEGIN IMMEDIATE`:
  1. Mark the presented token `rotated_at = now`, `successor_hash = <new>`.
  2. Insert the new refresh and access tokens.
  3. Update the grant's `refresh_expires_at` and `last_used_at`.

  Return the new pair. Hold the **raw successor pair in process memory for 60 s**, keyed by the old token's hash.
  - `client_id` must equal the grant's, and `resource` (if sent) the grant's.
  - **`/token` never fetches CIMD**, because the client is bound on the grant.
- **Reuse of a rotated refresh token:**
  - **Within 60 s, with the cache entry present:** return the **same** successor pair. This covers Claude retrying after a dropped response.
  - **After 60 s:** **revoke the whole grant** (`refresh_reuse`) and return `invalid_grant`.
  - **After a restart inside the window** (cache gone): return `invalid_grant` **without** revoking. The client just re-authorises.
- **`/token` responses:**
  - accept only `application/x-www-form-urlencoded` (JSON or multipart gets 400 `invalid_request`);
  - every response carries `Cache-Control: no-store` and `Pragma: no-cache`;
  - success: `{"access_token","token_type":"Bearer","expires_in":3600,"refresh_token","scope"}`;
  - no CORS on `/token` or `/authorize`.

  | Condition | Error | HTTP |
  |---|---|---|
  | Code unknown, expired, already used (+ revoke), PKCE mismatch, `client_id`/`redirect_uri` ≠ code's | `invalid_grant` | 400 |
  | Refresh token unknown, expired (sliding window passed), revoked, reused outside grace (+ revoke); grant revoked; profile removed | `invalid_grant` | 400 |
  | Missing or duplicated parameter, wrong content type, malformed verifier | `invalid_request` | 400 |
  | `client_id` missing or failing the gate | `invalid_client` | 401 |
  | `resource` ≠ code's or grant's | `invalid_target` | 400 |
  | Any other `grant_type` (including `client_credentials` and `jwt-bearer`) | `unsupported_grant_type` | 400 |

- **Code exchange details**, decided in [Phase 2 consent POST and /token: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/115):
  - **Order.** Outside the transaction: the body is form-encoded; no parameter is repeated; `grant_type` is present and supported; `client_id` passes the gate (else `invalid_client`); `code`, `redirect_uri` and `code_verifier` are present; the verifier matches its pattern. Only then is the code touched. Inside the transaction: spend the code, then check `client_id`, `redirect_uri`, PKCE and `resource`.
  - **A failed check after the spend still spends the code**, so nobody can guess at it, and a retry is then a replay (with no grant to revoke). Request-shape errors don't spend it.
  - **Any exchange of a used code is a replay** and revokes its grant, even after the code's 120 s have passed.
  - A profile removed since the code was issued gets `invalid_grant`.
  - `resource` is canonicalised as at `/authorize` before it's compared with the code's.
- **Refresh details**, decided in [Phase 2 refresh rotation: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/119):
  - **Order.** Outside the transaction: the body is form-encoded, nothing is repeated, `grant_type` is present and supported, `client_id` passes the gate (else `invalid_client`), and `refresh_token` is present (else `invalid_request`). Inside one `BEGIN IMMEDIATE`: the token is known; its grant isn't revoked; the token row isn't revoked; the profile isn't removed; the sliding window hasn't passed; then reuse after the grace period (revoke); then `client_id` and `resource` against the grant; then a retry inside the grace period; then the rotation.
  - **The 60 s boundary.** A reuse exactly 60 s after the rotation is outside the grace period and revokes, as a code at exactly 120 s is expired.
  - **Reuse after the grace period revokes** whatever `client_id` or `resource` it's sent with. A token on an already-revoked grant, a removed profile or a passed window is refused without revoking.
  - **A retry inside the grace period** from the wrong client or resource is refused (`invalid_grant` / `invalid_target`) without revoking, and the right client can still retry. From the right client it returns the held pair and changes nothing in the database (`last_used_at` and `refresh_expires_at` stay where the rotation put them). A retry with the original token after its successor was itself rotated, both inside 60 s, still returns the original successor pair.
  - **Access tokens issued before a rotation** stay valid until their own expiry.
  - The response's `scope` is the grant's scopes, and every rotation issues a refresh token.
  - **The grace cache** lives in process memory, one per app, shared by `/token` and the tools. It's filled and read on the DB writer thread inside the rotation's transaction, so two racing refreshes with one token rotate it once and the second gets the first's pair. Expired entries are swept on each use. Each held raw token is registered with the log scrubber (§2.7) while held. `revoke_connection` (§3.2.7) and a code replay drop a revoked grant's entries. `profile_deleted` happens only in the startup profile sync, when the cache is empty.
- **Responses**, decided in [Phase 2 consent POST and /token: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/115):
  - `invalid_client` is 401 with no `WWW-Authenticate`, since these public clients never authenticate with `Authorization` (RFC 6749 §5.2);
  - error bodies are `{"error": "<code>"}` only, with no `error_description`, so nothing from the request is echoed;
  - the success body omits `refresh_token` when none is issued, rather than sending null;
  - neither `/authorize` nor `/token` sets a request-body limit beyond the server's own.
- **Auth log events** (INFO unless noted; raw codes and tokens are never logged):
  - `code_issued` (`client_label`, `profile`); `grant_created` (`grant_id`, `client_label`) on each exchange;
  - `refresh_rotated` (`grant_id`, `client_label`); `refresh_retried` for a retry served from the grace cache;
  - WARNING `grant_revoked` (`grant_id`, `reason`: `code_replay` or `refresh_reuse`);
  - `token_request_refused` (`reason`, `client_label`) for each refused token request. Refresh reasons are `refresh_unknown`, `grant_revoked`, `token_revoked`, `profile_removed`, `refresh_expired`, `refresh_reuse`, `client_mismatch`, `resource_mismatch` and `refresh_grace_lost` (the restart case).

  The log events are decided in [Phase 2 consent POST and /token: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/115) and [Phase 2 refresh rotation: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/119).

#### 3.2.5 Dev mode

- **Setup:** `server.auth_mode: dev` with `server.dev.token` mapped to `server.dev.profile`, for Claude Code or MCP Inspector against a local instance. MCP Inspector can't pass the `claude.ai` client gate, so dev mode is how to use it.
- It's a branch in the same `TokenVerifier`. **The AS routes are disabled** in dev mode.
- **Startup refuses** dev mode when the `public_url` host isn't `localhost` or `127.0.0.1`, and logs a loud warning on every dev-mode start.
- **Startup refuses** a `server.dev.token` shorter than 32 characters.
- `server.listen` isn't restricted in dev mode, because a container must listen on `0.0.0.0` for a published port to reach it. Instead the README and the example config say: in dev mode, publish the port on loopback only (e.g. `127.0.0.1:8000:8000`) or listen on `127.0.0.1`.

The token length and listen rules are decided in [Dev mode hardening: a minimum dev-token length, and binding loopback only](https://github.com/ryansull/syncshelf/issues/75).

#### 3.2.6 CIMD fetch and SSRF limits

- **Order.** The gate runs first, so the only host ever fetched is `claude.ai`.
- **Client:** `httpx2.AsyncClient(follow_redirects=False, timeout=Timeout(5.0, connect=3.0), trust_env=False)`, with TLS verification on.
- **Response checks:**
  - only **200** succeeds; any 3xx is a failure (the CIMD draft says MUST NOT follow redirects);
  - `Content-Type` must be `application/json`, comparing the media type only and case-insensitively (`application/json; charset=utf-8` passes; `application/jsonp` or a missing header fails);
  - the request sends `Accept-Encoding: identity`, and a body with any other `Content-Encoding` is refused, so the cap counts what's held in memory;
  - stream the body and abort past **5 KB**.
- **Optional resolution check:** require every resolved address to be `ip_address(a).is_global`. Not built.
- **Caching:**
  - cache **only valid documents**, keyed by `client_id`;
  - freshness is `Cache-Control: max-age`, clamped to 5 min–24 h. A missing `Cache-Control`, `no-cache`/`no-store`, or an unparsable `max-age` counts as 0 and is clamped up to 5 minutes;
  - a last-good copy is kept in `oauth_client_cache` and served **stale-on-error for up to 7 days**, counted from `fresh_until` (so until `fresh_until + 7 d`);
  - the response's `ETag` is stored, but no conditional request is sent, since only a 200 succeeds;
  - single-flight per URL. Requests queued behind a fetch that fails share its outcome (the stale copy or the error page) instead of each waiting out its own timeout. Nothing is cached, so the next request to arrive fetches again.
- **A failure with no usable cache** aborts `/authorize` with an error page (HTTP 502, §3.2.3), never a redirect.

The content-type, encoding, `max-age`, stale-window, ETag, queued-failure and resolution-check rules are decided in [Client gate and CIMD fetcher: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/102).

#### 3.2.7 Tools

| Tool | Contract |
|---|---|
| `my_connections()` | The caller's own grants: `[{grant_id, client: "Claude app" \| "Claude Code", created_at, last_used_at, current: bool}]`. The grant behind the current call is `current`. Revoked grants are omitted. |
| `revoke_connection(target: int \| "all_others" \| "all")` | Revokes the caller's own grant(s) with reason `user` and drops their grace-cache entries. `"all"` includes the current grant, so the next call gets 401 and Claude prompts to reconnect. Returns what was revoked. |

Both tools are available to every profile (no permission) and touch only the caller's grants. There is no container CLI and no global kill switch. When Claude itself is unavailable, the fallback is a one-line `sqlite3` command, documented in the runbook (phase 7).

Decided in [Connection tools: confirm calls the spec doesn't make](https://github.com/ryansull/syncshelf/issues/97):
- **Results.** `revoke_connection` returns the revoked connections as a list in `my_connections`' row shape, with their values from before the revocation. `"all_others"` or `"all"` with nothing to revoke returns `[]`.
- **An id that isn't an open grant of the caller's** (unknown, another profile's, or already revoked) is refused as a tool error with one message: "There is no open connection N on this SyncShelf profile. Call my_connections to see the connections that can be revoked." So another profile's grant isn't confirmed to exist. A grant revoked earlier keeps its first revocation.
- **Timestamps.** `created_at` and `last_used_at` are ISO 8601 in UTC, with seconds and an explicit offset (`2100-01-01T00:00:00+00:00`).
- **Client.** A grant with an unknown `client_label` (anything but `claude.ai` or `Claude Code`) is shown under its raw label rather than refused.
- **Annotations.** `my_connections` sets `readOnlyHint: true`. `revoke_connection` sets none, since MCP's defaults for a tool that isn't read-only already read as destructive.
- **Dev mode has no current connection.** The dev token carries no grant (`grant_id` is null), so `my_connections` marks nothing `current` and `"all_others"` revokes every open grant of the dev profile, the same as `"all"`. Neither ends the dev session: the dev token keeps working, so "the next call gets 401" doesn't happen in dev mode.

#### 3.2.8 Tests

These are §6 levels 1–3 of the findings in [Building SyncShelf's OAuth server](https://github.com/ryansull/syncshelf/issues/22), adopted as-is.

1. **Unit tables:**
   - **The client gate.** It must reject:
     - `http://claude.ai/x`, `https://claude.ai.evil.com/x`, `https://evil.com/claude.ai/x`;
     - `https://claude.ai:443/x`, `https://u@claude.ai/x`;
     - `https://claude.ai/`, `https://claude.ai/a/../b`, `?q`, `#f`;
     - any non-allowlisted `claude.ai` path.

     Assert the mock transport was **never called** for a rejected id.
   - **`redirect_allowed`.** It must:
     - accept `http://127.0.0.1:53111/callback` and `http://localhost:3118/callback` for Claude Code, and the exact hosted callback for the hosted doc;
     - reject `https://localhost:1/callback`, userinfo, `localhost.evil.com`, `/callback/`, `/callback?x=1`, `/%63allback`, the hosted callback with a trailing slash, and each client's redirect presented with the other's `client_id`.
   - **The CIMD fetcher.** 302, non-200, non-JSON, a 5 KB+1 body, a `client_id` mismatch, `max-age` honoured, stale-on-error, errors never cached, single flight.
   - PKCE helpers and parameter hygiene.
2. **Flows through the ASGI app.** These use `httpx2.ASGITransport`, a temp DB, the injectable clock, a test RSA key signing fake Access JWTs, a mocked JWKS, and the two real CIMD documents. Those are fetched live (one public GET each, no credentials) and their exact JSON kept as plain test data in `tests/cimd_docs.py`, outside `tests/fixtures/` so the §2.9 scrubber doesn't blank their URLs, with the fetch date and `Cache-Control` in a comment. If either document differs from what the code accepts, that's an issue, not a code change. Decided in [CIMD fixture: the 2026-10-17 capture isn't in the repo, and the scrubber would blank its URLs](https://github.com/ryansull/syncshelf/issues/101). Cases:
   - the full code flow, then `/mcp` with a tool reading `get_access_token().subject`;
   - unknown email (no code issued);
   - code replay (`invalid_grant` + the grant revoked);
   - code expiry at 121 s;
   - PKCE mismatch;
   - `resource` mismatch at `/authorize` and at `/token`;
   - a JSON body at `/token`;
   - refresh rotation;
   - reuse inside the grace window (same pair) and after it (family revoked; the existing access token now 401);
   - restart inside the window (`invalid_grant`, grant intact);
   - sliding expiry (a day-89 refresh works; then 90 idle days give `invalid_grant`);
   - a removed profile;
   - `/register` is 404;
   - the metadata contains exactly §3.2.2;
   - an unauthenticated `/mcp` gets 401 with `resource_metadata`;
   - concurrent refreshes with one token (exactly one rotation).
3. **Scripted PKCE client.** Drive the ASGI app with the SDK's `mcp.client.auth.OAuthClientProvider(client_metadata_url="https://claude.ai/oauth/claude-code-client-metadata", …)`. Its redirect handler follows `/authorize` with a forged Access header and auto-POSTs consent. This exercises discovery, CIMD, S256, `resource` and RFC 9207 `iss` validation.
- **Other tests:** the `authed_client(profile)` fixture; dev mode refusing a non-localhost URL; `my_connections` / `revoke_connection` (own grants only; `"all"` kills the current token); the profile sync (removal revokes with `profile_deleted`; re-adding clears `removed_at`).

**Live checks at the first deployment.** These are level 4, formally the phase 7 acceptance:
1. Does Claude accept `iss` on the authorization redirect? If not, drop `iss` and the metadata flag together.
2. Does the Access app cover **POST** `/authorize` and forward `Cf-Access-Jwt-Assertion` there?
3. Does Cloudflare bot protection on claude.ai ever block the server's CIMD fetch? The last-good cache is the mitigation.
4. Which loopback host does Claude Code use? Does Claude send `resource` on refresh? Both are accepted either way.
5. Does a real browser's **Allow** click pass the `Origin` check? ([Consent POST: Referrer-Policy no-referrer makes browsers send Origin: null](https://github.com/ryansull/syncshelf/issues/114))

**Done when:** levels 1–3 pass in CI, and a local instance in dev mode serves `tools/list` (`my_connections`, `revoke_connection`) to Claude Code.

---

### 3.3 Phase 3: Hardcover

**Goal:** Claude can answer reading-history questions from each profile's Hardcover library. Every Hardcover call goes through a per-profile limiter. The write procedure that later phases reuse is in place.

Decisions:
- [Hardcover write API: user_book mutations, statuses, rate limit](https://github.com/ryansull/syncshelf/issues/6) (findings comment: literal queries and mutations)
- [Hardcover read API: how rich can the reading history get?](https://github.com/ryansull/syncshelf/issues/7) (findings comment: field inventory and cost)
- [Reading history questions: what should Claude be able to answer?](https://github.com/ryansull/syncshelf/issues/17)
- [Read-side strategy: live Hardcover queries or a local mirror?](https://github.com/ryansull/syncshelf/issues/18)
- [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27) (the limiter)

**Prerequisite (build prep, not a map ticket):** the user creates a **free throwaway Hardcover account** and puts its PAT in the repo-root `.env` as `HARDCOVER_TOKEN_TEST`. Live write tests run only against it.

**Builds**
- `clients/hardcover.py`: a thin GraphQL client.
  - Adds `Bearer `, sets a descriptive `User-Agent`, and checks `error` on every mutation result.
  - Counts cost per top-level field, and parses the rate-limit headers.
- `sync/limiter.py`, `sync/mirror.py`, `sync/apply.py`.
- The `matcher` **normalisation and scoring helpers** (§3.4a.5), which `check_books` needs. The chain itself lands in 4a. This brings in `rapidfuzz` here [assembly choice].
- `tools/library.py`: the six read tools.
- `tools/writes.py`: `update_book`, `correct_read`, `remove_book` (§3.3.9), and `tools/confirm.py`, the confirm-token helper.

**Tables:** `mirror_state`, `mirror_books`, `mirror_user_books`, `mirror_reads`, `mirror_list_items` (`0003_mirror`).

#### 3.3.1 Hardcover API contract

Verified live on 2026-10-17 unless marked *(docs only)*.

- **Endpoint:** `POST https://api.hardcover.app/v1/graphql`, JSON `{"query","variables"}` (Hasura underneath).
- **Auth:** `authorization: Bearer <PAT>`.

  | Sent | Result |
  |---|---|
  | `Bearer <token>` (any case) | 200 |
  | no prefix | 400 `invalid_request` "Use 'Bearer <token>'" |
  | no header | 400 |
  | garbage | 401 `invalid_token` |

  *(docs only)* PAT scopes needed: `read:me`, `read:library`, `read:catalog`, `write:library`. The API is "heavily in flux", and tokens may be reset without notice during the beta.
- **`me` returns a list:** `{"me":[{…}]}`.
- **Errors:**
  - transport errors (auth, rate limit, disallowed operator) are non-200 `{"error": …}`;
  - **mutation validation errors are HTTP 200** with the message in the mutation's own `error` field. Check it every time.
- **Rate limit (Free plan):** a token bucket, **burst 10, refilling 1/s (60/min), plus 5,000/day** resetting at midnight UTC.
  - **Headers:**
    - `RateLimit-Policy: "Free";q=60;w=60;burst=10, "daily";q=5000;w=86400`;
    - `RateLimit`;
    - `X-Ratelimit-{Limit,Remaining,Reset}`;
    - `X-Ratelimit-Daily-{Limit,Remaining,Reset}`.
  - **429** carries `Retry-After` and `Ratelimit-Reset`, and doesn't decrement the daily count.
  - *(docs only)* Each **top-level field** costs one token. More than 10 top-level fields gives **403 `request_exceeds_capacity`**. The limit is **per Hardcover user**, shared across all of that user's tokens and apps.
- **Other limits:**
  - **Rows:** every list field is **silently capped at 1000 rows**, so page past it by keyset: `order_by: {id: asc}`, `limit: 1000`, and `id: {_gt: <last id>}` for the next page. Offset paging can skip or repeat a row when the data changes between pages. Decided in [Hardcover library paging: offset under concurrent edits, and lists over the 1000-row cap](https://github.com/ryansull/syncshelf/issues/103).
  - **Depth:** *(docs only)* a **depth cap of 3** is announced for 2026. It isn't enforced today, but keep every query at depth ≤ 3.
  - **Operators:** `_like`, `_ilike`, `_regex` and the `_similar` variants are **403** ("ilike and related operations are not permitted"). Title matching uses `_eq` or `search`.
  - **Timeouts:** *(docs only)* 30 s query timeout (408), 2 s for `search`. A 503 is safe to retry.
- **Own data only.** Other users' `user_books` are reachable today, but a 2026 restriction is announced. Always filter `user_id = <me.id>`, where the id comes from one cached `query { me { id } }`.
- **Statuses** (`user_book_statuses`):

  | id | status | slug | Notes |
  |---|---|---|---|
  | 1 | Want to Read | want-to-read | |
  | 2 | Currently Reading | currently-reading | |
  | 3 | Read | read | |
  | 4 | Paused | paused | |
  | 5 | Did Not Finish | did-not-finish | |
  | 6 | Ignored | ignored | **Never written by sync.** |
- **Rating:** `numeric`, **0.5–5 in 0.5 steps**. `null` clears it. Anything else returns 200 with `error` "Rating must be between 0.5 and 5 in 0.5 increments" and leaves the row unchanged.
- **Review:** write `review_markdown` + `review_has_spoilers`. That sets `review_raw`, `has_review` and `reviewed_at`. **Read it back as `review_markdown`**, which round-trips exactly. `review_raw` is the text with its Markdown stripped (`review` is HTML), so it never equals a formatted review. Decided in [Hardcover's review_raw strips Markdown](https://github.com/ryansull/syncshelf/issues/149).
- **Privacy:** `privacy_setting_id` is 1 Public, 2 Followers, 3 Private. Public rows may post to followers' feeds *(not tested)*.
- **Reading formats:** `reading_format_id` is 1 Read (physical), 2 Listened, 3 Both, 4 Ebook. `edition_format` is free text and often null, so use `reading_format_id`. In SyncShelf's format buckets (`audio | ebook | print | unknown`), a **Both** read counts as **both `audio` and `print`**: it matches either filter, and `reading_stats` counts its pages and its hours, so a format grouping can add up to more books than were read. Decided in [Reading format 3 ("Both"): which format bucket does it count in?](https://github.com/ryansull/syncshelf/issues/108).
- **Mutations** (introspected; all exercised live):
  ```
  insert_user_book(object: UserBookCreateInput!): {id, error, user_book}
  update_user_book(id: Int!, object: UserBookUpdateInput!): {id, error, user_book}
  delete_user_book(id: Int!): {id, book_id, user_id, user_book}
  insert_user_book_read(user_book_id: Int!, user_book_read: DatesReadInput!): {id, error, user_book_read}
  update_user_book_read(id: Int!, object: DatesReadInput!): {id, error, user_book_read}
  delete_user_book_read(id: Int!): {id, error}
  upsert_user_book_reads(...)                 # NEVER USE (see trap 2)
  delete_reading_journals_for_book(book_id: Int!): {ids}
  ```
  - `UserBookCreateInput`: `book_id!`, `edition_id`, `status_id`, `rating`, `review_markdown`, `review_has_spoilers`, `date_added`, `first_started_reading_date`, `user_start_date`, `user_date`, `privacy_setting_id`, … `UserBookUpdateInput` is the same without `book_id`.
  - `DatesReadInput`: `id`, `started_at`, `finished_at` (dates), `edition_id`, `progress_pages`, `progress_seconds`, `finished_at_precision`, `action`, `action_at`.
- **Idempotency:** `insert_user_book` for a book already shelved **upserts on (user, book_id)**. It returns the same id and applies the new fields. With no `edition_id` the row gets the book's default physical edition.
- **Insert straight into Read with dates, in one call:** `{book_id, edition_id, status_id: 3, user_start_date, user_date, privacy_setting_id}` creates the read with those dates.

**Traps the client must handle:**
1. **Status 2 or 3 invents "today" dates.** Setting 2 opens a read with `started_at = today`. Setting 3 stamps `finished_at = today` on the open read. 4 → 2 opens another read. 2 → 1 keeps the reads. `first_started_reading_date` is never recomputed, and **can't be cleared**: writing `null` returns no error and changes nothing, though another date replaces it ([Hardcover won't clear first_started_reading_date](https://github.com/ryansull/syncshelf/issues/148)).
2. **`upsert_user_book_reads` is unsafe.** It resets omitted fields and silently ignores entries without an `id`. Use the single-read mutations with **full objects**.
3. **Read aggregates (`read_count`, `last_read_date`) update about 3–5 s later.** Never read them back right after a write. SyncShelf counts reads itself.
4. **`delete_user_book` leaves `reading_journals` rows behind.** Every write produces journal history.
5. **Reads can lag writes.** A read right after a write occasionally returns the state before it (seen on an insert, for under a second). Read your own write back before acting on it (§3.3.6 rule 4). Decided in [Hardcover reads can lag writes](https://github.com/ryansull/syncshelf/issues/150).

**Queries** (each one top-level field unless noted):

```graphql
# Goodreads edition ids → Hardcover (platform 1 = goodreads)
query ByGoodreads($ids: [String!]!) {
  book_mappings(where: {platform_id: {_eq: 1}, external_id: {_in: $ids}}) {
    external_id book_id edition_id
    book { id title canonical_id }
    edition { id isbn_13 isbn_10 asin reading_format_id }
  }
}
# ISBN / ASIN exact lookups (one top-level field each; batch with _in)
query ByIsbn13($v: [String!]!) { editions(where: {isbn_13: {_in: $v}}) { id book_id isbn_13 reading_format_id book { title canonical_id } } }
query ByIsbn10($v: [String!]!) { editions(where: {isbn_10: {_in: $v}}) { id book_id isbn_10 reading_format_id book { title canonical_id } } }
query ByAsin($v: [String!]!)   { editions(where: {asin:    {_in: $v}}) { id book_id asin    reading_format_id book { title canonical_id } } }

# Typesense search (per_page capped at 25; ids-only then hydrate is the cheap pattern)
query Search($q: String!, $filter: String, $sort: String, $per: Int!, $page: Int!) {
  search(query: $q, query_type: "Book", per_page: $per, page: $page, filter_by: $filter, sort: $sort) { ids results }
}
#   filter_by "author_names:=[`Frank Herbert`]"  (works live, contrary to docs)
#   filter_by "genres:=[`Fantasy`]" with query "*" and sort "users_count:desc"  (works live)
#   default ranking: title,isbns,series_names,author_names,alternative_titles; then users_count
#   hit documents: id title subtitle author_names isbns series_names release_year
#                  rating users_count has_audiobook has_ebook pages audio_seconds genres description …
```

**The library query.** This is the mirror refresh and each acting sync's fetch. It has two top-level fields, so it costs **2** requests. `user_books` is paged past 1000 rows by keyset, and so are `lists` and each list's `list_books` ([#103](https://github.com/ryansull/syncshelf/issues/103)).

```graphql
query Library($user_id: Int!, $limit: Int = 1000, $offset: Int = 0) {
  user_books(where: {user_id: {_eq: $user_id}}, order_by: {id: asc}, limit: $limit, offset: $offset) {
    id book_id edition_id status_id rating has_review review_markdown review_has_spoilers
    date_added first_started_reading_date privacy_setting_id updated_at
    user_book_reads(order_by: {id: asc}) {
      id started_at finished_at paused_at edition_id progress_pages progress_seconds
      edition { reading_format_id }                       # depth 3
    }
    edition { id reading_format_id pages audio_seconds isbn_13 isbn_10 asin }
    book {
      id title subtitle release_year pages canonical_id
      cached_contributors cached_featured_series
      genres: cached_tags(path: "Genre")
      moods:  cached_tags(path: "Mood")
      tags:   cached_tags(path: "Tag")
      default_audio_edition    { id audio_seconds }       # depth 3
      default_ebook_edition    { id }
      default_physical_edition { id pages }
    }
  }
  lists(where: {user_id: {_eq: $user_id}}) { id name list_books { book_id } }
}
```

Notes on this query:
- A sync's own fetch keeps `review_markdown` **in memory only**, to skip no-op review writes. The mirror never stores it ([Hardcover's review_raw strips Markdown](https://github.com/ryansull/syncshelf/issues/149)).
- A key with a space needs the bracket form (`cached_tags(path: "[\"Content Warning\"]")`).
- Narrators are on the **audio edition**, not the book.
- `book.audio_seconds` is unreliable, so use the edition's.
- `cached_featured_series` gives only the featured series and can be stale.
- `cached_tags` holds at most 10 per category and is about 75–80% of the payload.

**Cost:** about 7 KB/book raw. A 1,500-book library is about 9–10 MB raw (under 1 MB gzipped), 2–3 requests, 2–3 s.

**Server-side:**
- `_aggregate` works on your own rows (count/avg/sum with `where`), but there's **no group-by**, so stats are computed locally.
- `me { goals { metric goal progress start_date end_date state } }` carries goal progress.
- `book.cached_similar_book_ids` (about 100 ids) is popularity-weighted co-readership, sequels included. It's a seed, not a recommender.

#### 3.3.2 Data reality to design for

On the first profile's account, most Read books have **no reads and no read dates**, because Hardcover's importer didn't create them. Date questions ("when did I read X", per-year stats) work for reads the syncs write from now on. Backfilling old dates is **out of scope**. Every answer reports reads without a format in an explicit `unknown` bucket.

#### 3.3.3 The per-profile Hardcover limiter

From [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27).

- **One async token bucket per profile**: **burst 8, refill 1/s**. That leaves 2 tokens of headroom for the user's other Hardcover use. Cost is counted **per top-level field**. Every Hardcover call for that profile goes through it: sync fetches and writes, mirror refresh, write-through, `get_book`, search, series, `check_books`, and Chaptarr `want=true`.
- **Interactive calls jump the queue.** MCP tool calls are served before queued sync calls.
- **429:** pause **that profile's whole client** until `Retry-After` (falling back to `Ratelimit-Reset`, then 5 s), then resume.
  - Sync loops **keep their locks** while paused, so `sync_now` can't sneak in a parallel run.
  - An interactive tool that would wait more than ~5 s answers **stale from the mirror** instead.
- **Daily budget:**
  - The limiter tracks `X-Ratelimit-Daily-Remaining`.
  - **Below `hardcover_daily_reserve` (1,000)**, sync loops skip Hardcover work until the daily reset (midnight UTC). Their baselines don't advance, so nothing is lost, and the run is `deferred`.
  - Interactive calls continue until **0**, then serve stale.
- **`403 request_exceeds_capacity`** is a **bug**: it's logged and surfaced, never retried.
- **State exposed** for `sync_status` (4a): `limiter: ok | paused | reserve`, `paused_until`, `daily_remaining`.

Budget context: each profile has its own 5,000/day. Idle syncs cost 0. A busy day (a dozen pushes and an hour of chat) is well under 200.

#### 3.3.4 The mirror

From [Read-side strategy: live Hardcover queries or a local mirror?](https://github.com/ryansull/syncshelf/issues/18), as amended by the cadence ticket.

- **What reads it:** `get_my_library`, `reading_stats`, the shelf-status annotations on `search_books` / `check_books`, and the started-series lookup in `series_progress`. `get_book` is **always live** and writes its fresh row back into the mirror.
- **Freshness: TTL refresh on read.**
  - A library tool first refreshes the profile's mirror if `as_of` is older than `mirror_ttl` (10 min). The refresh is the library query (2–3 requests, 2–3 s on that call).
  - Every response carries `as_of`.
  - **There's no force-refresh parameter.** Right after an edit made directly in Hardcover, wait out the TTL or ask about that one book (`get_book` is live).
- **Refresh failure: serve stale, flagged.** If the refresh fails (Hardcover down, 429 beyond 5 s, an expired token, the daily budget at 0), the tool answers from the mirror with `stale: true`, `stale_reason` and `as_of`. It refuses only when that profile has **no mirror yet**. `mirror_state.last_attempt_at` and `last_error` (scrubbed) record it.
- **Full replace per profile:** `DELETE … WHERE profile = ?` plus inserts, in one transaction. A user_book deleted in Hardcover disappears from the mirror; there are no tombstones. A replace never touches links or baselines; the mirror has no FKs in or out.
- **Mirror lock per profile:** a read-triggered refresh and a sync's fetch never run at once. A reader that finds a refresh in progress waits for it.
- **Write-through:** after any SyncShelf write, re-fetch just that user_book (1 request) and upsert it. Writes are Claude edits, `resolve_match` and sync pushes.
- **Household:** a `household` or `"<name>"` query refreshes **the other profile's** mirror with **that profile's own** token, under the same TTL. The `household_visible` gate applies before any refresh. A `household` answer where some profile has **no mirror yet** answers from the profiles that have one, flagged `stale: true` with that profile named in `stale_reason`. It refuses only when no profile asked about has a mirror. Decided in [Household reads when one opted-in profile has no mirror copy yet](https://github.com/ryansull/syncshelf/issues/116).
- **Merges (from 4a):** a mirror refresh counts as a Hardcover fetch for identity purposes. Seeing a linked book with `canonical_id` set rewrites links and source rows (§3.4a.4). Phase 3 just stores what it sees.
- **What the mirror holds** (no review text, no private notes):

  | Table | From the library query |
  |---|---|
  | `mirror_user_books` | `id`, `book_id`, `status_id`, `rating`, `edition_id`, `date_added` |
  | `mirror_reads` | each read's `id`, `started_at`, `finished_at`, `edition_id`, and `edition.reading_format_id` (NULL → the `unknown` bucket) |
  | `mirror_books` | `title`; `contributors_json` (`[{name, role}]` from `cached_contributors`); featured series id/name/position; `pages` (book, else default physical); `audio_seconds` (default audio edition); `tags_json` (top tags by category) |
  | `mirror_list_items` | `lists.id`, `name`, `list_books.book_id` |
  | `mirror_state` | `as_of`, `last_attempt_at`, `last_error` |

#### 3.3.5 Edition choice for a read SyncShelf writes

From [Reading history questions](https://github.com/ryansull/syncshelf/issues/17), decision 3, amending the ABS ticket.

**Every read SyncShelf writes carries an edition of the right format**, so format questions are answered from Hardcover itself and nothing about format is stored only in SyncShelf:
- the link's `hc_edition_id`, if the link's format matches the source format;
- otherwise the book's `default_audio_edition` / `default_ebook_edition` / `default_physical_edition` for the source format.

Reads logged by hand in Hardcover keep whatever the user picked.

**A Goodreads row with no Binding observed yet** (RSS-only) takes its format from the link's `hc_edition_id`: a `gr_mapping` or ISBN hit is the edition the user shelved, so a Kindle row maps to a Kindle edition. That edition, with its `reading_format_id`, goes on the read. If the link has no edition (fuzzy or human), or the edition has no `reading_format_id`, the read is written **with no edition**, so it lands in the `unknown` bucket rather than a guessed print edition. Once Binding has been observed, it decides the format for later reads ([#32](https://github.com/ryansull/syncshelf/issues/32), decision 7).

#### 3.3.6 The write procedure: "make Hardcover match" (`sync/apply.py`)

This is one procedure, used by every SyncShelf write: Goodreads (4a), ABS (5), Chaptarr `want=true` (6), `resolve_match`, and the direct edits (§3.3.9). It comes from diff decision 7, failure-policy decision 2, and the ABS ticket's decisions 3–5.

**Input:** profile, `hc_book_id`, and a **target** made of any of:
- `status_id`;
- `rating` (or clear);
- `review_markdown` + `review_has_spoilers` (or clear);
- one **read operation**:
  - `start(date)`;
  - `finish(date, start_date?, format, edition_id)`;
  - `correct(old_finish_date → new_finish_date)`.

**Rules:**
1. **Use fresh state.** Use the user_book and reads from a fetch **in the same run**, never an older mirror. Before inserting a read, re-read that user_book's reads, so a replay adopts rather than duplicates.
2. **Skip no-ops.** A field already equal in Hardcover isn't written. That's the only thing Hardcover's current value is used for.
3. **Absent user_book: one insert.** `insert_user_book` with `book_id`, `status_id`, `rating`/review, `edition_id` (§3.3.5) and `privacy_setting_id` from `hardcover_privacy`, plus the dates seeded in the same call:
   - status 2 → `user_start_date`;
   - status 3 → `user_date` (+ `user_start_date` if known).

   Privacy applies **to inserts only**; updates never change an existing row's privacy. A status-3 insert with `user_date` and no `user_start_date` creates exactly one read, finished on that date, with no start (verified live in phase 3, [#94](https://github.com/ryansull/syncshelf/issues/94)).
4. **Existing user_book: status write, then read reconciliation.**
   - Snapshot the reads.
   - Write the status (`update_user_book`).
   - **Wait for it to show** (trap 5): re-fetch until the user_book has the new `status_id`, or after an insert until it exists, for at most 3 s. If it never shows, the change fails as partly done, and a replay completes it (rule 9). Decided in [Hardcover reads can lag writes](https://github.com/ryansull/syncshelf/issues/150).
   - Re-fetch the reads and compare them with the snapshot. Every difference Hardcover made (a read it **created**, a `finished_at` it **stamped** with today) is either turned into the intended read with a full-object `update_user_book_read`, or **undone**:
     - delete a read the write created that isn't the intended one;
     - restore a stamped read to its snapshot values.
   - An invented "today" date is never left in place.
5. **Read operations** use `insert_user_book_read` / `update_user_book_read` / `delete_user_book_read` with **full objects**. Never `upsert_user_book_reads`.
   - **`finish`** completes or adopts an **adoptable read** (±14 days, or an open read started on or before the date): an open read gets `finished_at`, and a read already within the window is left as it is. Otherwise it inserts a new read (start, finish, edition).
   - **`correct`** updates the read whose `finished_at` equals the old date. If none matches, it inserts one, unless an adoptable read exists.
   - **`start`** opens a read with the explicit `started_at`.
6. **`first_started_reading_date`** is set explicitly whenever the earliest read's start changes. With no dated start left, it's left as Hardcover has it: Hardcover can't clear it (trap 1), so SyncShelf sends no `null`. Decided in [Hardcover won't clear first_started_reading_date](https://github.com/ryansull/syncshelf/issues/148).
7. **Never read back** `read_count` / `last_read_date` after a write.
8. **Never delete** a read, or a user_book, except a read Hardcover created as a side effect of this same write (rule 4). The only other deletions are Claude's confirmed ones (`remove_book`, `correct_read(delete=true)`, §3.3.9), which run outside this procedure.
9. **One change, all or nothing, and convergent.** A status write and its read edits are one change. If any step fails, the change fails, and its baseline (4a+) doesn't advance. A replay re-runs the whole procedure: steps already done are skipped as no-ops, and the rest are applied. **Nothing besides baselines ever needs undoing.**
10. **After success:** write through to the mirror (§3.3.4).

The procedure holds the profile's Hardcover apply lock (§2.2).

#### 3.3.7 The read tools

All six take `profile = "me" | "<name>" | "household"` (§2.8), are `readOnlyHint`, and carry `as_of` / `stale` when mirror-backed. Signatures are fixed by [Reading history questions](https://github.com/ryansull/syncshelf/issues/17), and the data source of each by [Read-side strategy](https://github.com/ryansull/syncshelf/issues/18).

**Questions they cover:**
1. Have I read *X*? What did I rate it, and what did I say about it?
2. What am I currently reading?
3. What's on my want-to-read by author, in a genre, or under N hours?
4. Which series am I partway through? What's next?
5. What did I rate highest last year?
6. How many books, pages and hours this year?
7. Audiobooks vs ebooks this year.
8. Who are my favourite authors? What are my top genres?
9. How am I doing on my reading goal?
10. When did I read *X*? How many times?
11. What was I reading in a given period?
12. What should I read next?
13. What have we both read? What did the other person think?
14. Which authors are similar to one I've read?
15. What should I read next in a genre?
16. I liked *X*: three similar books I might like.

**Recommendations are Claude's job.** There's no `recommend()` tool and no similarity scoring in SyncShelf. SyncShelf only grounds them:
- **taste:** ratings by author, genre and book;
- **exclusion:** anything shelved, including DNF and Ignored;
- **verification:** `check_books`;
- **seeds:** similar-book ids and genre search.

| Tool | Source | Contract |
|---|---|---|
| `get_my_library(status?, query?, author?, genre?, series?, format?, min_rating?, finished_from?, finished_to?, sort?, limit=25, cursor?, profile="me")` | mirror | Compact **library rows** (below). `status` is a slug (`want-to-read` …). `format` is `audio\|ebook\|print\|unknown`. `finished_*` are ISO dates over reads' `finished_on`. `sort` is `date_added\|last_finished\|rating\|title\|author`, default `date_added` desc [assembly choice for the sort keys]. `limit` defaults to 25, max 100. Every response carries `total` and `next_cursor`. When `total` exceeds the page, it also carries **counts by status for the whole filtered set**, so counting questions never need paging. "Highest rated last year" = a date range + `sort=rating`. |
| `get_book(book: int, profile="me")` | **live**, then written back to the mirror | The full record, keyed by Hardcover `book_id` (resolve titles with `search_books` / `check_books` first) [assembly choice: id only]. It includes: shelf state; every read with its `id`, dates and format; review + spoiler flag (**own profile only**); all series with positions (`book_series`); contributors with roles, plus narrators from the audio edition; tags (top 5 each of Genre / Mood / Tag); every content warning (≤ 10); lists the book is on (from the mirror); the top 10 similar books as compact rows. About 2 requests (the book or user_book, plus a hydrate of the similar ids). |
| `series_progress(series?, profile="me")` | mirror for started series, then **one live batched request** `series(where: {id: {_in: …}})` with `book_series(order_by: {position: asc}) { position book_id }`, depth ≤ 3 | Without `series`: the series started but not finished, each with the **next unread entry** (the lowest position not Read). With `series` (an id or name): the full reading order with per-entry status. Series lists are not cached. A series' `book_series` also holds translations and editions at the same position, box sets and unpositioned rows, so: box sets (`book.compilation`) and rows with no position are left out; each position is shown as its most-read book (`users_count`); a position is Read when **any** book at it is; and "next" and "finished" use **whole-number** positions only, so novellas (x.5) are listed but never block. A series read only through a box set shows as unread (accepted). Decided in [series_progress: one book per position, and do novellas count?](https://github.com/ryansull/syncshelf/issues/128). |
| `reading_stats(period, group_by?, profile="me")` | mirror (+ live `me { goals }`, 1 request, omitted on failure) | Books, pages and hours, grouped by `month \| format \| genre \| author \| rating`, plus goal progress. **Aggregates only, never a book list.** Computed locally. `period` is `YYYY`, `YYYY-MM`, `last_30_days`, `last_12_months`, `all` or `YYYY-MM-DD..YYYY-MM-DD` [assembly choice for the period forms]. A read counts in the period by its `finished_on`. Pages come from print/ebook reads, hours from audio reads' `audio_seconds` [assembly choice]. |
| `search_books(query?, author?, genre?, sort?, exclude_shelved=true, limit=10, profile="me")` | live Typesense; mirror for the annotation | Each hit is a compact row plus a short description, annotated with shelf status. `limit` defaults to 10, max 25. With `exclude_shelved`, every shelved book is dropped (including DNF and Ignored). Author and genre map to `filter_by`, and a filter with no query uses `query: "*"`. `sort` is `relevance\|popular\|rating\|newest` [assembly choice]. This is the general search tool that `resolve_match` relies on. |
| `check_books(candidates, profile="me")` | live resolve (search); mirror for shelf status | `candidates` is up to **20** items of `{title, author?}` or `{book_id}`. Each comes back as one compact row with rating, length, series position, **audio/ebook availability on Hardcover** (`has_audiobook` / `has_ebook`), shelf status (the other profile's too with `household`), and `match: exact \| likely \| not_found`. **It never guesses**: an unclear candidate is `not_found`, with no closest hit substituted. `exact` = a `book_id` was given, or the top hit passes the matcher's fuzzy auto-accept rule (title ≥ 95, author ≥ 90, margin ≥ 10). `likely` = title ≥ 85 and author ≥ 80 with margin ≥ 10. Anything else is `not_found` [assembly choice for the `likely` band; calibrate with the matcher in 4a]. A candidate with **no author** has its author score ignored: a clear title match can be `likely`, never `exact` ([check_books: can a title with no author match?](https://github.com/ryansull/syncshelf/issues/142)). |

#### 3.3.8 Response shape and size

- **Library row** (fixed, about 250 bytes):
  - `book_id`, title, first author, status, rating;
  - format(s) read, last finished date, read count (counted locally from `mirror_reads`);
  - featured series + position;
  - **top 3 genres**;
  - `profile` on household rows.

  No description, tags, review or cover.
- **`get_book`:** tags capped at the top 5 per category; all content warnings.
- **`search_books`:** 10 by default, 25 max (Hardcover's cap).
- **`check_books`:** 20 max per call, one compact row each.
- **`reading_stats`:** at most 15 buckets per grouping, then `other`, always plus `unknown`.
- **Deliberately left out:** a timeline/history tool (date filters plus `get_book` cover it), a lists tool (membership shows in `get_book`), and a `fields=` parameter.

#### 3.3.9 Direct Hardcover edits (`tools/writes.py`)

From [Direct Hardcover edits from Claude: which write tools, and abs_library_search](https://github.com/ryansull/syncshelf/issues/31), building on [Goodreads to Hardcover diff](https://github.com/ryansull/syncshelf/issues/13) decision 10 and §2.8.

**Ground rules:**
- Claude's edits write Hardcover **directly**, through the write procedure (§3.3.6), under the apply lock, at interactive limiter priority, then write-through.
- They **never touch a Goodreads or ABS baseline**. An unchanged source row therefore never clobbers them, and an edit can't be mistaken for a source change.
- If the source later changes that field, **the last writer wins**: the source change is a fresh act by the user and is pushed.
- Own profile only. No tool writes to another profile.
- **Edits act on the first call.** Only deletions (`remove_book`, `correct_read(delete=true)`) preview and need a confirm token. Claude asks the user in conversation before editing.
- **Every result returns the prior values** of what it changed (status, rating, review text + spoiler flag, the touched read), so Claude can undo.
- An insert uses `hardcover_privacy` (§3.3.6 rule 3). There's no per-call override.

| Tool | Contract |
|---|---|
| `update_book(book_id, status?, rating?, review?, review_has_spoilers?, started_on?, finished_on?, format?, reread=false)` | Builds one §3.3.6 target, so it's **one change**. Inserts the user_book if absent. `status` is a slug: `want-to-read \| currently-reading \| read \| paused \| dnf \| ignored`. **Ignored is writable here** (sync never writes it). There's no "none": that's `remove_book` [assembly choice]. `rating` is 0.5–5 in half steps, and `0` clears it. `review=""` clears the review and its spoiler flag [assembly choice]. Dates are ISO dates. `destructiveHint: false` [assembly choice]. |
| `correct_read(book_id, read_id, started_on?, finished_on?, format?, delete=false, confirm_token?)` | Edits one existing read (ids come from `get_book`) with a full-object `update_user_book_read`, and acts at once. With `delete=true` it's a deletion: a preview without a token, `delete_user_book_read` with one. Sets `first_started_reading_date` per §3.3.6 rule 6. `destructiveHint: true`. |
| `remove_book(book_id, confirm_token?)` | Without a token, a **preview**: status, rating, whether a review exists, every read with its dates, whether journal rows exist, and any source row still linked to the book. With a token it deletes the user_book **and calls `delete_reading_journals_for_book`**, then removes the book from the mirror. `destructiveHint: true`. |

**Dates: no date means no read.** Every date written to Hardcover is one the user stated.
- `status=read` without `finished_on` sets Read and logs **no read**. §3.3.6 rule 4 undoes Hardcover's auto-"today" read. `currently-reading` without `started_on` is the same.
- For "I just finished it", **Claude passes today's date explicitly**. It resolves "yesterday" or "last Tuesday" to an ISO date in the profile's `timezone`. The tool descriptions say so.
- **Refused:** `status=read` without `finished_on` on a book with an **open read**. The error (`date_needed`) names the read's start date, so Claude asks. A Read status never sits over a dangling open read.
- `finished_on` without `status` implies Read, and `started_on` alone implies Currently Reading [assembly choice].
- **`finished_on`** runs the `finish` operation, **with the ±14-day / open-read adoption** (§3.3.6 rule 5). A finish ABS already logged is adopted, not duplicated. `reread=true` skips adoption and inserts a new read.
- **`started_on` alone** runs `start`. It's refused if an open read already exists (use `correct_read`) [assembly choice].
- Paused and DNF leave reads alone [assembly choice].

**Format, when the user doesn't say.** It's used only when a read is written. The tool takes the first of these that applies:
1. the explicit `format`;
2. the open read being finished (its edition);
3. the **single** format shown by the user's links and source rows for that book (an ABS row gives audio, a Kindle Goodreads row gives ebook).

Otherwise it's refused with `format_needed`, and Claude asks. **It never guesses.** The edition then follows §3.3.5.

**Deletions and the confirm token** (`tools/confirm.py`, landing here and reused by `chaptarr_request` in phase 6 [assembly choice]):
- The token lives **10 minutes**, in memory, and is bound to (profile, tool, `book_id`, `read_id`?, a snapshot of the user_book and reads).
- **On confirm, the server re-checks** the snapshot. If anything changed, it returns a fresh preview and does nothing.
- These are the only deletions SyncShelf ever makes outside §3.3.6 rule 4 (rule 8 is amended accordingly).
- A removed book is **not re-added** by sync while its source row's baseline is unchanged. A later source change is a fresh act and is pushed (last writer wins). The preview says so when a linked source row exists.
- This is the confirmed removal `resolve_match` points at for the old user_book after a re-link (§3.4a.11).

**`abs_library_search` is dropped.** `library_status` (phase 6) reports `have` per format from Chaptarr, which sees the files ABS reads. Books added to ABS outside Chaptarr are an accepted gap.

#### 3.3.10 Tests

- **`FakeHardcover`** reproduces the traps:
  - status 2/3 auto-creates and stamps reads dated today;
  - a read upsert resets omitted fields;
  - a delete leaves journal rows;
  - 429 + `Retry-After`;
  - the 1000-row silent cap;
  - 200-with-`error` validation;
  - the async aggregates.

  Contract tests run against recorded fixtures.
- **Limiter:** burst/refill, interactive priority, a 429 pausing all callers, the daily-reserve cutoff (syncs `deferred`, interactive continuing), and 403 capacity treated as a bug.
- **Mirror:** TTL refresh, `as_of`, stale-but-flagged on failure, refusal with no mirror, full replace drops deleted rows, write-through, the household refresh using the other profile's token, and the `household_visible` refusal.
- **Write procedure:** each trap converges, including the fault-injection case "status ok, read fix-up failed → rerun converges", the ±14-day adoption edges (day 14 vs 15, open read, undated never adoptable) and edition choice.
- **Read tools through the ASGI app** (`authed_client`): caps, paging and counts, the `unknown` bucket, own-profile-only reviews, `check_books` never guessing.
- **Edit tools through the ASGI app:**
  - no date means no read, and no auto-"today" read survives;
  - `date_needed` over an open read;
  - adoption vs `reread=true`;
  - the format inference order and `format_needed`;
  - prior values in every result;
  - Ignored writable;
  - baselines untouched;
  - inserts use `hardcover_privacy`.

  Deletions: a preview writes nothing; a token is bound, expires at 10 minutes, and a changed snapshot returns a fresh preview; `remove_book` also deletes journals (the fake's trap 3); a removed book isn't re-added by an unchanged source row.
- **Live:**
  - reads on the real tokens;
  - **writes only on `HARDCOVER_TOKEN_TEST`**, restoring what they change. These confirm the status-3-with-`user_date`-only insert and the reconciliation in §3.3.6.

**Done when:** from Claude Code (dev mode or a deployed instance), each of the 16 questions gets a correct, size-capped answer for the first profile. Answers note `unknown`-format and missing-date reads honestly.

---

### 3.4a Phase 4a: Goodreads RSS, `/ingest`, and the sync engine

**Goal:** Goodreads changes reach Hardcover with no Goodreads session. Kindle reads arrive within about an hour, through RSS. A manual CSV upload is the fallback and the source of format, ISBN13 and Read Count. This phase builds the engine that ABS reuses in phase 5: identity, the matcher, the per-field diff, runs, failure policy, the scheduler, attention and `sync_status`.

Decisions:
- [Goodreads CSV and shelf RSS: what fields actually arrive](https://github.com/ryansull/syncshelf/issues/10) (findings comment)
- [Canonical book identity across Goodreads, Hardcover, ABS and Chaptarr](https://github.com/ryansull/syncshelf/issues/12)
- [Goodreads to Hardcover diff: what 'Goodreads changed it' means](https://github.com/ryansull/syncshelf/issues/13)
- [Matching and the unmatched queue: thresholds and resolve_match](https://github.com/ryansull/syncshelf/issues/14)
- [Goodreads acquisition design: starting, storing and reusing a session](https://github.com/ryansull/syncshelf/issues/25) (the observation contract)
- [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27)
- [Failure policy: retries, quarantine and partial runs](https://github.com/ryansull/syncshelf/issues/28)
- [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30)
- [Goodreads backfill: when it runs, from which feed, and what a field's first observation means](https://github.com/ryansull/syncshelf/issues/32)

**Builds**
- Clients: `clients/goodreads_rss.py` and `clients/goodreads_csv.py` (the parser, shared with `/ingest` and 4b).
- The engine:
  - `sync/observations.py`, `identity.py`, `matcher.py` (chain + candidates), `goodreads.py` (the diff);
  - `sync/runs.py` and `scheduler.py`;
  - `attention.py`.
- Endpoints and tools:
  - `ingest.py` (`POST /ingest/goodreads-csv`);
  - `tools/sync.py` (`sync_now`, `sync_status`, `retry_failed`) and `tools/matching.py` (`unmatched_books`, `resolve_match`, `rebaseline`).
- Upgrades to earlier phases:
  - `/healthz` gains `status`/`profiles` (§3.4a.10);
  - the read tools gain `attention`;
  - mirror refreshes start following merges (§3.4a.4).

**Tables:** `links`, `source_rows`, `match_candidates`, `baseline_fields`, `runs`, `change_log`, `schedules`, `ingest_batches`, `backfills` (`0004_sync`). The SQL-invariant tests for these tables ship here.

#### 3.4a.1 Goodreads shelf RSS contract

Verified live, anonymous, 2026-10-17.

- **URL:** `GET https://www.goodreads.com/review/list_rss/<user_id>?shelf=%23ALL%23&per_page=200&page=<n>`, plus `&key=<rss key>` only if the profile configures one. Both household profiles are public and need no key. **Never log the key.**
- **Paging:**
  - 100 per page by default, and `per_page` is honoured up to **200**. Above 200 it silently falls back to 100.
  - Pages are disjoint and concatenate exactly. A page past the end is HTTP 200 with a valid channel and **zero items**.
  - At least 1,000 items are reachable, with no depth cap seen.
- **Equivalent requests:** `shelf=%23ALL%23`, an empty `shelf=` and no `shelf` all return the same feed.
- **Order:** newest `user_date_added` first.
- **Headers:** `200 application/xml; charset=utf-8`, `Cache-Control: max-age=0, private`, `<ttl>60</ttl>`.
- **Usable page:**
  - HTTP 200;
  - parses as RSS with a `<channel>`;
  - the channel names the requested shelf.

  *Verify the `#ALL#` channel-title form on 4a's first live fetch, and fix the check to it.* Anything else is a **failed fetch, never "no books"**.
- **Hazards:**
  - **an unknown or misspelt shelf name returns the whole library**. SyncShelf only ever requests `#ALL#`;
  - **404 `text/html` "invalid user_id"** means no such user, so the run is `fetch_failed`;
  - a private profile's behaviour without `key` is unverified.
- **AWS WAF** is in front of Goodreads. A **202** with `x-amzn-waf-action: challenge`, or a **405** CAPTCHA, means stop. Treat it as an unusable page and never retry past it. None was seen in 21 requests.
- **Politeness:** `robots.txt` disallows `/review/list_rss` for `*`. The feed is a user-facing subscription link, so keep to a reader's rate: one paged pass per profile per run, **≥ 2 s between requests**, single-flight per profile, and an honest `User-Agent`.
- **Item fields** (side by side with the CSV in the findings):

  | Element | Form | Use |
  |---|---|---|
  | `book_id` | integer | **the source row key** (= CSV `Book Id`, the Goodreads **edition** id) |
  | `title` | series embedded, e.g. `Example Book (Example Series, #2)` | matching |
  | `author_name` | primary author only | matching |
  | `isbn` | ISBN-10 or empty (`X` occurs) | matching. **No ISBN13 in RSS.** |
  | `user_rating` | `0`–`5` integer, `0` = unrated | rating |
  | `user_read_at` | RFC 822 at `00:00:00 +0000`, or empty | date_read: **date part only, never timezone-shifted** |
  | `user_date_created` | RFC 822, Pacific offset | date_added (= CSV `Date Added`) |
  | `user_date_added` / `pubDate` | RFC 822, Pacific offset | the "last touched" stamp; the date source for an undated finish |
  | `user_shelves` | comma-separated; **`read` is omitted** | status via the shelf map; **no exclusive shelf present ⇒ `read`** |
  | `user_review` | HTML or empty | review |
  | `guid` | `/review/show/<review_id>` | not used as a key |
  | `book/num_pages`, `book_published`, cover URLs, `average_rating`, `description`, `user_name` | — | ignored (`user_name` is never logged) |

#### 3.4a.2 Goodreads library CSV contract

Verified on the first profile's export (28 rows). The same parser serves `/ingest` (4a) and the session export (4b).

- **Encoding:** UTF-8, **no BOM**, LF, trailing newline, RFC 4180 quoting. Reviews can make quoted cells span lines. Decode as UTF-8, never cp1252.
- **Header:** 23 columns. **Match by header name**, because Goodreads has inserted columns before:
  `Book Id, Title, Author, Author l-f, Additional Authors, ISBN, ISBN13, My Rating, Publisher, Binding, Number of Pages, Year Published, Original Publication Year, Date Read, Date Added, Bookshelves, Bookshelves with positions, Exclusive Shelf, My Review, Spoiler, Private Notes, Read Count, Owned Copies`.
  Header validation requires at least `Book Id, Title, Author, ISBN, ISBN13, My Rating, Binding, Date Read, Date Added, Exclusive Shelf, My Review, Spoiler, Read Count` [assembly choice for the required set].
- **Column rules:**
  - **`Book Id`:** a plain integer, the edition id. Not `="…"`-wrapped.
  - **`ISBN` / `ISBN13`:** wrapped as `="…"`; an empty one is `=""`. Keep ISBN-10 as a string (`X` occurs).
  - **`My Rating`:** `0` = unrated, a rated value is `N.0`. Parse as float, then int.
  - **`Date Read`, `Date Added`:** `YYYY/MM/DD`. `Date Read` is **mostly empty** (1 of 28 rows set).
  - **`Exclusive Shelf`:** **the authoritative status**. `Bookshelves` omits `read`.
  - **`Read Count`:** `1` for **read and currently-reading**, `0` for to-read. It counts "started", not "finished".
  - **`Binding`:** CSV-only, and the only format signal (Kindle Edition, Hardcover, Paperback, Mass Market Paperback, …).
  - **`My Review`:** expect HTML and embedded newlines. *Unverified:* the sample has no reviews.
  - **`Spoiler`:** *unverified* value form; it was empty in the sample. Normalise empty to false, anything else to true, and confirm on the first CSV that has a spoiler review.
  - `Private Notes` and `Owned Copies` are not carried.
- **Kindle rows:** 12 of 14 have **no ISBN at all**, none has an ASIN, and 9 of 10 Kindle reads have no `Date Read`. The `Book Id` is the only reliable key.
- **Audible doesn't push to Goodreads.** Audiobook finishes come from ABS. Any Goodreads audiobook row is a manual entry.

#### 3.4a.3 The observation contract

From [Goodreads acquisition design](https://github.com/ryansull/syncshelf/issues/25). Each run produces one **observation batch** per profile.

- **`source`:** `gr_rss`, `gr_csv_auto` (4b) or `gr_csv_ingest`.
- **`observed_at`:**
  - RSS: the fetch start time.
  - Automated CSV (4b): the generation time.
  - `/ingest`: the optional `generated_at` the uploader states. **Without it, the batch is marked `stale`**, and a stale batch **only fills** (§3.4a.8).
- **`complete`:**
  - a CSV: always true when its header validates;
  - RSS: true only if every page was usable **and** paging ended on an empty page.
- **Rows** are keyed by the Goodreads edition `book_id`, with these normalised fields:

| Field | CSV | RSS |
|---|---|---|
| status | `Exclusive Shelf` | `user_shelves` through the per-profile shelf map; no exclusive shelf present ⇒ `read` |
| rating (null when 0) | ✓ | ✓ |
| date_read | ✓ | ✓ (`user_read_at`) |
| date_added | ✓ | ✓ (`user_date_created`) |
| last_touched | — | ✓ (`user_date_added`) |
| review (plain text) | ✓ | ✓ (HTML → text) |
| spoiler | ✓ | — |
| shelves (non-exclusive) | ✓ | ✓ |
| title, author, isbn10 | ✓ | ✓ |
| isbn13, binding → format, read_count | ✓ | — |

**A field a feed doesn't carry is *not observed*, never *cleared*.** The diff never treats RSS's missing `read_count`, `spoiler` or format as a change.

**Binding → format** (confirmed in [#32](https://github.com/ryansull/syncshelf/issues/32); a code constant, not config):
- `Kindle Edition`, `Nook`, `ebook` (case-insensitive contains) → `ebook`;
- `Audiobook`, `Audible Audio`, `Audio CD`, `MP3 CD`, `Audio Cassette` → `audio`;
- anything else → `print`. An unrecognised Binding value is logged once per distinct value, so the table can grow.

#### 3.4a.4 Identity

From [Canonical book identity across Goodreads, Hardcover, ABS and Chaptarr](https://github.com/ryansull/syncshelf/issues/12).

1. **The canonical entity is the Hardcover `book_id`.**
2. **Shared links, per-profile source rows.**
   - **Links** (`links`, keyed `(source, source_id)`) are facts about the world, stored once for both profiles.
   - **Source rows** (`source_rows`, keyed `(profile, source, source_id)`) carry the state (`linked | queued | ignored`), the `hc_book_id` that profile's writes target, its `hc_user_book_id` once known, and the baselines.
   - `gone_at` is a separate column, because a linked row can be gone.
3. **Formats.** One book per work, with the format on the link. An audio and an ebook ABS item of one title are two links to one book.
4. **Ids on a link.** `hc_book_id` is the key. `hc_edition_id` is recorded where the source names one (a `book_mappings` hit, or an exact ISBN/ASIN edition), and is overwritten if it changes.
5. **Authority: method ranking.** `human` > `gr_mapping` > `isbn13` / `isbn10` / `asin` > `fuzzy`.
   - A re-match replaces a link only with a **strictly stronger** method.
   - **A `human` link is never replaced.** This is enforced by the `links_human_sticky` trigger.
   - Hardcover alone decides whether a book still exists or has merged.
6. **Per-profile conflict.** The shared link is the default for a new source row. If a profile's own Hardcover library contradicts it (the importer put that profile's copy on a different book), **that profile's source row is queued** with `profile_conflict` and the shared link stays. `resolve_match` writes the shared link only when nothing contradicts it; otherwise it writes only that profile's source row.
7. **Invalidation.**
   - **Merges are followed.** Any Hardcover fetch (sync fetch, mirror refresh, hydrate) that sees a linked book with `canonical_id` set rewrites `links.hc_book_id` and `source_rows.hc_book_id` to the canonical book. This is logged in `change_log` (`kind = merge`), not queued.
   - **A vanished book is re-queued.** A `book_id` Hardcover no longer returns marks the link `stale` and queues the row (`stale`). It's detected when a by-id lookup (hydration, `resolve_match` validation, a merge check) returns no row [assembly choice for the detection point].
   - **A vanished source id is left alone** (an ABS rescan, a Goodreads edition swap). Its `last_seen_at` freezes, and the new id gets its own link.
   - **No periodic re-verify job.**
8. **Chaptarr is resolved live, and nothing about it is stored** (phase 6).
9. **The map-only backfill** (§3.4a.12).

#### 3.4a.5 The matcher

From [Matching and the unmatched queue: thresholds and resolve_match](https://github.com/ryansull/syncshelf/issues/14). The normalisation and scoring helpers land in phase 3; the chain lands here.

**The chain.** The first step yielding an accepted hit wins, and the step name is the link `method`. Every hit **follows `canonical_id`** before it counts. Lookups are batched with `_in` per run.

| Step | Goodreads row | ABS item (phase 5) |
|---|---|---|
| 1 | `gr_mapping`: `Book Id` → `book_mappings` (platform 1) | `asin`: `editions.asin` = item ASIN |
| 2 | `isbn13`: `editions.isbn_13` | `isbn13` / `isbn10`: the single `isbn`, normalised by length |
| 3 | `isbn10`: `editions.isbn_10` | `fuzzy` |
| 4 | `fuzzy` | |

This contradicts the originating prompt's ISBN-first order. The Goodreads mapping resolved 28/28 sample rows, and 12 of 14 Kindle rows have no ISBN.

- **A hit on a different edition is fine.** The edition is informational, and the **format comes from the source** (Binding, or the ABS media type), never from the matched edition.
- **Exact hits pass a loose title floor.** Accept an id hit only if the normalised title scores **≥ 60** against the source title. Below that, queue it as `id_title_mismatch` with the hit as the top candidate.
- **Fuzzy candidates** come from Hardcover `search` (Typesense is the only fuzzy path, since `_ilike` is 403):
  - `search(query: <normalised title>, query_type: "Book", per_page: 5, filter_by: "author_names:=[<primary author>]")`;
  - if that returns 0 hits, retry once without the filter;
  - hydrate the ids with one `books(where: {id: {_in: …}})`.
- **Scoring** (`rapidfuzz`):
  - **title** = `max(token_sort_ratio(full), token_sort_ratio(title before ':'))`;
  - **ties with adaptations:** when the top hit's **full** title scores 100, the margin is checked on **full-title** scores, so a runner-up that only reaches the threshold through the part before `:` (`… : A Graphic Novel`) doesn't make it ambiguous. Decided in [Matcher: the before-':' title score ties a book with its adaptations](https://github.com/ryansull/syncshelf/issues/138);
  - **author** = the best `token_sort_ratio` of the source's primary author against each candidate `author_names` entry.
- **Normalisation**, the same function on both sides, in this order (decided in [Matcher normalisation: the stated step order makes steps 6 and 7 no-ops](https://github.com/ryansull/syncshelf/issues/105)):
  1. NFKD, then strip diacritics;
  2. casefold;
  3. `&` → `and`;
  4. strip a trailing Goodreads series parenthetical `(… #N)`;
  5. **author names only:** turn `Last, First` into `First Last` (on a title it would swap any single-comma title);
  6. drop punctuation, collapse whitespace;
  7. drop a leading `the|a|an`.

  The series parenthetical and the comma must be seen before punctuation is dropped.
- **Fuzzy auto-accept** (outside the backfill only):
  - title **≥ 95**, author **≥ 90**, **and** the runner-up's title score at least **10** points lower;
  - otherwise queue as `fuzzy_low` or `fuzzy_ambiguous`, keeping candidates with title ≥ 60 (at most 5).

  `fuzzy` is the weakest method, so any later exact hit replaces it.
- **The thresholds (60 / 95 / 90 / 10) are starting values, not measured ones.** They're calibrated, then pinned (tests).
- **What a queue entry holds.** A queue entry is a `source_rows` row in state `queued`, plus:
  - `queue_reason`: `no_hit | id_title_mismatch | fuzzy_low | fuzzy_ambiguous | not_in_library | profile_conflict | stale | ambiguous_mapping` (`ambiguous_mapping`: the Goodreads id's `book_mappings` rows point at different books after `canonical_id`, each kept as a candidate; decided in [Matcher: one Goodreads id with several book_mappings rows](https://github.com/ryansull/syncshelf/issues/185));
  - `queued_at`;
  - `match_input` (JSON: title, authors, ISBNs, ASIN, Goodreads `Book Id`, format, year, as seen);
  - up to 5 `match_candidates` (`hc_book_id`, `hc_edition_id?`, method, title score, author score, rank, `fetched_at`);
  - **its baseline, kept as usual**, so the pending diff is always computable.
- **Re-matching happens only on a change or on request.** A queued row is re-matched when its source row changes (new fields, a new ISBN) or on `unmatched_books(refresh=true)`. There's no background sweep.
- **Ignored is sticky and reversible.** An ignored row is never matched, queued or pushed again, even if it changes. `resolve_match` with a real id revives it.
- **Cost:** at most 2 searches + 1 hydrate per new unmatched row. The id steps are batched.

#### 3.4a.6 The Goodreads diff

From [Goodreads to Hardcover diff: what 'Goodreads changed it' means](https://github.com/ryansull/syncshelf/issues/13), as amended by [Audiobookshelf to Hardcover: what counts as finished](https://github.com/ryansull/syncshelf/issues/16) (decision 5: ±14-day adoption) and [Reading history questions](https://github.com/ryansull/syncshelf/issues/17) (decision 3: an edition on every read).

**The rule:** SyncShelf diffs each Goodreads source row against **its own per-field Goodreads baseline**, never against Hardcover. A changed field is the only thing that causes a Hardcover write.

1. **The baseline is per field and stores values.** One `baseline_fields` row per (source row, field), for `status`, `rating`, `date_read`, `review`, `spoiler` and `read_count`. Each holds the normalised value as `TEXT` with its own `observed_at`. Nothing is hashed, so logs show the change itself (`rating 0→4`).
   - **status:** the exclusive shelf goes through `shelf_map` (`read`→3, `currently-reading`→2, `to-read`→1, plus configured custom shelves such as a DNF shelf→5). **An unmapped exclusive shelf is logged and its status isn't pushed.**
   - **rating:** an integer 0–5, where 0 means null.
   - **date_read:** an ISO date or null.
   - **review:** HTML normalised (`<br>` → newline, entities decoded, other tags stripped, whitespace collapsed), then compared. It's written as `review_markdown`, with `spoiler` → `review_has_spoilers`.
2. **Fields carried:** status, rating, date read, review and spoiler (plus `read_count` as a re-read signal). **Not carried:** non-exclusive shelves/tags, private notes, owned copies.
3. **First sighting.**
   - Rows present at the backfill take their observed values as the baseline, so **the backfill pushes nothing** (§3.4a.12).
   - A row first seen **after** the backfill starts from an **empty baseline** (`observed_at NULL`), so in **that first observation** every non-empty field is a change and is pushed.
   - **A field's first-ever observation is otherwise a baseline, never a change** ([#32](https://github.com/ryansull/syncshelf/issues/32), decision 4). On a row SyncShelf has already seen (every backfilled row included), a field that a feed carries for the first time (`read_count`, `spoiler` and Binding from the first CSV after RSS) only sets its baseline. So the first CSV after an RSS backfill pushes nothing. `spoiler` has no exception: a spoiler review written in the RSS window misses the flag until that review next changes.
   - The baseline is recorded **whatever the link state**. A `queued` row keeps one, and once resolved, its pending diff applies.
4. **Removals are recorded and never pushed.** Only a **complete enumeration** may conclude that a row is gone. The source row gets `gone_at`, its `last_seen_at` freezes, a `change_log` row (`kind = gone`) is written, and it's counted in `sync_status`. A partial, filtered or empty feed concludes nothing about absence. A move to another shelf is a status change, not a removal.
5. **Clears.** Rating → 0 clears the Hardcover rating, and an emptied review clears the review. **A vanished Date Read is never pushed**, because reads are history and missing dates are normal Goodreads noise. The baseline still updates in every case.
6. **Reads and re-reads.** The diff never deletes reads, and touches only reads it created or matched by date.
   - **A new read** only on an explicit signal: `read_count` **rises from an observed value** (N → N+1, CSV), or the status is seen going **Read → Currently Reading → Read**. `read_count` never counts on its first observation, even on a brand-new row: a new row first seen as Read, with any `read_count`, inserts **exactly one** read, from the status change ([#32](https://github.com/ryansull/syncshelf/issues/32), decision 5). That shows up as baseline `currently-reading`, `prev_value = read`, and a new observation `read`.
   - **Any other Date Read change is a correction.** It updates the read whose `finished_at` equals the old baseline date. If none matches, it inserts a read, unless an **adoptable read** exists.
   - **An undated finish** (normal for Kindle) gets the date part of RSS `user_date_added`, or failing that the date SyncShelf first saw the transition (`pending_since`). The `change_log.date_source` records which. A Currently Reading start date follows the same rule.
   - New reads carry an edition (§3.3.5).
7. **Writing:** through the write procedure (§3.3.6). Its read rules are the ones above: adoptable reads (±14 days, or an open read) for inserts, and corrections by exact old date.
8. **CSV vs RSS: the newer observation wins, field by field.** An observation applies to a field only if its feed **carries** that field **and** it's **newer than that field's baseline `observed_at`**. A field missing from a partial feed is never read as cleared. A CSV's `observed_at` is its **generation** time, so a stale export can't roll back a change RSS already pushed.
9. **Commit timing.**
   - Write to Hardcover first. Then advance that field's baseline (`value`, `observed_at`, `prev_value`) and write the `change_log` row (`kind`, `field`, `old_value`, `new_value`, `trigger`, `date_source`) in **one SQLite transaction**.
   - After a crash, the next run re-diffs and re-applies. Status, rating and review writes are idempotent, and read inserts adopt first, so a replay never duplicates.
   - A failed write (including HTTP 200 with `error`) leaves the baseline untouched.
10. **Claude edits never touch a baseline** (§3.3.9). The last writer wins.
11. **Privacy:** inserts use `hardcover_privacy` (default `private`). Updates never change privacy.

**Pending-change mechanics** on `baseline_fields`:
- When an observation differs from `value`, set `pending_value`, and set `pending_since` if it's unset.
- If a **new** source value arrives that differs from `pending_value`, it supersedes: `pending_value` and `pending_since` are reset, and `attempts`, `first_failed_at`, `last_error*` and `quarantined_at` are cleared.
- On success, the pending columns are cleared along with the advance.
- If the observation comes back equal to `value`, the pending change is cleared (superseded).
- A status change and its read edits are **one** change, carried on the `status` field.

#### 3.4a.7 Runs and the failure policy

From [Failure policy: retries, quarantine and partial runs](https://github.com/ryansull/syncshelf/issues/28) and the cadence ticket.

**A run has four stages:**
1. **Fetch the source**, in full: RSS pages to the empty page, or the CSV is the whole library.
2. **Diff against baselines.** This needs no Hardcover data.
3. **Fetch the Hardcover library** (§3.3.1), **only if** there is something to push, a row to match, or a **pending change** that isn't quarantined. An **idle run costs 0** Hardcover requests. The fetch also refreshes the mirror, and write decisions use **only this same-run fetch**.
4. **Apply** each change through §3.3.6 under the profile's apply lock, committing each with its baseline.

**The run log.** `runs` gets a row at the start (`running`), and the row ends as one of:

| State | Meaning |
|---|---|
| `ok` | Fetched and applied, possibly zero changes. |
| `partial` | The fetch was incomplete (e.g. an unusable RSS page), or some changes failed. |
| `fetch_failed` | Nothing usable was fetched. |
| `deferred` | The Hardcover library fetch failed, or the daily reserve was hit, so nothing was applied. |
| `aborted` | The process died. Every row still `running` at startup becomes `aborted`. |

- The row records counts of changes `applied`, `failed` and `quarantined`, plus `first_error` (scrubbed).
- `runs` rows are written for `gr_rss`, `gr_csv` and `abs`. The `mirror` and `chaptarr_watch` values in the CHECK are reserved: mirror refreshes are recorded in `mirror_state`, and the watcher in `chaptarr_requests` [assembly choice].
- **There's no wall-clock deadline.** A run paused on a 429 just waits. Hangs are guarded by per-request HTTP timeouts.
- **Nothing is ever rolled back.**

**Error classes and retries within a run:**

| Class | Examples | Within the run | Afterwards |
|---|---|---|---|
| transient | HTTP 5xx, timeout, network error | retried **twice**, after 2 s and 8 s, then left pending | next run (a timeout may have landed; convergence makes the replay safe) |
| permanent | HTTP 200 with `error`; any 4xx other than 429 | no retry | next run |
| bug | `403 request_exceeds_capacity` | never retried | **quarantined at once**, surfaced |
| — | 429 | not a failure: the limiter pauses | — |

**Quarantine** is time-based:
- A change is quarantined once it has failed for **≥ 24 h and ≥ 3 attempts**. Transient and permanent errors share this rule.
- A quarantined change doesn't trigger the lazy Hardcover fetch.
- **Release** happens on any of:
  1. a new source value that differs from `pending_value`;
  2. **a new SyncShelf version**, since startup releases everything once when `meta.last_started_version` differs;
  3. `retry_failed(change_id | "all")`.

**Source-fetch failures:**
- **What was fetched is applied.** An RSS pass with an unusable page is a `partial` run with `complete = false`, which never concludes removals.
- **Nothing usable** (the first page unusable, a 404 user, a CSV failing header validation) makes the run `fetch_failed`, with no diff.
- **"No changes" is distinct:** an idle run is `ok` with zero changes, not `fetch_failed`. `sync_status` shows `last_success_at` separately from `last_run_at`.

**Backoff** applies to fetch failures only (`fetch_failed`, or a `partial` caused by the fetch). It's stored in `schedules.consecutive_failures` / `backoff_until`.

| Source | Curve | Notes |
|---|---|---|
| Goodreads RSS | 1 h → 2 h → 4 h, **capped at 6 h** | |
| ABS (phase 5) | 15 min → 30 min → 1 h, capped at 1 h | |
| CSV (4b) | retries the next night window | WAF stickiness also applies |

- Backoff resets on the first success.
- **A failed Hardcover library fetch** makes the run `deferred` with **no backoff**. The next scheduled run tries again.

#### 3.4a.8 `POST /ingest/goodreads-csv`

From [The front door](https://github.com/ryansull/syncshelf/issues/11) and [Deployment topology](https://github.com/ryansull/syncshelf/issues/20). This is the **LAN-only manual fallback**, whatever state the Goodreads session is in.

- **Auth:** each profile has its own ingest secret, and **the secret identifies the profile**, so the request carries no user field. Compare in constant time against each profile's secret. It's sent as `Authorization: Bearer <ingest secret>` [assembly choice for the header]. Not covered by MCP auth.
- **Body:** `multipart/form-data`, with the CSV as a **file part** named `file` (Starlette's `max_part_size` limits non-file fields only), plus an optional `generated_at` field (ISO-8601, the export's generation time) [assembly choice for the field names].
- **Handling:**
  1. Validate the header (§3.4a.2).
  2. Store the batch in `ingest_batches`: `source = gr_csv_ingest`, `observed_at = generated_at` or NULL, `stale = generated_at absent`, `complete = 1`, and the payload as received. Keep the last 3 per profile.
  3. Nudge the profile's Goodreads loop (`trigger = 'ingest'`).
  4. Return **202** `{"batch_id": …, "run": "started" | "already_running", "stale": bool}`. When `stale` is true, the body also suggests resending with `generated_at`. The client never waits on a sync.

  Errors are `401` (unknown secret) and `400` (not a valid CSV, with the missing columns named).
- **Schedule effect:** a **dated** `/ingest` CSV counts as a CSV success and pushes the CSV due date out (4b).
- **A stale batch only fills** ([#32](https://github.com/ryansull/syncshelf/issues/32), decision 9). It may:
  - set baselines for **never-observed** fields on **existing** rows (no push, per diff rule 3);
  - refresh match inputs (ISBN13, Binding), re-matching queued rows whose input changed.

  It **never** creates rows, pushes, marks rows gone, counts as the backfill, or moves the CSV due date. Under "newer observation wins" it could never beat an observed baseline anyway.

#### 3.4a.9 Scheduler and cadence

From [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27) and [Hosting the MCP server…](https://github.com/ryansull/syncshelf/issues/4).

**Intervals.** Per profile, every interval configurable.

| Loop | Interval | Cost per run | Phase |
|---|---|---|---|
| Goodreads RSS `#ALL#` | **60 min** | about 9 requests for 1,500 books, ≥ 2 s apart | 4a |
| Goodreads CSV | due **5 days** after the last CSV success, run in the next **02:00–05:00** window in the profile's timezone | the export flow | 4b |
| ABS | **15 min** | 1 `GET` | 5 |
| Chaptarr pending watcher | **2 min**, only while a request is `pending` | `pendingauthorimport` + rows | 6 |

**Loops and locks:**
- Each loop is a supervised asyncio task: `while True: run_once(); sleep(interval + jitter)`.
- Each run is wrapped in `try/except` with logging, so one failure never kills the loop. Shutdown cancels the task.
- **One lock per (profile, source).** A scheduled tick that finds the lock held **skips** and logs it.

**Startup resumes, never bursts:**
- Each loop's next run is `schedules.last_run_at + interval` (or `next_due_at`), plus up to 60 s of jitter.
- An overdue loop runs **once**, staggered, never as a catch-up burst.
- The two profiles are **offset by half an interval** per source, so their Goodreads traffic (same IP) never overlaps.

**`sync_now`:**
- If a run is going, it returns "already running since T, run id R" at once.
- Otherwise it starts the run as a task held by the sync object (not tied to the HTTP request), and returns the run id.
- Claude reads the result through `sync_status`.

**Sources are pulled in full**, with no cursors. Hardcover is fetched lazily (§3.4a.7).

#### 3.4a.10 Attention, `sync_status` and `/healthz`

From [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30) and [Failure policy](https://github.com/ryansull/syncshelf/issues/28) (decision 9).

**Attention items** (`attention.py`). These are computed from state on every read, for the caller's own profile:

| `code` | Kind | Raised when | Clears when | `action` | Phase |
|---|---|---|---|---|---|
| `quarantined` | state | ≥ 1 quarantined change | none left | `sync_status` → `retry_failed` | 4a |
| `hardcover_bug` | state | a quarantined change has `last_error_class = bug` | that change is released | `sync_status`; report it as a bug | 4a |
| `source_stale` | state | an enabled RSS/ABS source has no success for > 24 h, or the CSV none for > 7 days | the next success | `sync_status` | 4a (CSV clause from 4b) |
| `goodreads_session` | state | the session is `needs_sign_in`, `challenged` or `bad_credentials` | the state leaves those | `goodreads_sign_in`, or for `challenged` "export the CSV by hand to /ingest" | 4b |
| `chaptarr_pending_long` | state | a request has been `pending` > 24 h | it lands or expires | none: wait, or resolve it in Chaptarr | 6 |
| `chaptarr_contained` | event | a request is `contained` and `acknowledged_at IS NULL` | `dismiss_attention(id)` | check the author in Chaptarr, then dismiss | 6 |
| `chaptarr_expired` | event | a request `expired` at 7 days and `acknowledged_at IS NULL` | `dismiss_attention(id)` | re-request or dismiss | 6 |

**Kept out of `attention`:** mirror refresh errors, single failed runs, recovered transients and **the unmatched queue**. A book needing a match is normal ambiguity, while quarantine means sync is broken. They show only in `sync_status`.

**`sync_status(profile="me")`** gives the caller's own profile in full detail. Sections for phases not yet shipped are **omitted**, not null.

```
{ profile, now, version,
  attention: [{code, message, action, id?}],
  sources: { gr_rss | gr_csv | abs: {
    enabled, last_result, last_run_at, last_success_at, last_observed_at,
    next_due_at, consecutive_failures, backoff_until,
    backfill: {state: pending|done, done_at?, rows_linked?, rows_queued?},
    recent_runs: [{id, trigger, state, started_at, duration_s,
                   applied, failed, quarantined, first_error}] }},
  goodreads_session: {state, since, detail, action},                  # 4b
  mirror:    {as_of, last_attempt_at, last_error},
  hardcover: {limiter: ok|paused|reserve, paused_until, daily_remaining},
  quarantine: {total, items: [{change_id, hc_book_id, title, field, old, new,
                               last_error, error_class, first_failed_at, attempts}]},
  unmatched: {total, by_reason, oldest_queued_at},
  gone: {total},
  chaptarr:  {pending, items: [{id, hc_book_id, title, format, state,
                                requested_at, attention}]} }       # 6
```

- **`recent_runs`:** the last 3 runs per source, plus every non-`ok` run in the last 24 h, capped at 20.
- **`quarantine.items`:** up to 25, with an exact `total`. Titles come from the mirror.
- **`review` values** show as length plus the first 40 characters.
- **Every error string** passes through the host-alias rewrite and the secret scrub.
- **`gone.total`** counts rows with `gone_at` set ([Goodreads to Hardcover diff](https://github.com/ryansull/syncshelf/issues/13), decision 4) [assembly choice for its placement].

**`sync_status(profile="household")`** is the **health view**. Every profile may see it for every profile, **regardless of `household_visible`**, because it's operational data.
- Per profile: `{profile, attention: [codes], sources: {<src>: {last_result, last_success_at}}, goodreads_session: {state}}`.
- **No titles, values, error strings or book-naming counts.**

**`/healthz`** (LAN-only, unauthenticated, titles-free):
- **200** whenever the process is up and `SELECT 1` succeeds. Body: `{"status": "ok" | "attention", "version", "profiles": {<name>: [<codes>]}}`.
- **503** only when the DB check fails.
- Docker's `HEALTHCHECK` uses it for liveness, so a CAPTCHA never restarts the container. Uptime Kuma or Home Assistant alert on `status`. **SyncShelf sends no notifications itself.**

#### 3.4a.11 Tools

| Tool | Contract |
|---|---|
| `sync_now(source: "goodreads" \| "abs" \| "all" = "all", full: bool = False)` | Starts a run for the caller's profile, or reports the one in progress (§3.4a.9). `goodreads` runs RSS. `full=true` also forces a CSV export (4b; before 4b it's a tool error naming the missing phase). `abs` and `all` include ABS from phase 5. It returns run ids, never waits for completion, and the result is read through `sync_status` [assembly choice for the parameter forms]. |
| `sync_status(profile: "me" \| "household" = "me")` | §3.4a.10. |
| `retry_failed(change_id: int \| "all")` | Releases the caller's own quarantined change(s) (clears `quarantined_at`, `attempts`, `first_failed_at`) and queues an immediate run of the affected sources. |
| `unmatched_books(source?: "goodreads" \| "abs", include_ignored=false, refresh=false)` | The caller's queue. Each entry has an opaque `entry_id` (`goodreads:<Book Id>`, `abs:<libraryItemId>`), source, reason and `queued_at`; the input as seen (title, author, ISBN/ASIN, format); the **pending diff in words** ("would mark Read, finished 2026-03-02, rate 4"); and candidates with title, subtitle, authors, release year, Hardcover URL, scores and method. `refresh=true` re-runs matching on the listed entries first. Claude can always propose a book that isn't a candidate, found through `search_books`. |
| `resolve_match(entry_id, hardcover_book_id: int \| None)` | **With an id:** (1) validate that the book exists and follow `canonical_id`; (2) write a `human` link (shared unless the profile's library contradicts it) and set the row `linked`; (3) **apply that one row's pending diff immediately** (for a backfill-queued row, see §3.4a.12), under the source's lock and the apply lock, without a `runs` row (its `change_log` rows have `run_id = NULL`) [assembly choice]; (4) return what was pushed or what failed. On failure the link stays and the baseline doesn't advance. **With `None`:** set the row `ignored`. It works on **any** source row, including linked ones, to correct a wrong automatic link. The old user_book is **left in place and named** in the result ("Book X still has a user_book from the old link"). SyncShelf never deletes it on its own. Claude can remove it with `remove_book` (§3.3.9), after the user confirms. |

| `rebaseline(source: "goodreads" \| "abs", confirm?: str)` | **Preview + confirm token** (the `remove_book` pattern, §3.3.9), on the caller's own profile. It re-runs the backfill on that source: backfill matching (§3.4a.12) on queued rows and rows without a link (human links and `ignored` rows are kept), then every baseline of the source is reset to its latest observation, dropping pending changes. The preview counts what would happen ("12 rows re-match, 3 pending changes would be dropped"). The confirmed call rewrites the `backfills` row and **pushes nothing**. It's for a backfill that ran before the hand import into Hardcover finished. `goodreads` from 4a, `abs` from phase 5. |

`attention` is added to all six read tools' results.

#### 3.4a.12 The map-only backfill

From [identity](https://github.com/ryansull/syncshelf/issues/12) (decision 8), [diff](https://github.com/ryansull/syncshelf/issues/13) (decision 3), and [Goodreads backfill: when it runs, from which feed, and what a field's first observation means](https://github.com/ryansull/syncshelf/issues/32).

**What it is:**
- The **initial Goodreads → Hardcover import is done by hand** with Hardcover's own importer, and is out of scope. SyncShelf's map-only mode **reads Hardcover and never writes**.
- It populates links and source rows from each profile's existing Hardcover library plus the Goodreads input.
- **Linked automatically** only when the Goodreads `Book Id` resolves through `book_mappings`, or an exact ISBN-13/ISBN-10 resolves to an edition, **and** the resulting book (after `canonical_id`) is **in that profile's Hardcover library**.
- **Queued:** a mapping to a book not in the library (`not_in_library`), any title/author-only candidate, or no hit. **There are no fuzzy auto-links during the backfill.** Rows it queues carry `source_rows.backfill_queued = 1`.
- Backfilled rows take their observed values as the baseline, so **the backfill pushes nothing**.
- **SyncShelf never creates reads for books already Read at backfill.** Backfilling old dates is out of scope. The one narrow exception is a user-resolved row, below.

**Trigger: automatic, on the first complete enumeration.**
- A profile's first Goodreads run whose batch is `complete` (§3.4a.3) **is** its backfill. That's either RSS `#ALL#` with every page usable and paging that ended on an empty page, or a **dated** CSV. A stale `/ingest` batch never counts.
- Runs before that (incomplete RSS) store their batch and push nothing. 4a needs no manual onboarding step.
- **Either feed suffices.** The backfill doesn't wait for a CSV, because the first-observation rule (§3.4a.6, rule 3) makes the first later CSV push nothing.
- Completion is recorded in `backfills` (`profile`, `source`, `done_at`, `run_id`, `rows_linked`, `rows_queued`). Until that row exists, `sync_status` shows the source as `backfill: pending`.
- **Re-run** with `rebaseline("goodreads")` (§3.4a.11).

**Resolving a backfill-queued row.** When `resolve_match` resolves a row with `backfill_queued = 1`, the flag is cleared and:
- **if the profile has no user_book for the chosen book**, the row is diffed against an **empty baseline**. Its status, rating and review are inserted (privacy per `hardcover_privacy`), plus a read **only when Goodreads has a real Date Read**, never an undated or invented one;
- **if a user_book already exists**, nothing is pushed: the link only. The baseline stays the observation.

This narrows the out-of-scope line above. A date the user stated, on a book the user explicitly adds, isn't a backfilled date, and every book already in the library still gets no historic read.

**ABS uses the same rules** (§3.5.2, rule 8). The ABS first sync is its backfill: exact ids only (ASIN/ISBN), in the profile's library, with no fuzzy auto-link. It's stamped in `backfills` with `source = 'abs'`, re-run through `rebaseline("abs")`, and it shares the first-observation and push-if-absent rules.

#### 3.4a.13 Tests

- **Golden `diff.yaml`:** every rule in §3.4a.6, including:
  - first sighting;
  - removals (complete vs partial vs empty);
  - clears, and a vanished Date Read;
  - re-read via Read Count and via the R→CR→R cycle;
  - corrections, and undated finishes with each `date_source`;
  - newer-wins across CSV/RSS, and a stale CSV not rolling back RSS;
  - commit-after-write and replay adoption;
  - a Claude edit untouched by an unchanged row, and last-writer-wins;
  - privacy on insert only;
  - the backfill cases: RSS backfill then first CSV (no push, no read from `read_count`); a new Read row with `read_count` 1 or 2 (one read); `read_count` N → N+1 (one re-read); `spoiler` first observed on an existing row (baseline); a stale batch (fills only, no rows, no gone, no due-date move); resolving a backfill-queued row with and without a user_book, and with and without a Date Read; `rebaseline` preview counts and keeping human/ignored rows;
  - RSS-only read edition: the linked edition's format, else no edition.
- **Golden `matcher.yaml`:** the chain order, `canonical_id` following, the title floor 60, and each fuzzy threshold and the margin, just above and just below.
- **Golden `attention.yaml`:** each code raised and cleared; dismiss on event vs state items; own-profile-only on household queries.
- **Matcher calibration** (`tests/live/calibrate_matcher.py`): run the real matcher over the Goodreads CSV in `local/`, read-only, against live Hardcover search through the limiter. Print every pair in the **near band** (title 80–98, author 80–95, or a margin under 15) for the user to label right or wrong. Set the thresholds from the labels, and commit the labelled pairs (public titles and ids only) into `matcher.yaml`.
- **RSS:**
  - paging to the empty page;
  - an unusable page → a `partial` run concluding **no removals**;
  - a 404 user → `fetch_failed`;
  - a WAF 202 treated as unusable;
  - the backoff curve.
- **The failure-policy suite** (fault injection):
  - replay converging (status ok, read fix-up failed, then a rerun);
  - a timeout that landed;
  - each error class's in-run retries;
  - quarantine at 24 h / 3 attempts, and each release path;
  - a crash leaving `running` → `aborted`;
  - quarantined changes not triggering a Hardcover fetch;
  - an idle run making **zero** Hardcover calls.
- **Identity:**
  - the sticky-human trigger, the state CHECKs, and method ranking (stronger replaces, never a human);
  - a merge followed on a mirror refresh;
  - a vanished book → `stale`;
  - a per-profile conflict queued for one profile only.
- **`/ingest`:**
  - secret → profile, and an unknown secret → 401;
  - a bad header → 400 naming the columns;
  - `generated_at` absent → stale, with `"stale": true` in the 202;
  - 202 without waiting;
  - last-3 retention.
- **Scheduler:**
  - resume from SQLite with jitter, overdue-once, half-interval offset;
  - `sync_now` while running, and a tick skipping while the lock is held.
- **Redaction:**
  - the household view and `/healthz` contain no titles or values;
  - stored error strings are scrubbed before insert.

**Done when:** for a test profile against the fakes, an RSS change (a Kindle finish, a rating, a review) reaches `FakeHardcover` with the right dates, edition and privacy. Partial and failed fetches never push removals. `sync_status` and `attention` report every failure path. The first real-account run is the backfill: it pushes nothing, and `sync_status` shows `backfill` done with link and queue counts.

---

### 3.4b Phase 4b: Goodreads session export

**Goal:** SyncShelf signs in to Goodreads itself, with each person's email and password, and runs the library CSV export over plain HTTP about every five days. The CSV is the only source of format, ISBN13 and Read Count. RSS keeps incremental sync going whatever the session state.

Decisions:
- [Goodreads session and library export: what can SyncShelf automate?](https://github.com/ryansull/syncshelf/issues/24) (findings comment: cookies, export flow, WAF, prior art)
- [Goodreads acquisition design: starting, storing and reusing a session](https://github.com/ryansull/syncshelf/issues/25)
- the cadence, failure-policy and observability tickets

Depends only on 4a.

**Boundary (out of scope on the map):** SyncShelf never solves, evades or works around a CAPTCHA, a bot check or a WAF challenge. It uses no stealth patches and changes no fingerprint. Where a sign-in can't complete honestly, it stops and flags, and a person exports the CSV by hand to `/ingest`. Writing to Goodreads is out of scope permanently.

**Prerequisites**
- Each profile's Goodreads **email and password** (both accounts have them) are in the stack env or secret files (`goodreads.email`, `goodreads.password_file`). With no password configured, the profile's session state is `disabled`. 4a still runs.
- The **Playwright sidecar**: the official `mcr.microsoft.com/playwright` image running `playwright run-server`.
  - It sits on an internal Docker network only, with no Traefik labels, no published ports and no volume.
  - `services.browser_ws` points at it.
  - The Python `playwright` client version is pinned in lockstep with the image tag.
- The account owner is reachable for the opening live test, in case Amazon sends a one-time code or a new-device approval.

**Opening step: an attended live test.** This is a scripted, attended sign-in per profile, run from the household server. It records:
- whether a challenge appears;
- the cookie set and the expiry of each cookie;
- the `POST /review_porter/export/<id>` response status and body;
- the `#exportFile` link-text timestamp format and its timezone;
- export generation time and CSV size.

Record the findings on the map. **If every sign-in is challenged, 4b ships with sign-in disabled and 4a carries Goodreads.** No decision on the map changes.

**Builds**
- `clients/goodreads_export.py`: the jar-backed HTTP client and the export flow.
- `sync/goodreads_session.py`: the sign-in state machine, the jar, and the sidecar connection. `playwright` is imported lazily, only when signing in.
- `tools/goodreads.py`: `goodreads_sign_in`, `goodreads_verify`.
- The `gr_csv` schedule.

**Tables:** `goodreads_sessions` (`0005_goodreads_session`). The cookie jar is a file, never in SQLite.

#### 3.4b.1 Goodreads sign-in and session contract

From the findings and anonymous observation, 2026-10-17. Nothing signed-in was observed; the opening test fills that in.

- **Every sign-in button goes through Amazon's authentication portal** hosted on Goodreads' own domain (`https://www.goodreads.com/ap/signin`), with `openid.pape.max_auth_age=0`, which asks for fresh authentication.
  - The **email form** has fields `ap_email` and `ap_password`, a **"Keep me signed in"** checkbox (`rememberMe`), and CAPTCHA markup.
  - Amazon can send **one-time-code** or **approve-this-sign-in** prompts, most likely on the first sign-in from a new device.
  - Google sign-in can't be automated (Google blocks automation-controlled browsers), and Apple needs a code on every new browser. Neither is used.
- **A session is a cookie bundle:**
  - the Rails `_session_id2`;
  - the **Amazon SSO set** scoped to `.goodreads.com`: `at-main`, `sess-at-main`, `session-token`, `x-main`, `ubid-main`, `session-id`, `session-id-time`, `sst-main`.

  `ccsid` and `aws-waf-token` are optional. **`jwt_token` is stripped** (it expires in minutes and breaks requests if stale).
- **Rotation.** `_session_id2` is regenerated from the Amazon cookies through a same-URL redirect that carries a `Set-Cookie`. The client must **apply `Set-Cookie` on every response, redirects included**, or the redirect repeats forever. **The long-lived credential is the Amazon set.**
- **Expiry signal:** a redirect to `/user/sign_in` or `/user/new`. **Session lifetime is unknown**; the opening test measures it.
- **AWS WAF:** a challenge is **202** with `x-amzn-waf-action: challenge`, and a CAPTCHA is **405** with `x-amzn-waf-action: captcha`. Both mean **stop**.

#### 3.4b.2 Signing in

- **The browser.** SyncShelf drives a **plain** Playwright Chromium through the sidecar.
  1. Seed a fresh context from the jar, so Amazon still sees the known device cookies (`ubid-main`, `session-id`) and is less likely to challenge.
  2. Fill `ap_email` / `ap_password` and tick `rememberMe`.
  3. Submit, and confirm it lands **signed in** on goodreads.com.
- **Outcomes:**

  | What appears | State | What happens |
  |---|---|---|
  | Signed in | `ok` | Harvest cookies for `goodreads.com` domains only (never `accounts.google.com`, `appleid.apple.com` or `amazon.com`), strip `jwt_token`, and write the jar. |
  | Approval prompt | `awaiting_approval` | The browser waits **up to 5 min** while the person taps Approve on their device. |
  | One-time code | `awaiting_code` | Claude asks the person, who passes it in through `goodreads_verify(code)`. This is the account owner doing their own 2FA. |
  | CAPTCHA, WAF 202/405, or any page SyncShelf doesn't recognise | `challenged` | **Stop.** Sticky: no unattended session use until a person runs `goodreads_sign_in()`. |
  | Password rejected | `bad_credentials` | Sticky and **never retried**, so it can't lock the account. |
- **Unattended re-sign-in.** When a scheduled export finds the session expired, SyncShelf may sign in again on its own, **at most once per 24 h per profile** (`last_auto_sign_in_at`), and only if **no prompt appears**. Any prompt abandons the attempt and sets `needs_sign_in`.
- **Session states** (`goodreads_sessions.state`): `ok` · `needs_sign_in` · `awaiting_approval` · `awaiting_code` · `challenged` · `bad_credentials` · `disabled`. `detail` is a scrubbed human-readable reason.

#### 3.4b.3 The cookie jar

- **File:** `/data/goodreads/<profile>.cookies.json`, mode **0600**, written atomically (temp file + rename).
- **Not in SQLite,** so a DB copy never carries a live session.
- **Not encrypted.** The password beside it in the stack env is the stronger credential, and a key from the same env would add nothing.
- **Leak guards:**
  - the jar is only ever sent to `https://www.goodreads.com`, never across a redirect to another host or to `http`;
  - cookie, CSRF and password values are never logged.
- **The jar is the only session state.** The sidecar is stateless.

#### 3.4b.4 The export

Plain HTTP with the jar, after a session exists:

1. `GET https://www.goodreads.com/review/import`. Read the CSRF token (`<meta name="csrf-token">`, or a hidden `authenticity_token`), the numeric user id, and any existing `div#exportFile` link: `<a href="/review_porter/export/<id>/goodreads_export.csv">Your export from MM/DD/YYYY - HH:MM</a>`. A redirect to `/user/sign_in` or `/user/new` means the session has expired.
2. **If an existing export is younger than 24 h, reuse it.** Otherwise trigger a new one:
   ```
   POST https://www.goodreads.com/review_porter/export/<user_id>
   X-CSRF-Token: <token>     X-Requested-With: XMLHttpRequest
   Content-Type: application/x-www-form-urlencoded
   Referer: https://www.goodreads.com/review/import     Origin: https://www.goodreads.com
   body: format=json
   ```
   - The response body is undocumented; the prior-art tools only check for a 2xx (or 302).
   - **Starting a new export deletes the previous file.**
   - One tool author reports a cooldown of a few days between exports, unverified. A non-2xx is logged with its status.
3. Poll `GET /review_porter/export/<user_id>/goodreads_export.csv` **every 10 s for up to 10 min**. It's 404 until ready.
4. `GET` the CSV, then parse it with the 4a parser (§3.4a.2).

- **Pacing:** ≥ 2 s between Goodreads requests, single flight per profile.
- **A WAF 202/405 at any step** sets `challenged` (sticky). RSS keeps running.
- **`observed_at`:**
  - the **trigger time** when SyncShelf triggered the export;
  - otherwise the `#exportFile` timestamp, read in the profile's `timezone` (confirm in the opening test).

  The batch is `gr_csv_auto`, complete when its header validates, and stored in `ingest_batches`.
- **Stability:** the export flow has been stable since at least 2018. The rest of Goodreads is migrating to Next.js. If the import page migrates, the HTML scraping (CSRF token, `#exportFile`) is what breaks.

#### 3.4b.5 CSV schedule

From [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27).

- **When it runs:** a CSV is **due 5 days after the last CSV success**, and runs in the next **02:00–05:00 window** in the profile's timezone. The due time is `schedules.next_due_at`, so restarts don't reset it.
- **Failure:** a failed export retries in the **next night window**, not sooner.
- **Other paths:**
  - a `/ingest` CSV counts as a success and pushes the due date out;
  - `sync_now("goodreads", full=True)` runs one immediately.
- **Staleness:** no CSV success for 7 days raises `source_stale`.

#### 3.4b.6 Tools

| Tool | Contract |
|---|---|
| `goodreads_sign_in()` | Starts an attended sign-in for the caller's own profile. It **returns at once** with the state (`ok` / `awaiting_approval` / `awaiting_code` / `challenged` / `bad_credentials`), and the browser keeps waiting in the background. Claude re-checks through `sync_status` or by calling it again. It also clears a sticky `challenged` or `bad_credentials` by trying afresh. |
| `goodreads_verify(code: str)` | Submits a one-time code to the caller's pending sign-in. |

`sync_status` gains `goodreads_session {state, since, detail, action}` and the `gr_csv` source. The `goodreads_session` attention item goes live.

#### 3.4b.7 Tests

- `FakeGoodreads` export flow: trigger, poll (404 → 200), download, and **24 h export reuse**.
- An expiry redirect → `needs_sign_in`, with re-sign-in at most once per 24 h.
- **A challenge → sticky `challenged`.**
- `bad_credentials` never retried.
- Cookie rotation applied across redirects.
- The jar is only sent to `https://www.goodreads.com`, and is written 0600 and atomically.
- **The CSV night window**, and that a restart keeps the due date.
- `/ingest` pushing the due date out.
- The sign-in state machine, against a fake browser driver.
- Sign-in is **never** automated in tests. The live check is the attended opening script.

**Done when:** after the opening test, a profile with credentials runs an unattended night-window export, and it reaches Hardcover through the 4a engine. Every failure state shows in `sync_status` and `attention` with the right action.

---

### 3.5 Phase 5: Audiobookshelf

**Goal:** finished and started audiobooks and ebooks in each person's ABS reach Hardcover through the 4a engine (`source = 'abs'`). Audio and ebook count the same.

Decisions:
- [Audiobookshelf API: per-user progress and finished status](https://github.com/ryansull/syncshelf/issues/9) (findings comment)
- [Audiobookshelf to Hardcover: what counts as finished](https://github.com/ryansull/syncshelf/issues/16)
- the matching ticket (decision 6, widened by #16), the cadence, failure-policy and testing tickets

**Entry gate: ABS live re-verification.** The live ABS had **no libraries** at research time, so every item, progress and search shape below is **source only** (ABS `v2.37.1`). Before building, once the audiobook and Chaptarr ebook libraries exist:

1. **Create a non-root ABS user and an API key per profile.** The research key belongs to root. Put each key in `abs.token`.
2. **Re-run the read-only shape checks:**
   - `GET /api/libraries`;
   - one `GET /api/items/<id>?expanded=1&include=progress` each for an audiobook and an ebook;
   - `GET /api/libraries/<id>/search?q=…`;
   - the filtered `items` call;
   - an **ASIN/ISBN count** over `items?minified=1`.
3. **Count how often an ABS ASIN hits Hardcover `editions.asin`,** and how often `book_mappings` platform 32 (Audible) would. Record the numbers. The matcher chain stays as decided, and any proposal to add a step goes to a new ticket.
4. **Check ebook finishing,** once real EPUBs exist: where `ebookProgress` ends on a finished read (to confirm the 0.97 bar), and whether the ABS mobile apps set `isFinished` for ebooks.
5. **Re-record the ABS fixtures,** drop their `source-only` mark, and run the matcher calibration over ABS items.

If any check contradicts a decided rule, open a ticket and leave that rule unbuilt.

**Builds:** `clients/abs.py`, `sync/abs.py` (the ABS diff engine), the `abs` schedule, and ABS in `sync_now` / `sync_status`. **No new tables.**

#### 3.5.1 ABS API contract

- **Version and auth** (verified live): `2.37.1`, local auth.
  - The token is an **ABS API key**: a JWT with claims `type: "api"`, `keyId`, `name`, `iat`, and **no `exp`**. Send it as `Authorization: Bearer <key>` over **HTTPS** (the `http` URL 302s).
  - ABS resolves the key to its owner and **acts strictly as that user**. Key `permissions` aren't enforced, and `lastUsedAt` is never written.
  - Revoking means deactivating or deleting the key in ABS.
  - **Not used:** login JWTs (1 h / 30 d refresh) and the deprecated legacy user token.
- **`GET /api/me/progress` → `{"mediaProgress":[…]}`** (empty envelope verified live; row shape source only). It returns every progress row for the key's user, across every library, in one request. **This is the only call each run makes.** Rows look like this:
  ```jsonc
  { "libraryItemId": "<id>",           // the source_id
    "mediaItemType": "book", "episodeId": null,
    "duration": 41234.5,               // audio seconds; 0 for ebook-only items
    "progress": 0.42,                  // audio fraction 0..1
    "ebookProgress": 0.37,             // ebook fraction 0..1
    "isFinished": false,
    "finishedAt": null,                // ms epoch, or null
    "startedAt": 1791000000000,        // ms epoch (row createdAt)
    "lastUpdate": 1792000000000 }      // ms epoch (row updatedAt; may be device-backdated)
  ```
  - There's **one row per (user, item)**, carrying audio and ebook progress together.
  - The same title in the audio and ebook libraries gets **two independent rows**.
  - Rows whose item was removed from ABS stay in the list; tolerate them.
- **Item metadata for matching** (source only), fetched **only for rows newly reaching ≥ 5 % or finished**, and cached in `source_rows.match_input`: `GET /api/items/<libraryItemId>?expanded=1` → `media.metadata`:
  - `title`, `subtitle`;
  - `authors[{name}]`, `narrators[]`, `series[{name, sequence}]`;
  - a single `isbn` and a single `asin` (each a string or null);
  - `publishedYear`.

  Also used: `media.numAudioFiles`, `media.duration`, `media.ebookFile`. **Format:** `audio` if `numAudioFiles > 0`, else `ebook` [assembly choice; confirm at the entry gate]. **Never store or log** `path` / `relPath`.
- **How finishing works** (source only):
  - **Audio** finishes automatically when under 10 s remain (`markAsFinishedTimeRemaining`), or past `markAsFinishedPercentComplete` if that's set.
  - **"Mark as finished"** sets `isFinished` in any client. The ABS **web ebook reader never sets `isFinished`**.
  - **Un-finishing** (marking unfinished, or scrubbing back from the end) clears `finishedAt`.
  - `lastUpdate` and `finishedAt` can be **device-supplied** (offline clients backdate them).
  - Deleting progress deletes the row.
- **Not useful for sync:** `items-in-progress` (excludes finished books), `listening-sessions` (no finished flag; ebooks create none).
- **Rate limits:** API reads have none. The only limiter covers the login, refresh and password routes. GETs take 40–70 ms live.
- **Never call the mutating `GET`s:** `…/remove-from-continue-listening` and `…/readd-to-continue-listening`.
- **Never log** raw `/api/me` or `/api/users` bodies, which contain the legacy user token.

#### 3.5.2 The ABS → Hardcover rules

From [Audiobookshelf to Hardcover: what counts as finished](https://github.com/ryansull/syncshelf/issues/16).

**Terms:**
- **progress** = `max(progress, ebookProgress)`;
- **started** = progress ≥ **0.05**;
- **finished** = `isFinished` **or** progress ≥ **0.97**.

1. **Finish rule.** One rule covers both formats, so audio and ebook are equivalent. The threshold is what makes ebook finishes work, and it also catches audiobooks stopped during the closing credits. Books with long back matter need a manual "Mark as finished".
2. **Dates** become a local **date** in the profile's `timezone` (only dates are written, never times). Device dates are accepted as they are.
   - **Finish date:** `finishedAt` when `isFinished`. For a threshold finish, the row's `lastUpdate` **on the run that first saw ≥ 0.97**. That pending `finished_on` is kept until the pending value changes.
   - **Start date:** `startedAt`.
3. **Currently Reading.** When a row first becomes started, set status **2** with an open read (`started_at` = start date), **only if** the user_book is absent, Want to Read (1) or Paused (4). Read, DNF and Ignored are left alone on a start.
4. **A finish always sets Read** (status **3**), whatever the prior status, DNF and Ignored included. Then an **adoptable read** is completed or adopted; otherwise a new read is inserted (start = `started_on`, finish = `finished_on`, edition per §3.3.5 with the item's format). **Rating and review are never touched by ABS.**
5. **Same read vs re-read: ±14 days, for both sources.** A finish within ±14 days of an existing read (or with an open read) is the same read. An audio copy and an ebook copy finishing within the window are one read. A re-read is a finish outside the window.
6. **Un-finishing and abandonment never touch Hardcover.** A reset, a scrub-back, a deleted progress row or a stalled start is recorded in the baseline and logged, and pushes nothing. SyncShelf never moves a book out of Currently Reading and never deletes a read.
7. **No cursor: a per-row baseline.**
   - Every run fetches `/api/me/progress` in full and diffs each row against its ABS baseline: `baseline_fields` for `abs_state` (`none | started | finished`), `started_on` and `finished_on`.
   - Pushes happen only on the transitions **`none → started`** and **`* → finished`**, so an un-finished-then-finished row is a new finish event.
   - This contradicts the research's `finishedAt > X` cursor, because backdated device dates would fall behind a cursor.
   - A status change and its read edits are one change, carried on `abs_state`.
   - Commit timing is the 4a rule: write first, then advance the baseline. A replay adopts.
8. **The first sync is baseline only.** On a profile's first ABS run, every row's current state becomes its baseline and **nothing is pushed**. Only transitions after onboarding reach Hardcover. It **is** the ABS backfill and uses its rules (§3.4a.12): exact ids only, in the library, no fuzzy auto-link; stamped in `backfills`; re-run through `rebaseline("abs")`; and resolving a backfill-queued item pushes only if the book is absent from the library ([#32](https://github.com/ryansull/syncshelf/issues/32), decision 10).
9. **Matching scope.** Matching runs only for rows that are **started or finished**. Rows under 5 % never touch Hardcover or the unmatched queue. A queued row's pending diff reads, for example, "would mark Currently Reading from 2026-10-01" or "would mark Read, finished 2026-10-12".
10. **`change_log`** records `trigger = isFinished | threshold` and `date_source = abs_finished_at | abs_last_update`.

**Cadence:** every **15 min** per profile, 1 `GET`. Backoff on fetch failure is 15 min → 30 min → 1 h (cap). Each ABS transition costs Hardcover a status write, one read fetch for adoption, and at most one read insert or update. That comes on top of the run's lazy library fetch.

#### 3.5.3 Tests

- **Golden `abs_finish.yaml`:**
  - the 5 % / 97 % thresholds;
  - the ±14-day window edges (day 14 vs 15);
  - open-read completion;
  - un-finish → re-finish;
  - audio + ebook of one book;
  - a backdated `finishedAt`;
  - the Currently Reading gate (absent/1/4 vs 3/5/6);
  - a finish over DNF;
  - the threshold finish date kept across runs;
  - the first sync pushing nothing.
- **`FakeABS`:** one progress row per user per item, removed-item rows, and the mutating `GET`s never called.
- **Fixtures** stay `source-only` until the entry gate re-records them.
- **Live:** GET-only with each profile's own key.

**Done when:** finishing an audiobook, and an ebook at ≥ 97 %, in a test profile's ABS marks the book Read in Hardcover with the right date and a format-correct edition. Starting one marks it Currently Reading (only over none/Want/Paused). Un-finishing changes nothing.

---

### 3.6 Phase 6: Chaptarr

**Goal:** Claude can request a book as an audiobook, an ebook or both, see what's already in the library per format, and watch the household's download queue. Ebooks are first-class.

Decisions:
- [Chaptarr API surface and how it models audiobook vs ebook](https://github.com/ryansull/syncshelf/issues/8)
- [Chaptarr supervised write test: add, monitor, search, clean up](https://github.com/ryansull/syncshelf/issues/26) (findings comment: the live write log)
- [The Chaptarr request tool: making ebooks first-class](https://github.com/ryansull/syncshelf/issues/15)
- the observability ticket (`dismiss_attention`)

> **Hard constraint: Chaptarr grabs from a private tracker.** A cancelled or incomplete download damages the owner's tracker standing.
> - **A search is a committed, uncancellable download.**
> - SyncShelf **never** removes a queue item from the download client, cancels a download, cancels a `pendingauthorimport`, deletes an author or row, or retries an add blindly.
> - **No session, test or tool** triggers a search, leaves a row wanted, or touches the queue **without the owner's explicit go-ahead for that action**. A tool's go-ahead is its confirm step.

**Builds:** `clients/chaptarr.py`, `sync/chaptarr_watch.py`, `tools/chaptarr.py`, and `dismiss_attention` in `tools/sync.py`.

**Tables:** `chaptarr_requests` (`0006_chaptarr`). Its `acknowledged_at` replaces the earlier `attention` column; attention is derived, never stored ([Observability](https://github.com/ryansull/syncshelf/issues/30)).

#### 3.6.1 Chaptarr API contract

Checked against Chaptarr **`0.9.965.0`** (`develop`), whose source is at `github.com/Chaptarr/chaptarr` `e67374f`. Chaptarr is pre-1.0 and changes fast: **re-check the version at build time**, and treat a mismatch as a reason to re-record fixtures.

- **API and auth:**
  - **The native `/api/v1`** (Readarr's shape, heavily extended), with the `X-Api-Key` header (`?apikey=` also works, but keeps the key in URLs, so don't use it).
  - **Don't use** the `/readarr/{hc|gr}/{ebook|audiobook}/api/v1` facade, which is per-format and lossy.
  - No OpenAPI is served live. The source repo's `src/Chaptarr.Api.V1/openapi.json` (GPL-3.0, 235 paths) matches live, and is the reference.
- **Ids:**
  - **Provider-prefixed strings:** `hc:<book>`, `gr:<work>`, `az:<asin>`, plus `ol:` and `gb:` in source. Edition ids look like `hc:edition:<id>-audiobook`.
  - **Provider ids are durable; local row ids are not**, and a row's primary id can change.
  - Goodreads ids in Chaptarr are **work** ids.
  - In the live library, `foreignBookId` prefixes split hc 241 / gr 105 / az 39.
- **Format model** (verified live):
  - **One book row per work per format** (`mediaType: "audiobook" | "ebook"`). Sibling rows share provider ids, and each has its own id, edition, files, profile, root folder and monitor flag.
  - One book can be wanted in both formats at once.
  - Author settings are split per format (`audiobookMonitored`, `ebookMonitored`, `<fmt>MonitorNewItems`, `<fmt>QualityProfileId`, `<fmt>RootFolderPath`, …). A format side is created on demand.
- **Effectively wanted** (verified live, all four combinations) = the **author's side for that format and the row flag** are both on. Only these rows show in `wanted/missing?mediaType=` and get grabbed by RSS.
- **Lookup:**
  - `GET /api/v1/book/lookup?term=hc:<book_id>` → results for both formats (verified live). Each carries `localAudiobookBooks: [{id}]` / `localEbookBooks: [{id}]` for rows already in the library.
  - An id **must** be prefixed (`hc:`, `gr:`, `az:`, `isbn:`). A bare id is 400, and `asin:` / `goodreads:` / `hardcover:` silently return `[]`.
  - Free-text lookup works, but remote results are all tagged `audiobook`, so **look up without `mediaType`** and set the format on the add.
  - **Take the author id from this lookup, never from Hardcover.** They differed live.
- **The add** (verified live): `POST /api/v1/book/import`:
  ```json
  {"foreignBookId": "hc:<id>", "foreignAuthorId": "<id from Chaptarr's lookup>", "mediaType": "audiobook|ebook",
   "authorMonitoring": {"monitor": "specificBook", "<fmt>Monitored": true,
                        "<fmt>MonitorNewItems": "none", "searchForMissing": false}}
  ```
  - `rootFolder`, `qualityProfileId` and `metadataProfileId` are **omitted**. Chaptarr fills them from the format's effective defaults.
  - ⚠ **Without `authorMonitoring`, a new author's whole bibliography is imported as monitored.** One test title produced 3,547 rows, 3,379 of them wanted. **Always send it explicitly.**
  - **"Pending" comes back as `400`, not 202:** `[{"propertyName":"Import","errorMessage":"…being prepared by the metadata server. Chaptarr saved your request and will add it automatically…"}]`. It happens for a new author **and** for a new format side on an existing author. Treat it as **accepted, pending**, never as a failure.
  - The call can **block about 5 minutes** before answering. The client timeout is **6 min** (a code constant). **Never retry on a timeout**, since a retry re-queues: inspect `pendingauthorimport` and the rows instead.
  - A pending request sits in `GET /api/v1/pendingauthorimport` (e.g. `ebookStatus: Pending`, `ebookBooksToMonitor: ["hc:<id>"]`, `overallStatus: Retrying`) until it lands. Audiobook rows landed in about 1 minute; an ebook hadn't after 10.
  - **Never use** `POST /api/v1/book`: without `mediaType` it adds both formats.
- **Monitoring** (verified live):
  - **Rows:** `PUT /api/v1/book/monitor {"bookIds":[…],"monitored":bool}` → **202**. It persists, in bulk too (batches of 500).
  - **Author side:** `GET /api/v1/author/{id}` → edit `<fmt>Monitored` / `<fmt>MonitorNewItems` → `PUT /api/v1/author/{id}` → **202**.
- **Search** (verified live): `POST /api/v1/command {"name":"BookSearch","bookIds":[<rowId>]}` → **201** (`status: queued`). Row ids are format-scoped.
  - Poll `GET /api/v1/command/{id}` until `completed` (about 50 s). The `message` says whether anything was grabbed: "Book search completed. 1 reports downloaded."
  - The search walks down ranked releases until the download client accepts one.
  - A grab appears in `queue` as `downloading` and in `history?bookId=` as `grabbed`.
- **Queue:** `GET /api/v1/queue?includeBook=true&mediaType=<fmt>`.
  - Records carry `bookId`, `status`, `trackedDownloadState` (`downloading`, `importPending`, `importBlocked`, `imported`, …), `trackedDownloadStatus`, `sizeleft`, `timeleft`, `estimatedCompletionTime`, `errorMessage`, `statusMessages` and `book.mediaType`. There's **no top-level `mediaType`**.
  - **SyncShelf drops** `indexer`, `downloadClient`, `downloadId`, `outputPath` and `title`.
- **Library state:**
  - **have:** `hasFiles` / `statistics.bookFileCount > 0`;
  - **files:** `GET /bookfile?bookId=<rowId>`. **`bookfile` with no filter is 500.**
- **Quirks:**
  - The ebook row's `title` can be a translated edition's title, so display Hardcover's title instead.
  - The metadata server returns transient **202/503** ("temporarily unavailable"). The tool **reports** these rather than looping.
  - ⚠ **The ebook row shape is source only.** The ebook import reached `pendingauthorimport` correctly and was cancelled before it landed.

#### 3.6.2 Per-format state

[Assembly choice: derivation and precedence. The state names are decided.]

For one book and one format, the state is the **first** of these that holds:

| State | Holds when |
|---|---|
| `have` | a row exists for the format with files |
| `import_blocked` | a queue record for that row has `trackedDownloadState = importBlocked` |
| `downloading` | any other queue record exists for that row |
| `pending` | a `chaptarr_requests` row is `pending` for (book, format), or `pendingauthorimport` lists the book for that format |
| `wanted` | the row is effectively wanted (row flag + author side), with no files |
| `dormant` | the row exists but isn't effectively wanted |
| `none` | no row for that format |

#### 3.6.3 Tools

All three need the **`chaptarr` permission** (hidden from `tools/list` and refused server-side without it). They're keyed by Hardcover `book_id`; Claude resolves books with `search_books` / `check_books` first. **No tool cancels, removes or deletes anything in Chaptarr, ever.**

| Tool | Contract |
|---|---|
| `chaptarr_request(book_id, format: "audio" \| "ebook" \| "both", want: bool = False, confirm_token: str \| None = None)` | Without a token it returns a **preview** and writes nothing. With a token it **acts**. `destructiveHint: true`. |
| `library_status(book_ids: list[int])` | A batch of up to 20. The per-format state (§3.6.2) from Chaptarr only, plus `requested_by` / `requested_at` for SyncShelf-made requests. Read-only. It doesn't query ABS (fed from Chaptarr's folders, lagging by minutes), and doesn't overlap `check_books`, which reports availability *on Hardcover*. |
| `chaptarr_queue()` | Read-only, **the whole household**. Every queue record in both formats (`trackedDownloadState`/`Status`, `sizeleft`, `timeleft`, `book.mediaType`, `importBlocked` flagged, and Hardcover's title), plus SyncShelf's pending and recent requests with `requested_by` and state. The dropped fields above are never returned. |
| `dismiss_attention(id)` | Scoped to the caller's own **event** items (`chaptarr_contained`, `chaptarr_expired`). Sets `chaptarr_requests.acknowledged_at`; the request keeps its state. Trying it on a state-derived item returns an error naming the tool that fixes the state. No permission needed. |

**The preview** gives, per format:
- the resolved Chaptarr ids;
- title and author **from Hardcover**;
- the current state and the action that would be taken;
- whether the author or the author's side is new, which means a **pending add with unknown timing**;
- how many dormant rows would be unmonitored;
- for `want=true`, the Hardcover change;
- the line ***"This starts a download from a private tracker that can't be cancelled."***

**The confirm token:**
- lives **10 minutes**, in memory (one worker);
- is bound to (profile, `book_id`, format, `want`, the resolved ids, a snapshot of the state);
- **on confirm, the server re-checks state.** If anything changed, it returns a fresh preview and does nothing.
- `both` is two independent per-format plans under one token, with an outcome reported per format.

#### 3.6.4 The request flow (confirm call)

`resolve` → re-check state against the token → *(want=true)* Hardcover first → per format, act on the existing state:

| State | Action |
|---|---|
| `have`, `downloading`, `pending`, `wanted` | **No-op** that reports the state. No re-search: a wanted row is left to Chaptarr's RSS. |
| `import_blocked` | No-op, plus "needs attention in Chaptarr". SyncShelf never resolves it. |
| `dormant` | The **monitor path:** dormant-row unmonitor if needed → `book/monitor` on → side on (`<fmt>MonitorNewItems: none`) → **check** → `BookSearch`. |
| `none` | The **add path:** dormant-row unmonitor if needed → `book/import` (search off). If it comes back **pending**, record a `pending` request and stop here: the watcher finishes it. Otherwise → **check** → `BookSearch`. |

**Rules:**
1. **Resolving the book in Chaptarr.**
   - First try `book/lookup?term=hc:<book_id>` → `localAudiobookBooks` / `localEbookBooks`.
   - If that misses, try a free-text title/author lookup. Accept it only for a **single hit that passes the matcher's exact-hit floor** (title ≥ 60); any prefix (`gr:`, `az:`) is fine.
   - Ambiguous or missing → `not_found`, with no guess.
   - Nothing is stored except the request record.
2. **Dormant-row unmonitor.** If the author's side for the format is off and the author has **other monitored rows without files** in that format, SyncShelf bulk-unmonitors those first (`book/monitor`), then turns the side on with `<fmt>MonitorNewItems: none`. Nothing that was effectively wanted changes, apart from the requested book. The preview and the response report the count.
3. **The check.** After the add or monitor, count the author's effectively wanted rows in that format. **Exactly the requested row must be new.** Only then send `BookSearch` for that row id, poll `command/{id}`, and report the completion message. The request becomes `searched`.
4. **Containment.** If more than the requested row became wanted:
   - turn the author's side for that format **off**, so wanted drops to 0 and RSS can't grab;
   - leave the rows alone, **fire no search**;
   - mark the request `contained`, which raises the `chaptarr_contained` attention item, and say so in the response.

   **It never deletes the author**, who may own files.
5. **`want=true`.** At confirm, Hardcover is written **before** Chaptarr: status 1 (Want to Read), **only if the book isn't in the user's library**, through §3.3.6 (an insert with `hardcover_privacy`). An existing status is never changed. Each side reports its own outcome. A Chaptarr failure leaves the want in place, with **no rollback and no automatic retry**.
6. **A no-op request** is recorded as `noop`, for `requested_by` history [assembly choice].
7. **Configuration:** `chaptarr_url` and `chaptarr_api_key` only. Root folders and quality/metadata profiles come from Chaptarr's per-format defaults.

#### 3.6.5 The pending watcher

- **When it runs:** every **2 min**, only while any `chaptarr_requests` row is `pending`. It polls `pendingauthorimport` and the author's rows.
- **When the row lands:** run the **check** → `BookSearch` once → `searched` (or `contained`). Set `landed_at`.
- **Watch window:** the watcher watches a pending request for **7 days**.
  - After **24 h**, `chaptarr_pending_long` is raised.
  - At 7 days the request becomes **`expired`**, **no search fires**, and `chaptarr_expired` is raised.
- **SyncShelf never cancels a `pendingauthorimport`.** That's the owner's call.
- **On restart:** pending requests resume being watched. The watcher has no `schedules` row; it runs while pending rows exist.
- **One pending request per (book, format)** is enforced by the partial unique index.

#### 3.6.6 Tests

- **Golden `chaptarr_request.yaml`:**
  - every row of the §3.6.4 table;
  - a token re-check on a state change;
  - `both` with mixed states;
  - the dormant-row unmonitor;
  - a failed check → containment (side off, no search);
  - pending → land → search;
  - the 7-day expiry;
  - `want=true` with the book absent vs present, and a Chaptarr failure after a Hardcover success.
- **`FakeChaptarr` traps:**
  - without `authorMonitoring`, the whole bibliography becomes wanted;
  - the 400 "saved your request" pending and `pendingauthorimport`;
  - wanted needs both the author side and the row flag;
  - the blocking add and the timeout path.
- **Pending watcher:** runs only while requests are pending, resumes after a restart, and attention fires at 24 h and 7 days.
- **Live: GET-only.** The harness client raises before any POST, PUT or DELETE. Search, queue and the pending flow use **only** the fixtures recorded in the supervised write test. **Any further live Chaptarr write is a supervised manual session with per-action approval, never a test.**

**Done when:** against the fakes, every path in the golden file passes, and no code path can issue a forbidden call (a test greps the client for `DELETE` and `removeFromClient` and fails if they appear). GET-only live checks pass. The first real request is the owner's own, made through Claude with the confirm step.

---

### 3.7 Phase 7: Deployment runbook and live acceptance

**Goal:** a person who has never seen the code can deploy SyncShelf, onboard both household members, operate it, and pass the live acceptance with real Claude clients. The runbook is `docs/runbook.md` [assembly choice for the path], written with the placeholders from §2.6.

Decisions:
- [Deployment topology: Traefik, Cloudflare Tunnel, reaching Chaptarr](https://github.com/ryansull/syncshelf/issues/20)
- [Public-repo hygiene](https://github.com/ryansull/syncshelf/issues/5)
- [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19) (container user, snapshots)
- [Observability](https://github.com/ryansull/syncshelf/issues/30) (health, logs)
- [OAuth policy leftovers](https://github.com/ryansull/syncshelf/issues/23) (the revocation fallback)
- [Testing strategy](https://github.com/ryansull/syncshelf/issues/29) (level 4)

**Runbook contents**

1. **Topology.** Cloudflare edge → the **existing `cloudflared`** → **Traefik** → SyncShelf, over plain HTTP inside the host, with TLS terminating at Cloudflare. `Cf-Access-Jwt-Assertion` passes through Traefik untouched. uvicorn runs with `--proxy-headers` and `--forwarded-allow-ips` limited to the Traefik network.
2. **Compose (generic, committed as `deploy/compose.example.yaml`)** [assembly choice for the path]:
   - **The `syncshelf` service:**
     - the `ghcr.io/<owner>/syncshelf:<version>` image;
     - `/data` as a **local-disk bind mount**, and `profiles.yaml` mounted **read-only**;
     - secrets as env vars or files;
     - networks: the default bridge plus the **external Traefik network**, no host networking;
     - the `json-file` log driver with `max-size` / `max-file`;
     - the image's `HEALTHCHECK`.
   - **The optional `browser` service** (4b): `mcr.microsoft.com/playwright:<pinned>` running `playwright run-server`, on an **internal network only**, with no Traefik labels, no published ports and no volume.
   - **Where real values live:** they go in the **Portainer stack** (domain, Traefik labels, LAN URLs, secrets as stack env), never in git.
3. **Host prep:**
   - `chown -R 10001:10001 /data`;
   - make `profiles.yaml` readable by 10001;
   - local disk only (never NFS or SMB).
4. **Traefik routers:**
   - **public** (Tunnel hostname `shelf.example.com`): only `/mcp`, `/.well-known/*`, `/authorize` and `/token`;
   - **LAN-only** (e.g. `shelf.home.arpa`): `/ingest/goodreads-csv` and `/healthz`. The Tunnel hostname never routes them.
5. **Cloudflare:**
   - **Access apps:**
     - a hostname-wide **Allow** app for the household emails;
     - a **Bypass** app for `/mcp`, `/.well-known/*` and `/token`. Verify that `/mcp` itself matches, since a trailing wildcard doesn't match its parent path.
     - Copy the `/authorize` app's **AUD tag** to `ACCESS_AUD`, and the team domain to `server.access.team_domain`.
   - **Bots and WAF:**
     - skip WAF and bot rules on the bypassed paths;
     - turn off Block AI Bots / Bot Fight Mode for the hostname. "Block AI bots" was marked as being deprecated on 2026-09-15, so check what replaces it.
   - **Rate limiting:** a rate-limit rule on `/token`.
   - **No IP restriction** to Anthropic's range: tokens are the gate, and Claude Code connects from the user's machine.
6. **Outbound:** Chaptarr and ABS at their LAN addresses (they stay unexposed), and Hardcover and Goodreads over the internet. All base URLs come from config.
7. **Backups:** host tooling only. Use `sqlite3 /data/syncshelf.db ".backup …"`, or stop the container first. **Copying a live WAL database is unsafe.** Prune old `/data/pre-migration-NNNN.db` snapshots.
8. **Health and alerting:**
   - Docker `HEALTHCHECK` on `/healthz` (liveness only; 503 only when the DB is down);
   - example **Uptime Kuma** keyword monitor (`"status":"ok"`) and **Home Assistant REST sensor** configs pointing at the LAN `/healthz`.

   SyncShelf sends no notifications.
9. **Operations SQL**, run inside the container with `sqlite3`:
   - revoke a grant, or all of a profile's grants, when Claude itself is unavailable (`UPDATE oauth_grant SET revoked_at=…, revoked_reason='admin' WHERE …`);
   - rename a profile (the documented manual steps across every `profile` column).
10. **Onboarding a person.** This graduates the map's "Second-user onboarding runbook" fog.

    **The operator:**
    1. Adds the person's email to the Access **Allow** policy.
    2. Adds a `profiles.yaml` entry: name, email, timezone, permissions, `household_visible`, `hardcover_privacy`, `goodreads.user_id` and `shelf_map`.
    3. Puts the person's secrets in the stack env:
       - a **Hardcover PAT** with scopes `read:me`, `read:library`, `read:catalog`, `write:library` (docs only);
       - an **ABS API key on their own non-root ABS user**;
       - their **Goodreads email and password** (4b);
       - a generated **ingest secret**.
    4. Restarts the stack.

    **The person:**
    1. In claude.ai: Settings → Connectors → **Add custom connector**. Set the URL to `https://shelf.example.com/mcp`, the OAuth client to **"Use Claude's published identity"** (CIMD), and authentication to sign in now.
    2. Completes the Access login, then **Allow** on the consent page.
    3. Desktop and mobile then use the same connector. Authentication settings can't be edited later; remove and re-add the connector to change them.
    4. Optionally, Claude Code: `claude mcp add --transport http syncshelf https://shelf.example.com/mcp`, then `/mcp` to authenticate.
    5. Through Claude, runs **`goodreads_sign_in()`** and answers any approval or code prompt (4b).

    **Then:**
    - **After** the hand import into Hardcover has finished, enable the profile's Goodreads source. Its first complete run is the backfill (§3.4a.12). If it ran too early, use `rebaseline("goodreads")`. Then work the queue with `unmatched_books`.
    - The ABS first sync runs by itself and is baseline only.
    - Confirm with `sync_status()`.
11. **Live acceptance (level 4).** Log each `/authorize` and `/token` request's parameters, minus secrets, while testing.
    - **Clients:** Claude Code, claude.ai web, Desktop, and use from mobile. Check `tools/list` per profile (Chaptarr tools present only with the permission).
    - **Refresh:** set the access TTL to 5 minutes in a staging config and confirm silent refresh.
    - **Revocation:** `revoke_connection("all")`, and confirm Claude asks to reconnect rather than erroring.
    - **The phase 2 live checks** (§3.2.8): `iss` acceptance, Access on POST `/authorize`, whether the CIMD fetch is ever blocked, the loopback host, `resource` on refresh, and that a real browser's Allow click passes the `Origin` check.

**Tests:** the runbook steps are executed once end to end on the real deployment, and level 4 passes. The findings are recorded on the map.

**Done when:** both household members are connected from every Claude surface they use, `/healthz` is monitored, and the runbook was followed as written, with no undocumented step.

---

## Appendix A: Full DDL

This is the DDL from [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19), with one amendment applied: in `0006_chaptarr`, `acknowledged_at` replaces `attention`, per [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30). Each `-- NNNN_name` block is one migration file. No migration references a table from a later phase.

```sql
-- 0001_meta (phase 1) -----------------------------------------------------
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,          -- 'last_started_version'
  value TEXT NOT NULL
);

-- 0002_auth (phase 2) -----------------------------------------------------
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

-- 0003_mirror (phase 3) ---------------------------------------------------
CREATE TABLE mirror_state (
  profile         TEXT PRIMARY KEY REFERENCES profiles(name),
  as_of           INTEGER,                          -- last successful full fetch
  last_attempt_at INTEGER,
  last_error      TEXT                              -- NULL after a success
);

CREATE TABLE mirror_books (
  profile           TEXT NOT NULL REFERENCES profiles(name),
  hc_book_id        INTEGER NOT NULL,
  title             TEXT NOT NULL,
  contributors_json TEXT NOT NULL,                  -- [{name, role}]
  series_id         INTEGER,
  series_name       TEXT,
  series_position   REAL,
  pages             INTEGER,
  audio_seconds     INTEGER,
  tags_json         TEXT NOT NULL,                  -- top tags by category
  PRIMARY KEY (profile, hc_book_id)
);
CREATE INDEX mirror_books_series ON mirror_books(profile, series_id);

CREATE TABLE mirror_user_books (
  profile         TEXT NOT NULL REFERENCES profiles(name),
  hc_user_book_id INTEGER NOT NULL,
  hc_book_id      INTEGER NOT NULL,
  status_id       INTEGER NOT NULL CHECK (status_id BETWEEN 1 AND 6),
  rating          REAL CHECK (rating IS NULL OR (rating BETWEEN 0.5 AND 5 AND rating * 2 = CAST(rating * 2 AS INTEGER))),
  hc_edition_id   INTEGER,
  date_added      TEXT,
  PRIMARY KEY (profile, hc_user_book_id),
  UNIQUE (profile, hc_book_id)
);
CREATE INDEX mirror_user_books_status ON mirror_user_books(profile, status_id);

CREATE TABLE mirror_reads (
  profile           TEXT NOT NULL REFERENCES profiles(name),
  hc_read_id        INTEGER NOT NULL,
  hc_user_book_id   INTEGER NOT NULL,
  started_on        TEXT,
  finished_on       TEXT,
  hc_edition_id     INTEGER,
  reading_format_id INTEGER,                        -- NULL shown as the 'unknown' bucket
  PRIMARY KEY (profile, hc_read_id)
);
CREATE INDEX mirror_reads_finished ON mirror_reads(profile, finished_on);

CREATE TABLE mirror_list_items (
  profile    TEXT NOT NULL REFERENCES profiles(name),
  hc_list_id INTEGER NOT NULL,
  list_name  TEXT NOT NULL,
  hc_book_id INTEGER NOT NULL,
  PRIMARY KEY (profile, hc_list_id, hc_book_id)
);

-- 0004_sync (phase 4a; ABS reuses it in phase 5) -----------------------------
CREATE TABLE links (                                -- shared across profiles
  source        TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  source_id     TEXT NOT NULL,                      -- GR edition book_id | ABS libraryItemId
  hc_book_id    INTEGER NOT NULL,
  hc_edition_id INTEGER,                            -- informational, never a key
  format        TEXT CHECK (format IN ('audio','ebook','print')),
  method        TEXT NOT NULL CHECK (method IN
                  ('human','gr_mapping','isbn13','isbn10','asin','fuzzy')),
  state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','stale')),
  linked_at     INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  PRIMARY KEY (source, source_id)
);
CREATE INDEX links_book ON links(hc_book_id);       -- merge rewrites
CREATE TRIGGER links_human_sticky
  BEFORE UPDATE OF method ON links
  WHEN OLD.method = 'human' AND NEW.method <> 'human'
  BEGIN SELECT RAISE(ABORT, 'human link is sticky'); END;

CREATE TABLE source_rows (                          -- per profile; the unmatched queue
  profile         TEXT NOT NULL REFERENCES profiles(name),
  source          TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  source_id       TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('linked','queued','ignored')),
  hc_book_id      INTEGER,                          -- target of this profile's writes
  hc_user_book_id INTEGER,
  queue_reason    TEXT CHECK (queue_reason IN
                    ('no_hit','id_title_mismatch','fuzzy_low','fuzzy_ambiguous',
                     'not_in_library','profile_conflict','stale','ambiguous_mapping')),
  queued_at       INTEGER,
  match_input     TEXT,                             -- JSON: title, authors, isbns, asin, gr_book_id, format, year
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,                 -- freezes when gone or id swapped
  gone_at         INTEGER,                          -- set only by a complete enumeration
  backfill_queued INTEGER NOT NULL DEFAULT 0 CHECK (backfill_queued IN (0,1)),  -- queued by the backfill; cleared on resolve
  PRIMARY KEY (profile, source, source_id),
  CHECK ((state = 'linked') = (hc_book_id IS NOT NULL)),
  CHECK ((state = 'queued') = (queue_reason IS NOT NULL AND queued_at IS NOT NULL))
);
CREATE INDEX source_rows_queue ON source_rows(profile, state, queued_at);
CREATE INDEX source_rows_book  ON source_rows(hc_book_id);

CREATE TABLE match_candidates (
  profile       TEXT NOT NULL,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  rank          INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 5),
  hc_book_id    INTEGER NOT NULL,
  hc_edition_id INTEGER,
  method        TEXT NOT NULL,
  title_score   REAL,
  author_score  REAL,
  fetched_at    INTEGER NOT NULL,
  PRIMARY KEY (profile, source, source_id, rank),
  FOREIGN KEY (profile, source, source_id)
    REFERENCES source_rows(profile, source, source_id) ON DELETE CASCADE
);

CREATE TABLE baseline_fields (
  id               INTEGER PRIMARY KEY,             -- the change_id for retry_failed
  profile          TEXT NOT NULL,
  source           TEXT NOT NULL,
  source_id        TEXT NOT NULL,
  field            TEXT NOT NULL CHECK (field IN
                     ('status','rating','date_read','review','spoiler','read_count',
                      'abs_state','started_on','finished_on')),
  value            TEXT,                            -- normalised; NULL = observed empty / no baseline
  observed_at      INTEGER,                         -- NULL = empty baseline (first seen after backfill)
  prev_value       TEXT,                            -- value before the last advance (re-read cycle)
  -- pending change: set when a source value differs from value, cleared on commit or supersede
  pending_value    TEXT,
  pending_since    INTEGER,                         -- first-seen date for undated finishes
  attempts         INTEGER NOT NULL DEFAULT 0,
  first_failed_at  INTEGER,
  last_error       TEXT,
  last_error_class TEXT CHECK (last_error_class IN ('transient','permanent','bug')),
  quarantined_at   INTEGER,
  UNIQUE (profile, source, source_id, field),
  FOREIGN KEY (profile, source, source_id)
    REFERENCES source_rows(profile, source, source_id) ON DELETE CASCADE
);
CREATE INDEX baseline_fields_quarantined ON baseline_fields(profile, quarantined_at)
  WHERE quarantined_at IS NOT NULL;
CREATE INDEX baseline_fields_pending ON baseline_fields(profile, pending_since)
  WHERE pending_since IS NOT NULL;

CREATE TABLE runs (
  id          INTEGER PRIMARY KEY,
  profile     TEXT NOT NULL REFERENCES profiles(name),
  source      TEXT NOT NULL CHECK (source IN ('gr_rss','gr_csv','abs','mirror','chaptarr_watch')),
  trigger     TEXT NOT NULL CHECK (trigger IN ('schedule','sync_now','ingest','startup')),
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER,
  state       TEXT NOT NULL CHECK (state IN
                ('running','ok','partial','fetch_failed','deferred','aborted')),
  applied     INTEGER NOT NULL DEFAULT 0,
  failed      INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0,
  first_error TEXT
);
CREATE INDEX runs_recent ON runs(profile, source, started_at);
-- startup: UPDATE runs SET state='aborted', ended_at=now WHERE state='running'
-- purge: runs older than 90 days

CREATE TABLE change_log (
  id              INTEGER PRIMARY KEY,
  run_id          INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  profile         TEXT NOT NULL REFERENCES profiles(name),
  source          TEXT,                             -- NULL for Claude-initiated identity edits
  source_id       TEXT,
  hc_book_id      INTEGER,
  hc_user_book_id INTEGER,
  kind            TEXT NOT NULL CHECK (kind IN
                    ('push','read_insert','read_adopt','link','resolve_match','ignore',
                     'unignore','merge','gone','stale')),
  field           TEXT,
  old_value       TEXT,
  new_value       TEXT,
  trigger         TEXT CHECK (trigger IN ('read_count','reread_cycle','status','isFinished','threshold')),
  date_source     TEXT CHECK (date_source IN
                    ('goodreads_date_read','user_date_added','first_seen','abs_finished_at','abs_last_update')),
  at              INTEGER NOT NULL
);
CREATE INDEX change_log_row  ON change_log(profile, source, source_id);
CREATE INDEX change_log_book ON change_log(hc_book_id);

CREATE TABLE schedules (
  profile              TEXT NOT NULL REFERENCES profiles(name),
  source               TEXT NOT NULL CHECK (source IN ('gr_rss','gr_csv','abs')),
  last_run_at          INTEGER,
  last_success_at      INTEGER,
  last_observed_at     INTEGER,                     -- shown in sync_status
  next_due_at          INTEGER NOT NULL,            -- CSV due date lives here
  last_result          TEXT,                        -- the last run's state
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  backoff_until        INTEGER,
  PRIMARY KEY (profile, source)
);

CREATE TABLE ingest_batches (
  id           INTEGER PRIMARY KEY,
  profile      TEXT NOT NULL REFERENCES profiles(name),
  source       TEXT NOT NULL CHECK (source IN ('gr_csv_auto','gr_csv_ingest')),
  received_at  INTEGER NOT NULL,
  observed_at  INTEGER,                             -- NULL when /ingest sent no generated_at
  stale        INTEGER NOT NULL CHECK (stale IN (0,1)),
  complete     INTEGER NOT NULL CHECK (complete IN (0,1)),
  payload      BLOB NOT NULL,                       -- the CSV as received
  row_count    INTEGER,
  processed_at INTEGER,
  run_id       INTEGER REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX ingest_batches_profile ON ingest_batches(profile, received_at);

CREATE TABLE backfills (                            -- one row per completed backfill (#32)
  profile     TEXT NOT NULL REFERENCES profiles(name),
  source      TEXT NOT NULL CHECK (source IN ('goodreads','abs')),
  done_at     INTEGER NOT NULL,                     -- rewritten by rebaseline()
  run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  rows_linked INTEGER NOT NULL,
  rows_queued INTEGER NOT NULL,
  PRIMARY KEY (profile, source)
);
-- keep the last 3 per profile

-- 0005_goodreads_session (phase 4b) -------------------------------------------
CREATE TABLE goodreads_sessions (
  profile              TEXT PRIMARY KEY REFERENCES profiles(name),
  state                TEXT NOT NULL CHECK (state IN
                         ('ok','needs_sign_in','awaiting_approval','awaiting_code',
                          'challenged','bad_credentials','disabled')),
  state_since          INTEGER NOT NULL,
  detail               TEXT,                        -- human-readable reason, no secrets
  last_auto_sign_in_at INTEGER                      -- unattended re-sign-in at most once per 24 h
);
-- the cookie jar is /data/goodreads/<profile>.cookies.json, never SQLite

-- 0006_chaptarr (phase 6) ---------------------------------------------------
CREATE TABLE chaptarr_requests (
  id                INTEGER PRIMARY KEY,
  profile           TEXT NOT NULL REFERENCES profiles(name),
  hc_book_id        INTEGER NOT NULL,
  format            TEXT NOT NULL CHECK (format IN ('audio','ebook')),  -- 'both' = two rows
  chaptarr_author_id INTEGER,
  chaptarr_book_id  INTEGER,
  foreign_book_id   TEXT,                           -- e.g. 'hc:<id>' as sent to book/import
  state             TEXT NOT NULL CHECK (state IN ('pending','searched','contained','expired','noop')),
  requested_at      INTEGER NOT NULL,
  landed_at         INTEGER,
  search_command_id INTEGER,
  result_message    TEXT,
  acknowledged_at   INTEGER                         -- dismiss_attention; attention itself is derived, never stored
);
CREATE UNIQUE INDEX chaptarr_requests_one_pending
  ON chaptarr_requests(hc_book_id, format) WHERE state = 'pending';
CREATE INDEX chaptarr_requests_state ON chaptarr_requests(state);
```

**Notes on reading the DDL:**
- `profiles.name` is checked loosely in SQL (`GLOB '[a-z]*'`, length ≤ 32). Config validation enforces the full `^[a-z][a-z0-9_]{0,31}$`.
- In `baseline_fields`:
  - `value` is normalised `TEXT`, typed in code;
  - `observed_at NULL` marks an empty baseline;
  - `prev_value` supports the Read → CR → Read re-read signal;
  - `pending_since` is the first-seen date for undated finishes and survives failed or deferred runs.
- `chaptarr_requests` attention is **derived**: `state IN ('contained','expired') AND acknowledged_at IS NULL`, or `state = 'pending'` for > 24 h.
- `runs.source` reserves `mirror` and `chaptarr_watch` (§3.4a.7).

---

## Appendix B: Tool index

| Tool | Phase | Permission | Kind | Scope |
|---|---|---|---|---|
| `my_connections()` | 2 | — | read | own grants |
| `revoke_connection(target)` | 2 | — | write (SyncShelf) | own grants |
| `get_my_library(...)` | 3 | — | read (mirror) | `profile` param |
| `get_book(book)` | 3 | — | read (live) | `profile` param |
| `series_progress(series?)` | 3 | — | read (mirror + live) | `profile` param |
| `reading_stats(period, group_by?)` | 3 | — | read (mirror) | `profile` param |
| `search_books(...)` | 3 | — | read (live) | `profile` param |
| `check_books(candidates)` | 3 | — | read (live + mirror) | `profile` param |
| `update_book(book_id, ...)` | 3 | — | write (Hardcover) | own |
| `correct_read(book_id, read_id, ..., delete, confirm_token?)` | 3 | — | write (Hardcover) | own; `destructiveHint` |
| `remove_book(book_id, confirm_token?)` | 3 | — | write (Hardcover) | own; `destructiveHint` |
| `sync_now(source, full)` | 4a | — | action | own |
| `sync_status(profile)` | 4a | — | read | own (full) or household (health view) |
| `retry_failed(change_id \| "all")` | 4a | — | action | own |
| `unmatched_books(...)` | 4a | — | read | own |
| `resolve_match(entry_id, hardcover_book_id \| None)` | 4a | — | write (Hardcover) | own |
| `goodreads_sign_in()` | 4b | — | action | own |
| `goodreads_verify(code)` | 4b | — | action | own |
| `chaptarr_request(book_id, format, want, confirm_token?)` | 6 | `chaptarr` | write (Chaptarr; Hardcover if `want`) | own; `destructiveHint` |
| `library_status(book_ids)` | 6 | `chaptarr` | read | — |
| `chaptarr_queue()` | 6 | `chaptarr` | read | household |
| `dismiss_attention(id)` | 6 | — | write (SyncShelf) | own event items |

**Tools from the originating prompt that changed:**
- `chaptarr_search` is **dropped**, because the read tools resolve books.
- `library_has(title, format?)` became `library_status(book_ids[])`.
- `want_and_grab` became `chaptarr_request(…, want=true)`.
- There is no `recommend()`.
- `abs_library_search` is **dropped**, because `library_status` reports `have` from Chaptarr.
- `rate_book` became `update_book(…, rating)`, which carries every shelf edit.

---

## Appendix C: Decision index

Every decision this spec states lives in one of these tickets. They're grouped by the phase that mainly consumes them. The map, [Map: SyncShelf — MCP server + sync service](https://github.com/ryansull/syncshelf/issues/1), carries a one-line gist of each.

| Phase | Tickets |
|---|---|
| Shared | [Hosting the MCP server, ingest endpoint and scheduler in one process](https://github.com/ryansull/syncshelf/issues/4) · [Public-repo hygiene: licence, secret scanning, config conventions](https://github.com/ryansull/syncshelf/issues/5) · [SQLite schema and repo structure](https://github.com/ryansull/syncshelf/issues/19) · [Testing strategy: fixtures, golden sets and live tests](https://github.com/ryansull/syncshelf/issues/29) · [Observability: sync_status payload, attention, and structured logs](https://github.com/ryansull/syncshelf/issues/30) · [Credentials and sample inputs in place](https://github.com/ryansull/syncshelf/issues/2) |
| 2 | [Does a Claude custom connector work behind Cloudflare Access?](https://github.com/ryansull/syncshelf/issues/3) · [The front door: auth architecture](https://github.com/ryansull/syncshelf/issues/11) · [Building SyncShelf's OAuth server: SDK provider, Authlib, or hand-rolled?](https://github.com/ryansull/syncshelf/issues/22) · [OAuth policy leftovers: revocation, refresh reuse, absolute cap, scopes](https://github.com/ryansull/syncshelf/issues/23) |
| 3 | [Hardcover write API: user_book mutations, statuses, rate limit](https://github.com/ryansull/syncshelf/issues/6) · [Hardcover read API: how rich can the reading history get?](https://github.com/ryansull/syncshelf/issues/7) · [Reading history questions: what should Claude be able to answer?](https://github.com/ryansull/syncshelf/issues/17) · [Read-side strategy: live Hardcover queries or a local mirror?](https://github.com/ryansull/syncshelf/issues/18) · [Sync cadence and Hardcover request budget](https://github.com/ryansull/syncshelf/issues/27) · [Direct Hardcover edits from Claude: which write tools, and abs_library_search](https://github.com/ryansull/syncshelf/issues/31) |
| 4a | [Goodreads CSV and shelf RSS: what fields actually arrive](https://github.com/ryansull/syncshelf/issues/10) · [Canonical book identity across Goodreads, Hardcover, ABS and Chaptarr](https://github.com/ryansull/syncshelf/issues/12) · [Goodreads to Hardcover diff: what 'Goodreads changed it' means](https://github.com/ryansull/syncshelf/issues/13) · [Matching and the unmatched queue: thresholds and resolve_match](https://github.com/ryansull/syncshelf/issues/14) · [Failure policy: retries, quarantine and partial runs](https://github.com/ryansull/syncshelf/issues/28) · [Goodreads backfill: when it runs, from which feed, and what a field's first observation means](https://github.com/ryansull/syncshelf/issues/32) |
| 4b | [Goodreads session and library export: what can SyncShelf automate?](https://github.com/ryansull/syncshelf/issues/24) · [Goodreads acquisition design: starting, storing and reusing a session](https://github.com/ryansull/syncshelf/issues/25) |
| 5 | [Audiobookshelf API: per-user progress and finished status](https://github.com/ryansull/syncshelf/issues/9) · [Audiobookshelf to Hardcover: what counts as finished](https://github.com/ryansull/syncshelf/issues/16) |
| 6 | [Chaptarr API surface and how it models audiobook vs ebook](https://github.com/ryansull/syncshelf/issues/8) · [Chaptarr supervised write test: add, monitor, search, clean up](https://github.com/ryansull/syncshelf/issues/26) · [The Chaptarr request tool: making ebooks first-class](https://github.com/ryansull/syncshelf/issues/15) |
| 7 | [Deployment topology: Traefik, Cloudflare Tunnel, reaching Chaptarr](https://github.com/ryansull/syncshelf/issues/20) |

**Amendments applied in this spec** (where a later ticket changed an earlier one):

| Later ticket | Changed | What |
|---|---|---|
| [ABS: what counts as finished](https://github.com/ryansull/syncshelf/issues/16) | [diff](https://github.com/ryansull/syncshelf/issues/13) decision 9; [matching](https://github.com/ryansull/syncshelf/issues/14) decision 6; [ABS API](https://github.com/ryansull/syncshelf/issues/9) | ±14-day / open-read adoption replaces exact-date adoption for both sources; matching widened to started-or-finished; no `finishedAt` cursor |
| [Reading history questions](https://github.com/ryansull/syncshelf/issues/17) | [ABS: what counts as finished](https://github.com/ryansull/syncshelf/issues/16) decision 4 | every new read gets an edition, falling back to the format default |
| [Sync cadence](https://github.com/ryansull/syncshelf/issues/27) | [Read-side strategy](https://github.com/ryansull/syncshelf/issues/18) decision 4 | only runs that act fetch Hardcover; idle runs cost 0 |
| [SQLite schema](https://github.com/ryansull/syncshelf/issues/19) | [OAuth server build](https://github.com/ryansull/syncshelf/issues/22); [OAuth leftovers](https://github.com/ryansull/syncshelf/issues/23) | column renames (`profile`, `client_label`, `rotated_at`/`successor_hash`); the refresh grace pair held in memory |
| [OAuth leftovers](https://github.com/ryansull/syncshelf/issues/23) | [OAuth server build](https://github.com/ryansull/syncshelf/issues/22) §3.3, §3.6 | within the grace window the **same** pair is returned (not a fresh rotation); `scopes_supported: ["syncshelf","offline_access"]` |
| [Testing strategy](https://github.com/ryansull/syncshelf/issues/29) | [OAuth server build](https://github.com/ryansull/syncshelf/issues/22) "phase 2 acceptance" | live Claude-client checks are level 4, run at phase 7 or the first deployment |
| [Observability](https://github.com/ryansull/syncshelf/issues/30) | [Chaptarr request tool](https://github.com/ryansull/syncshelf/issues/15); [SQLite schema](https://github.com/ryansull/syncshelf/issues/19) | `import_blocked` isn't attention; `acknowledged_at` replaces `attention`; the household health view ignores `household_visible` |
| [Goodreads acquisition design](https://github.com/ryansull/syncshelf/issues/25) | [Deployment topology](https://github.com/ryansull/syncshelf/issues/20) | email/password sign-in is automated; cookie import and noVNC are dropped; phase 4 splits into 4a and 4b |
| [Failure policy](https://github.com/ryansull/syncshelf/issues/28) | [Matching](https://github.com/ryansull/syncshelf/issues/14) decision 12 | an `attention` field exists on read tools; the unmatched queue stays out of it |

---

## Appendix D: Assembly choices

These are the small calls this spec made where no ticket spoke. Each is the boring option. **All were accepted by the owner as written on 2026-10-17** ([Assemble the SyncShelf build spec](https://github.com/ryansull/syncshelf/issues/21)), and they bind like any ticket decision.

| # | Where | Choice | Why |
|---|---|---|---|
| D1 | §2.2 | A **per-profile Hardcover apply lock** around each run's fetch-and-apply stages, `resolve_match` and Claude's edits | Write decisions must use a same-run fetch. Without the lock, an RSS run and an ABS run of one profile could each adopt reads against the other's stale view and duplicate a read. |
| D2 | §2.3 | One `profiles.yaml` holds `server`, `services`, `cadence` and `profiles`. Cadence keys are global. Each profile has one `email`. The path comes from `SYNCSHELF_CONFIG`. | The tickets name the keys but not the file shape. |
| D3 | §2.5, §3.3 | `sync/apply.py` (the write procedure) lands in phase 3. The matcher's normalisation and scoring land in phase 3 (bringing `rapidfuzz` there) for `check_books`. | Phase 3 tools need them, and each phase must ship alone. |
| D4 | §3.1 | Phase 1 mounts the MCP sub-app with no tools, and refuses a non-localhost `public_url` | Proves the lifespan, route order and 421 allowlist before auth arrives, without ever exposing an unauthenticated `/mcp`. |
| D5 | §3.1 | The Dockerfile and the GHCR release workflow land in phase 1 | Every phase is deployable. Phase 7 is the runbook and live acceptance. |
| D6 | §3.1 | SQL-invariant tests are written with the migration that creates each table | The tables don't exist in phase 1. |
| D7 | §3.2.4 | `oauth_grant.last_used_at` is updated on every refresh, and on verification at most once a minute per grant | `my_connections` shows "last used" without a write per request. |
| D8 | §3.3.7 | `get_my_library` sort keys; `get_book` by `book_id` only; the `reading_stats` period forms; pages from print/ebook reads and hours from audio reads; goals live; `search_books` sort values | The signatures were fixed, and their enumerations weren't. |
| D9 | §3.3.7 | `check_books`: `exact` = the matcher's fuzzy auto-accept rule; `likely` = title ≥ 85 and author ≥ 80 with margin ≥ 10; anything else `not_found` | "Never guesses" was decided, and the band wasn't. Recalibrate it alongside the matcher. |
| D10 | §3.4a.2 | The required CSV header set; `Spoiler` empty → false, else true | Spoiler's value form is unverified (empty in the sample). |
| D11 | §3.4a.3 | Binding → format table | **Confirmed** by the owner in [Goodreads backfill: when it runs, from which feed, and what a field's first observation means](https://github.com/ryansull/syncshelf/issues/32). |
| D12 | §3.4a.4 | A vanished book is detected by a by-id lookup returning no row | The ticket decided the reaction, not the detection point. |
| D13 | §3.4a.7 | `runs` rows only for `gr_rss`, `gr_csv` and `abs`; `mirror` and `chaptarr_watch` reserved | `mirror_state` and `chaptarr_requests` already carry that state; a row every 2 minutes would be noise. |
| D14 | §3.4a.8 | `/ingest` uses `Authorization: Bearer <secret>`, a file part `file`, an optional `generated_at` field, and returns 202 `{batch_id, run}` | The ticket decided per-profile secrets, not the wire shape. |
| D15 | §3.4a.10 | `sync_status` gains `gone: {total}` | The diff ticket says gone rows are "counted in `sync_status`"; the observability payload omitted it. |
| D16 | §3.4a.11 | `sync_now(source = goodreads \| abs \| all, full = false)`; `resolve_match` applies without a `runs` row (`change_log.run_id = NULL`) | The tickets used `sync_now(goodreads, full=true)` informally. |
| D17 | §3.5.1 | ABS item format = `audio` if `numAudioFiles > 0`, else `ebook` | An audiobook with a supplementary ebook is an audio item. Confirm at the phase 5 entry gate. |
| D18 | §3.6.2 | Chaptarr per-format state derivation and precedence | The state names were decided, and the derivation wasn't. |
| D19 | §3.6.4 | A no-op request is recorded as `noop` | The `noop` state exists in the schema, and this gives `requested_by` history. |
| D20 | §3.7 | `docs/runbook.md` and `deploy/compose.example.yaml` | Paths only. |
| D21 | §3.3.9 | Edit tool details: status slugs with no "none"; `rating=0` and `review=""` clear; a lone date implies its status; `started_on` is refused over an open read; Paused/DNF leave reads alone; `get_book` returns read ids; `update_book` isn't `destructiveHint`; the confirm-token helper lands in phase 3 | #31 decided the tools and their rules, not these enumerations. |

**Proposed after the map closed** (§2.10). No ticket covered process. These are open to veto until the PR that adds them merges, and bind after that.

| # | Where | Choice | Why |
|---|---|---|---|
| D22 | §2.10 | Short-lived `phase-<n>/<topic>` branches; one-thing PRs under ~400 lines; several PRs per phase | Reviewable diffs, and `main` stays deployable between PRs. |
| D23 | §2.10 | A PR template with Summary, Spec, Tests, Discrepancies and a checklist | Makes the "don't decide in code" rule visible at review time. |
| D24 | §2.10 | The owner reviews and merges, agents never merge their own PR, squash merges, branch protection on `main` | One reviewer, linear history, CI as the gate. |
| D25 | §2.10 | `v0.<n>.<patch>` per phase release, with `v1.0.0` at phase 7 acceptance, plus a *Keep a Changelog* `CHANGELOG.md` | Phases map to versions, and image tags say which phase is deployed. |
| D26 | §2.10 | `README.md` from phase 1 | The spec had no entry point for someone landing on the public repo. |
| D27 | §2.10 | Docstrings on public code; MCP tool docstrings written for Claude and changed with behaviour | The tool description is the only interface documentation Claude sees. |
| D28 | §2.1, §2.10 | `pyright` basic in CI; coverage reported, never gated | Cheap type safety; a coverage gate would reward the wrong tests. |
| D29 | §2.10 | Dependabot weekly for `uv` and Actions | Boring upkeep, with contract tests as the guard. |

---

## Appendix E: External versions this spec was checked against

| Thing | Version (2026-10-17) | Notes |
|---|---|---|
| MCP Python SDK | `mcp` 2.3.0 | v2 stable since 2026-07-28; 1.x maintenance-only |
| MCP spec | 2026-07-28 | DCR deprecated in favour of CIMD |
| CIMD draft | `draft-ietf-oauth-client-id-metadata-document-02` | 5 KB cap, no redirects, only 200 counts |
| Starlette / uvicorn / httpx2 | 1.8.0 / 0.54.0 / 2.13.1 | |
| Python | 3.14 | 3.15.0 released 2026-10-09, too new |
| Base image | `python:3.14-slim` (Debian trixie), SQLite 3.46.1 | inside the WAL-reset range, mitigated by the single writer |
| Hardcover API | `api.hardcover.app/v1/graphql`, Free plan | in beta: "heavily in flux"; depth cap of 3 announced |
| Audiobookshelf | 2.37.1 | live instance had no libraries; shapes from source |
| Chaptarr | 0.9.965.0 (`develop`), source `e67374f` | pre-1.0; re-check at build |
| Goodreads | RSS and export endpoints observed 2026-10-17 | the site is migrating to Next.js |
| Playwright | pinned at phase 4b build | image tag and Python client in lockstep |
