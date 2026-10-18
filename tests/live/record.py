"""Record scrubbed fixtures from a live service (SPEC §2.9).

    uv run python tests/live/record.py hardcover --token-var HARDCOVER_TOKEN_ALICE

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from syncshelf.clients.hardcover import GOALS, LIBRARY, ME, HardcoverClient  # noqa: E402
from syncshelf.sync.limiter import HardcoverLimiter  # noqa: E402

from scrub import write_fixture  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"
HARDCOVER_VERSION = "api.hardcover.app/v1 (sends no version header)"


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
        library = await client.execute(LIBRARY, {"user_id": user_id, "limit": limit, "offset": 0})
        goals = await client.execute(GOALS)
    finally:
        await client.aclose()
    out = FIXTURES / "hardcover"
    for name, data in (("me", me.data), ("library", library.data), ("goals", goals.data)):
        write_fixture(
            out / f"{name}.json",
            {"data": data},
            recorded_at=recorded_at,
            server_version=HARDCOVER_VERSION,
        )
        print(f"wrote {out / f'{name}.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=["hardcover"])
    parser.add_argument("--token-var", required=True, help="the .env variable holding the PAT")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--limit", type=int, default=5, help="user_books to record")
    args = parser.parse_args()
    token = read_env(args.env).get(args.token_var)
    if not token:
        sys.exit(f"{args.token_var} is not set in {args.env}")
    asyncio.run(record_hardcover(token, args.limit))


if __name__ == "__main__":
    main()
