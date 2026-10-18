"""The client gate, ``redirect_allowed`` and the CIMD fetcher (SPEC §3.2.3, §3.2.6), as the
§3.2.8 level 1 unit tables give them."""

import gzip
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing

import anyio
import httpx2
import pytest

from syncshelf.auth.cimd import (
    CLAUDE_APPS,
    CLAUDE_CODE,
    FRESH_MAX,
    FRESH_MIN,
    HOSTED_CALLBACK,
    MAX_DOCUMENT_BYTES,
    STALE_WINDOW,
    ClientDocuments,
    ClientRefused,
    DocumentError,
    check_client,
    client_allowed,
    freshness,
    parse_document,
    redirect_allowed,
)
from syncshelf.db import Database

from cimd_docs import CLAUDE_APPS_DOC, CLAUDE_CODE_DOC, FakeClaude, document_response
from clock import FakeClock

pytestmark = pytest.mark.anyio

REJECTED_IDS = [
    "http://claude.ai/x",
    "https://claude.ai.evil.com/x",
    "https://evil.com/claude.ai/x",
    "https://claude.ai:443/x",
    "https://u@claude.ai/x",
    "https://claude.ai/",
    "https://claude.ai",
    "https://claude.ai/a/../b",
    "https://claude.ai/oauth/claude-code-client-metadata?q",
    "https://claude.ai/oauth/claude-code-client-metadata#f",
    "https://claude.ai/oauth/claude-code-client-metadata/",
    "https://claude.ai/oauth/other-client-metadata",
    "https://CLAUDE.ai/oauth/claude-code-client-metadata",
    "",
    "https://claude.ai/" + "a" * 255,
]


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
async def clients(
    db: Database, clock: FakeClock, fake: FakeClaude
) -> AsyncIterator[ClientDocuments]:
    documents = ClientDocuments(clock=clock.time, transport=fake.transport)
    async with documents.running(db):
        yield documents


def cached_rows(db: Database) -> list[tuple]:
    with closing(sqlite3.connect(db.path)) as conn:
        return conn.execute(
            "SELECT client_id, fetched_at, fresh_until FROM oauth_client_cache"
        ).fetchall()


