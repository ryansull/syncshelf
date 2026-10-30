"""``POST /ingest/goodreads-csv`` through the app (SPEC §3.4a.8): the secret names the profile,
the header is checked, the batch is stored (the last 3 kept), the loop is nudged, and 202
comes back without waiting on a sync."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from syncshelf.app import create_app
from syncshelf.sync.scheduler import Scheduler

from clock import FakeClock
from fakes.goodreads_csv import HEADER, ExportBook, library_export
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, running

pytestmark = pytest.mark.anyio

ALICE = "alice-ingest-secret-0123456789abcdef"
BOB = "bob-ingest-secret-0123456789abcdef00"
URL = "/ingest/goodreads-csv"
GENERATED = int(datetime(2026, 10, 15, 8, tzinfo=UTC).timestamp())
EXPORT = library_export(ExportBook(10, "Dune", rating=4), ExportBook(11, "Emma"))


@pytest.fixture
async def http(
    tmp_path: Path, data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx2.AsyncClient, list[tuple[str, str]]]]:
    nudged: list[tuple[str, str]] = []
    monkeypatch.setattr(Scheduler, "nudge", lambda self, p, t: nudged.append((p, t)))
    goodreads = '{user_id: "1", shelf_map: {read: 3, currently-reading: 2, to-read: 1}}'
    bodies = {}
    for name, secret in (("alice", ALICE), ("bob", BOB)):
        (tmp_path / f"ingest_{name}").write_text(secret, encoding="utf-8")
        bodies[name] = (
            f"{{ingest_secret_file: {(tmp_path / f'ingest_{name}').as_posix()}, "
            f"goodreads: {goodreads}, timezone: Etc/UTC}}"
        )
    bodies["carol"] = "{}"
    fake = FakeHardcover()
    for n, name in enumerate(("alice", "bob", "carol")):
        fake.add_user(hardcover_token(name), user_id=42 + n)
    app = create_app(
        make_config(tmp_path, profiles=["alice", "bob", "carol"], profile_yaml=bodies),
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )
    async with running(app, token=None) as client:
        yield client, nudged


async def post(
    client: httpx2.AsyncClient, secret: str | None, data: bytes = EXPORT, **fields: str
) -> httpx2.Response:
    headers = {"authorization": f"Bearer {secret}"} if secret is not None else {}
    return await client.post(URL, headers=headers, files={"file": ("x.csv", data)}, data=fields)


async def stored(client: httpx2.AsyncClient) -> list[tuple[Any, ...]]:
    db = client._transport.app.state.db  # type: ignore[attr-defined]  # noqa: SLF001
    return await db.read(
        lambda conn: conn.execute(
            "SELECT id, profile, source, observed_at, stale, complete, row_count, processed_at "
            "FROM ingest_batches ORDER BY id"
        ).fetchall()
    )


async def test_the_secret_names_the_profile(http: Any) -> None:
    client, nudged = http
    response = await post(client, BOB, generated_at="2026-10-15T08:00:00Z")
    assert response.status_code == 202
    assert response.json() == {"batch_id": 1, "run": "started", "stale": False}
    assert await stored(client) == [(1, "bob", "gr_csv_ingest", GENERATED, 0, 1, 2, None)]
    assert nudged == [("bob", "ingest")]


@pytest.mark.parametrize("secret", [None, "wrong", ALICE[:-1], f"{ALICE}x"])
async def test_an_unknown_secret_is_refused(http: Any, secret: str | None) -> None:
    client, nudged = http
    response = await post(client, secret)
    assert response.status_code == 401
    assert await stored(client) == [] and nudged == []


async def test_a_bad_header_names_the_missing_columns(http: Any) -> None:
    client, _ = http
    header = tuple(c for c in HEADER if c != "Exclusive Shelf")
    response = await post(client, ALICE, library_export(ExportBook(10, "Dune"), header=header))
    assert response.status_code == 400
    assert response.json()["missing"] == ["Exclusive Shelf"]
    assert await stored(client) == []


@pytest.mark.parametrize(
    ("data", "fields", "says"),
    [
        (b"\xff\xfe not text", {}, "UTF-8"),
        (EXPORT, {"generated_at": "yesterday"}, "ISO-8601"),
        (EXPORT, {"generated_at": "2026-10-15T08:00:00"}, "ISO-8601"),  # no offset
    ],
)
async def test_an_upload_it_cant_read_is_refused(
    http: Any, data: bytes, fields: dict[str, str], says: str
) -> None:
    client, _ = http
    response = await post(client, ALICE, data, **fields)
    assert response.status_code == 400
    assert says in response.json()["error"]


async def test_the_export_must_be_a_file_part(http: Any) -> None:
    client, _ = http
    response = await client.post(
        URL, headers={"authorization": f"Bearer {ALICE}"}, data={"file": "Book Id,Title"}
    )
    assert response.status_code == 400
    assert "file part" in response.json()["error"]


async def test_without_generated_at_the_batch_is_stale(http: Any) -> None:
    client, _ = http
    answer = (await post(client, ALICE)).json()
    assert answer["stale"] is True
    assert "generated_at" in answer["hint"]
    ((_, _, _, observed_at, stale, *_),) = await stored(client)
    assert (observed_at, stale) == (None, 1)


async def test_a_running_sync_is_named(http: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, nudged = http
    monkeypatch.setattr(Scheduler, "running", lambda self, p: (7, 0))
    answer = (await post(client, ALICE, generated_at="2026-10-15T08:00:00+01:00")).json()
    assert answer["run"] == "already_running"
    assert nudged == [("alice", "ingest")]  # the next run picks it up


async def test_the_last_three_are_kept(http: Any) -> None:
    client, _ = http
    for _ in range(4):
        assert (await post(client, ALICE)).status_code == 202
    await post(client, BOB)
    assert [(i, p) for i, p, *_ in await stored(client)] == [
        (2, "alice"),
        (3, "alice"),
        (4, "alice"),
        (5, "bob"),
    ]


async def test_a_profile_without_goodreads_is_refused(
    http: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = http
    profiles = client._transport.app.state.profiles  # type: ignore[attr-defined]  # noqa: SLF001
    carol = profiles["carol"]
    monkeypatch.setitem(
        profiles, "carol", type(carol)(**{**carol.__dict__, "ingest_secret": "carol-secret"})
    )
    response = await post(client, "carol-secret")
    assert response.status_code == 400
    assert "no goodreads section" in response.json()["error"]
