"""A household's whole sync world against the fakes (SPEC §2.9): each profile's Goodreads
feed and Hardcover shelves, the database, the mirror, the write procedure and the Goodreads
engine, all on the injectable clock. Tests and golden runners build one with :func:`world`."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.config import Config
from syncshelf.db import Database
from syncshelf.profiles import sync_profiles
from syncshelf.sync.apply import Applier
from syncshelf.sync.goodreads import GoodreadsSync
from syncshelf.sync.goodreads_session import SignIns
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.sync.mirror import Mirror

from clock import FakeClock
from fakes.browser import FakeBrowser, signed_in
from fakes.goodreads import FakeGoodreads
from fakes.hardcover import FakeHardcover
from serving import (
    HARDCOVER_URL,
    SIDECAR,
    credential_env,
    hardcover_token,
    make_config,
)

GR_USER = "31415926"
HC_USER = 42
GR_USERS = {"alice": GR_USER, "bob": "27182818"}
"""Each profile's Goodreads user id."""
HC_USERS = {"alice": HC_USER, "bob": 43}
"""Each profile's Hardcover user id."""
SHELF_MAP = "{read: 3, currently-reading: 2, to-read: 1, did-not-finish: 5}"


@dataclass
class World:
    """Everything one test drives and inspects."""

    config: Config
    db: Database
    clock: FakeClock
    goodreads: FakeGoodreads
    hardcover: FakeHardcover
    sync: GoodreadsSync
    mirror: Mirror
    sign_ins: SignIns | None = None
    """The real holder over a fake browser, in a ``csv`` world. ``None`` without one."""
    browser: FakeBrowser | None = None

    async def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        """Run a read query against the database."""
        return await self.db.read(lambda conn: conn.execute(sql, params).fetchall())

    def hardcover_calls(self) -> list[str]:
        """The Hardcover operations sent so far, by name."""
        return [op for op, _ in self.hardcover.requests]


@asynccontextmanager
async def world(
    tmp_path: Path,
    *,
    profile: str = "alice",
    shelf_map: str = SHELF_MAP,
    reserve: int | None = None,
    others: Sequence[str] = (),
    csv: bool = False,
) -> AsyncIterator[World]:
    """A world for ``profile`` (and the ``others``, from ``bob``), each with a Goodreads
    section (``shelf_map`` as flow YAML), a Goodreads user and a Hardcover user, all empty.
    With ``reserve``, Hardcover calls go through a real limiter on the clock, keeping that
    much of the daily budget back. With ``csv``, every profile also carries Goodreads
    credentials and the engine gets the jars and a real sign-in holder over a fake browser,
    so its CSV source runs (§3.4b.5)."""
    names = [profile, *others]
    clock = FakeClock()
    goodreads = FakeGoodreads(clock)
    hardcover = FakeHardcover()
    hardcover.today = clock.now().date().isoformat()
    bodies = {}
    for name in names:
        goodreads.add_user(GR_USERS[name])
        hardcover.add_user(hardcover_token(name), user_id=HC_USERS[name])
        credentials = ', email: "${GR_EMAIL}", password: "${GR_PASSWORD}"' if csv else ""
        bodies[name] = (
            f'{{goodreads: {{user_id: "{GR_USERS[name]}", shelf_map: {shelf_map}'
            f"{credentials}}}, timezone: Etc/UTC}}"
        )
    config = make_config(
        tmp_path,
        profiles=names,
        profile_yaml=bodies,
        browser_ws=SIDECAR if csv else None,
        environ=credential_env() if csv else None,
    )

    def route(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "www.goodreads.com":
            return goodreads.handle(request)
        return hardcover.handle(request)

    transport = httpx2.MockTransport(route)
    db = await Database.open(tmp_path / "syncshelf.db", snapshot_dir=tmp_path)
    clients = {
        name: HardcoverClient(
            HARDCOVER_URL,
            hardcover_token(name),
            gate=None if reserve is None else HardcoverLimiter(name, reserve=reserve, clock=clock),
            transport=transport,
        )
        for name in names
    }
    sync = None
    try:
        await db.write(sync_profiles, names, int(clock.time()))
        mirror = Mirror(db, clients, ttl=600, clock=clock.time)
        applier = Applier(clients, mirror, config.profiles)
        browser = FakeBrowser(signed_in()) if csv else None
        sign_ins = (
            SignIns(browser, data_dir=tmp_path, db=db, clock=clock.time)
            if browser is not None
            else None
        )
        sync = GoodreadsSync(
            db=db,
            profiles=config.profiles,
            hardcover=clients,
            mirror=mirror,
            applier=applier,
            clock=clock,
            data_dir=tmp_path if csv else None,
            sign_ins=sign_ins,
            transport=transport,
        )
        yield World(config, db, clock, goodreads, hardcover, sync, mirror, sign_ins, browser)
    finally:
        if sync is not None:
            await sync.aclose()
        for client in clients.values():
            await client.aclose()
        await db.close()