class TestClientGate:
    @pytest.mark.parametrize("client_id", [CLAUDE_APPS, CLAUDE_CODE])
    def test_it_accepts_the_two_allowlisted_urls(self, client_id: str) -> None:
        assert client_allowed(client_id)

    @pytest.mark.parametrize("client_id", REJECTED_IDS)
    def test_it_rejects(self, client_id: str) -> None:
        assert not client_allowed(client_id)

    @pytest.mark.parametrize("client_id", REJECTED_IDS)
    async def test_a_rejected_id_is_never_fetched(
        self, clients: ClientDocuments, fake: FakeClaude, client_id: str
    ) -> None:
        with pytest.raises(ClientRefused) as refused:
            await check_client(clients, client_id, HOSTED_CALLBACK)
        assert refused.value.reason == "client_not_allowed"
        with pytest.raises(DocumentError):
            await clients.get(client_id)
        assert fake.requests == []

    async def test_a_missing_id_or_redirect_is_never_fetched(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        with pytest.raises(ClientRefused):
            await check_client(clients, None, HOSTED_CALLBACK)
        with pytest.raises(ClientRefused) as refused:
            await check_client(clients, CLAUDE_APPS, None)
        assert refused.value.reason == "redirect_uri_missing"
        assert fake.requests == []


HOSTED = parse_document(CLAUDE_APPS, json.dumps(CLAUDE_APPS_DOC))
CODE = parse_document(CLAUDE_CODE, json.dumps(CLAUDE_CODE_DOC))


class TestRedirectAllowed:
    @pytest.mark.parametrize(
        ("document", "redirect_uri"),
        [
            (CODE, "http://127.0.0.1:53111/callback"),
            (CODE, "http://localhost:3118/callback"),
            (CODE, "http://localhost:1/callback"),
            (CODE, "http://localhost:65535/callback"),
            (HOSTED, HOSTED_CALLBACK),
        ],
    )
    def test_it_accepts(self, document, redirect_uri: str) -> None:
        assert redirect_allowed(document, redirect_uri)

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://localhost:1/callback",
            "http://localhost@evil.com/callback",
            "http://u@localhost:3118/callback",
            "http://localhost:3118@evil.com/callback",
            "http://localhost.evil.com:3118/callback",
            "http://localhost:3118/callback/",
            "http://localhost:3118/callback?x=1",
            "http://localhost:3118/callback#f",
            "http://localhost:3118/%63allback",
            "http://localhost:3118/a/../callback",
            "http://localhost:3118/./callback",
            "http://localhost/callback",  # the policy wants a port
            "http://localhost:0/callback",
            "http://localhost:65536/callback",
            "http://localhost:03118/callback",
            "http://[::1]:3118/callback",  # registered nowhere, and not in the policy
            "http://LOCALHOST:3118/callback",
            "",
        ],
    )
    def test_it_rejects_for_claude_code(self, redirect_uri: str) -> None:
        assert not redirect_allowed(CODE, redirect_uri)

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            HOSTED_CALLBACK + "/",
            HOSTED_CALLBACK + "?x=1",
            "https://claude.ai:443/api/mcp/auth_callback",
            "http://claude.ai/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/%61uth_callback",
        ],
    )
    def test_it_rejects_for_the_hosted_doc(self, redirect_uri: str) -> None:
        assert not redirect_allowed(HOSTED, redirect_uri)

    def test_each_clients_redirect_is_refused_under_the_others_id(self) -> None:
        assert not redirect_allowed(CODE, HOSTED_CALLBACK)
        assert not redirect_allowed(HOSTED, "http://127.0.0.1:53111/callback")
        assert not redirect_allowed(HOSTED, "http://localhost:3118/callback")

    def test_the_policy_holds_whatever_a_document_registers(self) -> None:
        wide = parse_document(
            CLAUDE_CODE,
            json.dumps(
                {
                    **CLAUDE_CODE_DOC,
                    "redirect_uris": [
                        "https://evil.com/callback",
                        "http://localhost/callback/",
                        "http://[::1]/callback",
                    ],
                }
            ),
        )
        assert not redirect_allowed(wide, "https://evil.com/callback")
        assert not redirect_allowed(wide, "http://localhost:3118/callback/")
        assert not redirect_allowed(wide, "http://[::1]:3118/callback")

    def test_the_loopback_rule_needs_a_portless_registration(self) -> None:
        pinned = parse_document(
            CLAUDE_CODE,
            json.dumps({**CLAUDE_CODE_DOC, "redirect_uris": ["http://localhost:8080/callback"]}),
        )
        assert redirect_allowed(pinned, "http://localhost:8080/callback")
        assert not redirect_allowed(pinned, "http://localhost:3118/callback")

    async def test_check_client_refuses_a_redirect_without_redirecting(
        self, clients: ClientDocuments
    ) -> None:
        with pytest.raises(ClientRefused) as refused:
            await check_client(clients, CLAUDE_APPS, "http://localhost:3118/callback")
        assert refused.value.reason == "redirect_uri_not_allowed"

    async def test_check_client_trusts_a_matching_request(self, clients: ClientDocuments) -> None:
        trusted = await check_client(clients, CLAUDE_CODE, "http://127.0.0.1:53111/callback")
        assert trusted.client_id == CLAUDE_CODE
        assert trusted.client_label == "Claude Code"
        assert trusted.redirect_uri == "http://127.0.0.1:53111/callback"
        assert trusted.refresh_allowed
        hosted = await check_client(clients, CLAUDE_APPS, HOSTED_CALLBACK)
        assert hosted.client_label == "claude.ai"


