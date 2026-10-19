"""Record scrubbed fixtures from a live service (SPEC §2.9).

    uv run python tests/live/record.py hardcover --token-var HARDCOVER_TOKEN_ALICE
    uv run python tests/live/record.py goodreads_rss --user-var GOODREADS_USER_ID_ALICE

Credentials come from the repo-root ``.env`` (``--env`` to read another). Recording only
**reads**: it sends the same queries the client does, with a small page, and writes each
response through the scrubber, so no personal value reaches the committed file. Run the
fixture test afterwards (``uv run pytest tests/unit/test_fixtures.py``).
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from syncshelf.clients.goodreads_rss import (  # noqa: E402
    ALL_SHELF,
    MIN_INTERVAL,
    USER_AGENT,
    Unusable,
    page_items,
    rss_url,
)
from syncshelf.clients.hardcover import (  # noqa: E402
    BOOK,
    BOOKS_BY_ID,
    BY_GOODREADS,
    GOALS,
    LIBRARY,
    ME,
    SEARCH,
    SERIES,
    SHELF,
    SIMILAR,
    HardcoverClient,
)
from syncshelf.sync.limiter import HardcoverLimiter  # noqa: E402

from scrub import write_fixture, write_native_fixture  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"
HARDCOVER_VERSION = "api.hardcover.app/v1 (sends no version header)"
GOODREADS_IDS = ["247031753", "34873380", "57189884", "999999999999"]
GOODREADS_VERSION = "www.goodreads.com shelf RSS (sends no version header)"


def read_env(path: Path) -> dict[str, str]:
    """``KEY=value`` lines, ignoring comments and blanks, an ``export`` prefix and matching
    quotes around a value, as dotenv files allow. Values are never printed."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key.strip()] = value
    return env


async def record_hardcover(token: str, limit: int) -> None:
    # Gated like production, with no reserve: recording is a handful of reads.
    client = HardcoverClient(HARDCOVER_URL, token, gate=HardcoverLimiter("record", reserve=0))
    recorded_at = datetime.now(UTC).date().isoformat()
    try:
        me = await client.execute(ME)
        user_id = me.data["me"][0]["id"]
        variables = {"user_id": user_id, "limit": limit, "after": 0, "lists_after": 0}
        library = await client.execute(LIBRARY, variables)
        goals = await client.execute(GOALS)
        # A shelved book with reads if there is one, so the fixture shows a read's shape.
        shelved = library.data["user_books"]
        if not shelved:
            sys.exit("the library is empty: shelve a book on this account and record again")
        pick = next((ub for ub in shelved if ub["user_book_reads"]), shelved[0])
        ids = {"book_id": pick["book_id"], "user_id": user_id}
        book = await client.execute(BOOK, ids)
        shelf = await client.execute(SHELF, ids)
        similar_ids = (book.data["books"][0]["cached_similar_book_ids"] or [])[:10]
        similar = await client.execute(SIMILAR, {"ids": similar_ids})
        series_ids = [
            s["series"]["id"]
            for ub in shelved
            if (s := ub["book"]["cached_featured_series"]) and s.get("series")
        ][:2]
        series = await client.execute(SERIES, {"ids": series_ids})
        by_id = await client.execute(BOOKS_BY_ID, {"ids": [pick["book_id"]]})
        search = await client.execute(
            SEARCH, {"q": "dune", "filter": None, "sort": None, "per": 3, "page": 1}
        )
        # Public Goodreads edition ids (from the RSS fixture), and one Goodreads doesn't have.
        by_goodreads = await client.execute(BY_GOODREADS, {"ids": GOODREADS_IDS})
    finally:
        await client.aclose()
    out = FIXTURES / "hardcover"
    recorded = {
        "me": me.data,
        "library": library.data,
        "goals": goals.data,
        "book": book.data,
        "similar": similar.data,
        "shelf": shelf.data,
        "series": series.data,
        "search": search.data,
        "books_by_id": by_id.data,
        "by_goodreads": by_goodreads.data,
    }
    for name, data in recorded.items():
        write_fixture(
            out / f"{name}.json",
            {"data": data},
            recorded_at=recorded_at,
            server_version=HARDCOVER_VERSION,
        )
        print(f"wrote {out / f'{name}.json'}")


async def record_goodreads_rss(user_id: str, limit: int) -> None:
    """Two anonymous reads of a public profile's feed, paced like production: a first page of
    ``limit`` items, and a page past the end (HTTP 200 with no items). Both must pass the
    client's usable-page check before either file is written."""
    recorded_at = datetime.now(UTC).date().isoformat()
    out = FIXTURES / "goodreads"
    pages = {"rss_page": 1, "rss_end": 100_000}
    bodies: dict[str, str] = {}
    async with httpx2.AsyncClient(headers={"user-agent": USER_AGENT}, timeout=30) as http:
        for i, (name, page) in enumerate(pages.items()):
            if i:
                await asyncio.sleep(MIN_INTERVAL)
            params = {"shelf": ALL_SHELF, "per_page": str(limit), "page": str(page)}
            response = await http.get(rss_url(user_id), params=params)
            try:
                found = page_items(response)
            except Unusable as exc:
                sys.exit(f"{name}: {exc}; nothing written")
            if bool(found) != (name == "rss_page"):
                sys.exit(
                    f"{name}: expected {'items' if name == 'rss_page' else 'none'}; nothing written"
                )
            bodies[name] = response.text
    for name, body in bodies.items():
        write_native_fixture(
            out / f"{name}.xml", body, recorded_at=recorded_at, server_version=GOODREADS_VERSION
        )
        print(f"wrote {out / f'{name}.xml'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=["hardcover", "goodreads_rss"])
    parser.add_argument("--token-var", help="hardcover: the .env variable holding the PAT")
    parser.add_argument("--user-var", help="goodreads_rss: the .env variable holding the user id")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--limit", type=int, default=5, help="user_books or feed items to record")
    args = parser.parse_args()
    var = args.token_var if args.source == "hardcover" else args.user_var
    if not var:
        sys.exit(
            "--token-var is required" if args.source == "hardcover" else "--user-var is required"
        )
    value = read_env(args.env).get(var)
    if not value:
        sys.exit(f"{var} is not set in {args.env}")
    if args.source == "hardcover":
        asyncio.run(record_hardcover(value, args.limit))
    else:
        asyncio.run(record_goodreads_rss(value, args.limit))


if __name__ == "__main__":
    main()
