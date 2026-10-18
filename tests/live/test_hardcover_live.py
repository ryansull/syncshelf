"""The Hardcover client against the real API, reads only (SPEC §3.3.10, Live).

Every ``HARDCOVER_TOKEN_*`` profile token in the env file is used, since reads may use the real
tokens. ``HARDCOVER_TOKEN_TEST`` is left to the write tests.
"""

import pytest

from syncshelf.clients.hardcover import LIBRARY_PAGE, HardcoverClient

URL = "https://api.hardcover.app/v1/graphql"

pytestmark = [pytest.mark.live, pytest.mark.anyio]


def profile_tokens(env: dict[str, str]) -> dict[str, str]:
    return {
        var: value
        for var, value in env.items()
        if var.startswith("HARDCOVER_TOKEN_") and var != "HARDCOVER_TOKEN_TEST" and value
    }


async def test_each_profile_reads_its_own_whole_library(live_env: dict[str, str]) -> None:
    tokens = profile_tokens(live_env)
    assert tokens, "no HARDCOVER_TOKEN_<PROFILE> in the env file"
    for var, token in tokens.items():
        client = HardcoverClient(URL, token)
        try:
            user_id = await client.me_id()
            library = await client.library()
        finally:
            await client.aclose()
        assert isinstance(user_id, int), var
        assert len({ub["id"] for ub in library.user_books}) == len(library.user_books), var
        # The first page costs 2 (user_books + lists), each later page 1.
        pages = len(library.user_books) // LIBRARY_PAGE + 1
        assert library.cost == 2 + (pages - 1), var
        assert library.rate.daily_remaining is not None, var