class TestDocumentChecks:
    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"client_id": CLAUDE_APPS}, "client_id_mismatch"),
            ({"client_id": None}, "client_id_mismatch"),
            ({"redirect_uris": []}, "bad_redirect_uris"),
            ({"redirect_uris": "http://localhost/callback"}, "bad_redirect_uris"),
            ({"redirect_uris": [1]}, "bad_redirect_uris"),
            ({"token_endpoint_auth_method": "client_secret_basic"}, "not_a_public_client"),
            ({"client_secret": "x"}, "not_a_public_client"),
            ({"grant_types": ["refresh_token"]}, "no_authorization_code_grant"),
            ({"grant_types": None}, "no_authorization_code_grant"),
        ],
    )
    def test_it_refuses(self, change: dict, reason: str) -> None:
        doc = {**CLAUDE_CODE_DOC, **change}
        with pytest.raises(DocumentError) as error:
            parse_document(CLAUDE_CODE, json.dumps(doc))
        assert error.value.reason == reason

    @pytest.mark.parametrize("body", ["[]", "not json", '"x"', b"\xff\xfe"])
    def test_it_refuses_anything_but_a_json_object(self, body: str | bytes) -> None:
        with pytest.raises(DocumentError):
            parse_document(CLAUDE_CODE, body)

    def test_absent_auth_method_is_a_public_client(self) -> None:
        doc = {k: v for k, v in CLAUDE_CODE_DOC.items() if k != "token_endpoint_auth_method"}
        assert parse_document(CLAUDE_CODE, json.dumps(doc)).client_id == CLAUDE_CODE

    def test_refresh_is_allowed_only_with_the_refresh_token_grant(self) -> None:
        assert HOSTED.refresh_allowed
        doc = {**CLAUDE_CODE_DOC, "grant_types": ["authorization_code"]}
        assert not parse_document(CLAUDE_CODE, json.dumps(doc)).refresh_allowed


class TestFreshness:
    @pytest.mark.parametrize(
        ("header", "seconds"),
        [
            ("public, max-age=300", 300),
            ("max-age=3600", 3600),
            ("max-age=10", FRESH_MIN),
            ("max-age=999999", FRESH_MAX),
            ("no-cache", FRESH_MIN),
            ("max-age=abc", FRESH_MIN),
            (None, FRESH_MIN),
        ],
    )
    def test_max_age_is_clamped(self, header: str | None, seconds: int) -> None:
        assert freshness(header) == seconds


