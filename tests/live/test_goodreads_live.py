"""The Goodreads shelf RSS client against the real feed, read-only (SPEC §3.4a.1, §2.9).

Each ``GOODREADS_USER_ID_*`` in the env file is read anonymously, as a public profile's feed
is. One whole paced pass per profile, so this is about one request per 200 books, 2 s apart.
"""

import pytest

from syncshelf.clients.goodreads_rss import GoodreadsRss, Pacer

pytestmark = [pytest.mark.live, pytest.mark.anyio]


def user_ids(env: dict[str, str]) -> dict[str, str]:
    return {var: v for var, v in env.items() if var.startswith("GOODREADS_USER_ID_") and v}


async def test_each_profile_feed_is_usable_and_complete(live_env: dict[str, str]) -> None:
    ids = user_ids(live_env)
    assert ids, "no GOODREADS_USER_ID_<PROFILE> in the env file"
    pacer = Pacer()  # one pacer for all: the profiles share this machine's IP
    for var, user_id in ids.items():
        rss = GoodreadsRss(user_id, pacer=pacer)
        try:
            result = await rss.fetch_all()
        finally:
            await rss.aclose()
        # A usable page names the #ALL# shelf: the check is fixed to the live title form.
        assert result.complete, f"{var}: {result.problem}"
        assert result.items, f"{var}: an empty library"
        assert len({i.book_id for i in result.items}) == len(result.items), var
