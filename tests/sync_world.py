"""One profile's whole sync world against the fakes (SPEC §2.9): its Goodreads feed, its
Hardcover shelves, the database, the mirror, the write procedure and the Goodreads engine,
all on the injectable clock. Tests and golden runners build one with :func:`world`."""

from collections.abc import AsyncIterator
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
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.sync.mirror import Mirror

from clock import FakeClock
from fakes.goodreads import FakeGoodreads
from fakes.hardcover import FakeHardcover
from serving import HARDCOVER_URL, hardcover_token, make_config

GR_USER = "31415926"
HC_USER = 42
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
) -> AsyncIterator[World]:
    """A world for one profile with a Goodreads section (``shelf_map`` as flow YAML), a
    Goodreads user and a Hardcover user, both empty. With ``reserve``, Hardcover calls go
    through a real limiter on the clock, keeping that much of the daily budget back."""
    clock = FakeClock()
    goodreads = FakeGoodreads(clock)
    goodreads.add_user(GR_USER)
    hardcover = FakeHardcover()
    hardcover.today = clock.now().date().isoformat()
    hardcover.add_user(hardcover_token(profile), user_id=HC_USER)
    body = f'{{goodreads: {{user_id: "{GR_USER}", shelf_map: {shelf_map}}}, timezone: Etc/UTC}}'
    config = make_config(tmp_path, profiles=[profile], profile_yaml={profile: body})

    def route(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "www.goodreads.com":
            return goodreads.handle(request)
        return hardcover.handle(request)

    transport = httpx2.MockTransport(route)
    db = await Database.open(tmp_path / "syncshelf.db", snapshot_dir=tmp_path)
    gate = None if reserve is None else HardcoverLimiter(profile, reserve=reserve, clock=clock)
    clients = {
        profile: HardcoverClient(
            HARDCOVER_URL, hardcover_token(profile), gate=gate, transport=transport
        )
    }
    sync = None
    try:
        await db.write(sync_profiles, [profile], int(clock.time()))
        mirror = Mirror(db, clients, ttl=600, clock=clock.time)
        applier = Applier(clients, mirror, config.profiles)
        sync = GoodreadsSync(
            db=db,
            profiles=config.profiles,
            hardcover=clients,
            mirror=mirror,
            applier=applier,
            clock=clock,
            transport=transport,
        )
        yield World(config, db, clock, goodreads, hardcover, sync)
    finally:
        if sync is not None:
            await sync.aclose()
        for client in clients.values():
            await client.aclose()
        await db.close()