class TestFetcher:
    async def test_it_fetches_and_caches_a_valid_document(
        self, clients: ClientDocuments, fake: FakeClaude, db: Database, clock: FakeClock
    ) -> None:
        document = await clients.get(CLAUDE_CODE)
        assert document.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
        (request,) = fake.requests
        assert str(request.url) == CLAUDE_CODE
        now = int(clock.time())
        assert cached_rows(db) == [(CLAUDE_CODE, now, now + 300)]

    @pytest.mark.parametrize(
        "response",
        [
            httpx2.Response(302, headers={"location": "https://claude.ai/elsewhere"}),
            httpx2.Response(301, headers={"location": CLAUDE_CODE}),
            httpx2.Response(304),
            httpx2.Response(404),
            httpx2.Response(500),
            httpx2.Response(201, json=CLAUDE_CODE_DOC),
            document_response(CLAUDE_CODE_DOC, **{"content-type": "text/html"}),
            document_response(CLAUDE_CODE_DOC, **{"content-type": "application/jsonp"}),
            httpx2.Response(200, content=json.dumps(CLAUDE_CODE_DOC).encode()),
            document_response({**CLAUDE_CODE_DOC, "client_id": CLAUDE_APPS}),
        ],
        ids=["302", "301", "304", "404", "500", "201", "html", "jsonp", "no-type", "mismatch"],
    )
    async def test_a_bad_response_fails_and_is_not_cached(
        self, clients: ClientDocuments, fake: FakeClaude, db: Database, response: httpx2.Response
    ) -> None:
        fake.respond = lambda request: response
        with pytest.raises(DocumentError):
            await clients.get(CLAUDE_CODE)
        assert len(fake.requests) == 1  # a redirect is never followed
        assert cached_rows(db) == []

    async def test_a_json_media_type_with_parameters_is_accepted(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        fake.respond = lambda request: document_response(
            CLAUDE_CODE_DOC, **{"content-type": "application/json; charset=utf-8"}
        )
        assert (await clients.get(CLAUDE_CODE)).client_id == CLAUDE_CODE

    @staticmethod
    def padded(size: int) -> httpx2.Response:
        body = json.dumps(CLAUDE_CODE_DOC).encode()
        body += b" " * (size - len(body))
        assert len(body) == size
        return httpx2.Response(200, content=body, headers={"content-type": "application/json"})

    async def test_a_body_of_5_kb_is_accepted(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        fake.respond = lambda request: self.padded(MAX_DOCUMENT_BYTES)
        assert (await clients.get(CLAUDE_CODE)).client_id == CLAUDE_CODE

    async def test_a_body_of_5_kb_plus_1_is_refused(
        self, clients: ClientDocuments, fake: FakeClaude, db: Database
    ) -> None:
        fake.respond = lambda request: self.padded(MAX_DOCUMENT_BYTES + 1)
        with pytest.raises(DocumentError) as error:
            await clients.get(CLAUDE_CODE)
        assert error.value.reason == "too_large"
        assert cached_rows(db) == []

    async def test_a_transport_error_fails(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        def fail(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectTimeout("timed out", request=request)

        fake.respond = fail
        with pytest.raises(DocumentError) as error:
            await clients.get(CLAUDE_CODE)
        assert error.value.reason.startswith("transport_")

    async def test_max_age_is_honoured(
        self, clients: ClientDocuments, fake: FakeClaude, clock: FakeClock
    ) -> None:
        fake.respond = lambda request: document_response(
            CLAUDE_CODE_DOC, **{"cache-control": "max-age=3600"}
        )
        await clients.get(CLAUDE_CODE)
        clock.advance(3599)
        await clients.get(CLAUDE_CODE)
        assert len(fake.requests) == 1
        clock.advance(1)
        await clients.get(CLAUDE_CODE)
        assert len(fake.requests) == 2

    async def test_errors_are_never_cached(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        fake.respond = lambda request: httpx2.Response(503)
        with pytest.raises(DocumentError):
            await clients.get(CLAUDE_CODE)
        fake.respond = None
        assert (await clients.get(CLAUDE_CODE)).client_id == CLAUDE_CODE
        assert len(fake.requests) == 2

    async def test_stale_on_error_for_up_to_7_days(
        self, clients: ClientDocuments, fake: FakeClaude, clock: FakeClock, db: Database
    ) -> None:
        await clients.get(CLAUDE_CODE)
        fake.respond = lambda request: httpx2.Response(503)
        clock.advance(300 + STALE_WINDOW - 1)
        assert (await clients.get(CLAUDE_CODE)).client_id == CLAUDE_CODE
        assert len(fake.requests) == 2  # it did try to refresh
        start = int(FakeClock.START)
        assert cached_rows(db) == [(CLAUDE_CODE, start, start + 300)]  # the failure kept it
        clock.advance(1)
        with pytest.raises(DocumentError):
            await clients.get(CLAUDE_CODE)

    async def test_a_new_valid_document_replaces_the_cached_one(
        self, clients: ClientDocuments, fake: FakeClaude, clock: FakeClock, db: Database
    ) -> None:
        await clients.get(CLAUDE_CODE)
        clock.advance(300)
        fake.respond = lambda request: document_response(
            {**CLAUDE_CODE_DOC, "grant_types": ["authorization_code"]}, etag='"v2"'
        )
        assert not (await clients.get(CLAUDE_CODE)).refresh_allowed
        now = int(clock.time())
        assert cached_rows(db) == [(CLAUDE_CODE, now, now + 300)]

    async def test_one_fetch_at_a_time_per_url(
        self, db: Database, clock: FakeClock, fake: FakeClaude
    ) -> None:
        release = anyio.Event()
        calls: list[str] = []

        async def slow(request: httpx2.Request) -> httpx2.Response:
            calls.append(str(request.url))
            await release.wait()
            return fake(request)

        documents = ClientDocuments(clock=clock.time, transport=httpx2.MockTransport(slow))
        results = []

        async def get() -> None:
            results.append(await documents.get(CLAUDE_CODE))

        async with documents.running(db), anyio.create_task_group() as tg:
            tg.start_soon(get)
            with anyio.fail_after(5):
                while not calls:  # the first fetch is in flight and holds the lock
                    await anyio.sleep(0)
                for _ in range(4):
                    tg.start_soon(get)
                # The others found nothing cached and queued on the lock, not on the network.
                lock = documents._locks[CLAUDE_CODE]
                while len(lock._waiters or ()) < 4:
                    await anyio.sleep(0)
            release.set()
        assert calls == [CLAUDE_CODE]
        assert len(results) == 5

    async def test_waiters_share_a_failed_fetch_instead_of_queueing_another(
        self, db: Database, clock: FakeClock
    ) -> None:
        release = anyio.Event()
        calls: list[str] = []

        async def failing(request: httpx2.Request) -> httpx2.Response:
            calls.append(str(request.url))
            await release.wait()
            return httpx2.Response(503)

        documents = ClientDocuments(clock=clock.time, transport=httpx2.MockTransport(failing))
        failures: list[DocumentError] = []

        async def get() -> None:
            try:
                await documents.get(CLAUDE_CODE)
            except DocumentError as exc:
                failures.append(exc)

        async with documents.running(db):
            async with anyio.create_task_group() as tg:
                tg.start_soon(get)
                with anyio.fail_after(5):
                    while not calls:
                        await anyio.sleep(0)
                    for _ in range(4):
                        tg.start_soon(get)
                    lock = documents._locks[CLAUDE_CODE]
                    while len(lock._waiters or ()) < 4:
                        await anyio.sleep(0)
                release.set()
            assert calls == [CLAUDE_CODE]
            assert len(failures) == 5
            # A request arriving afterwards tries again: the failure isn't cached.
            release.set()
            with pytest.raises(DocumentError):
                await documents.get(CLAUDE_CODE)
            assert len(calls) == 2


class TestFetchedBodies:
    async def test_a_compressed_body_is_refused(
        self, clients: ClientDocuments, fake: FakeClaude
    ) -> None:
        body = gzip.compress(json.dumps(CLAUDE_CODE_DOC).encode())
        fake.respond = lambda request: httpx2.Response(
            200,
            content=body,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
        with pytest.raises(DocumentError) as error:
            await clients.get(CLAUDE_CODE)
        assert error.value.reason == "encoded_body"
        assert fake.requests[0].headers["accept-encoding"] == "identity"

    @pytest.mark.parametrize(
        "body",
        [
            b"\xef\xbb\xbf" + json.dumps(CLAUDE_CODE_DOC).encode(),
            json.dumps(CLAUDE_CODE_DOC).encode("utf-16"),
        ],
        ids=["bom", "utf-16"],
    )
    async def test_only_plain_utf8_is_accepted(
        self, clients: ClientDocuments, fake: FakeClaude, db: Database, body: bytes
    ) -> None:
        fake.respond = lambda request: httpx2.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
        with pytest.raises(DocumentError):
            await clients.get(CLAUDE_CODE)
        assert cached_rows(db) == []


def test_a_malformed_registered_uri_never_matches() -> None:
    doc = {
        **CLAUDE_CODE_DOC,
        "redirect_uris": ["http://[::1/callback", "http://localhost/callback"],
    }
    document = parse_document(CLAUDE_CODE, json.dumps(doc))
    assert redirect_allowed(document, "http://localhost:3118/callback")
    assert not redirect_allowed(document, "http://127.0.0.1:3118/callback")
