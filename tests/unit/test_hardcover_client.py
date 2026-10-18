"""The Hardcover client against ``FakeHardcover`` (SPEC §3.3.1)."""

import httpx2
import pytest

from syncshelf.clients.hardcover import (
    LIBRARY,
    LIBRARY_MORE,
    ME,
    USER_AGENT,
    HardcoverCapacityError,
    HardcoverClient,
    HardcoverHTTPError,
    HardcoverMutationError,
    HardcoverQueryError,
    HardcoverRateLimited,
    HardcoverUnavailable,
    RateLimit,
    check_mutation,
    query_cost,
)

from fakes.hardcover import URL, FakeHardcover

TOKEN = "fake-hardcover-token"

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(TOKEN, user_id=42)
    return fake


def client_for(fake: FakeHardcover, token: str = TOKEN) -> HardcoverClient:
    return HardcoverClient(URL, token, gate=None, transport=fake.transport)


# --- cost -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "cost"),
    [
        (ME, 1),
        (LIBRARY, 2),
        (LIBRARY_MORE, 1),
        ("{ me { id } }", 1),
        ("query Two { a: me { id } b: me { id } }", 2),
        ('query Q { books(where: {title: {_eq: "a { b } c"}}) { id } }', 1),
        ("mutation M($o: X!) { insert_user_book(object: $o) { id error } }", 1),
        ("query Q {\n  # a: comment { here }\n  me { id }\n  lists { id }\n}", 2),
        ("query Q($x: Boolean!) { me @include(if: $x) { id } }", 1),
        ("query Q { ... on query_root { me { id } } lists { id } }", 1),
        ("query Q { ...Frag me { id } }", 1),
    ],
)
def test_query_cost_counts_top_level_fields(query: str, cost: int) -> None:
    assert query_cost(query) == cost


def test_query_cost_refuses_something_that_is_not_an_operation() -> None:
    with pytest.raises(ValueError):
        query_cost("query Nothing")


# --- requests ---------------------------------------------------------------------------


async def test_every_request_carries_the_bearer_token_and_user_agent() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": {"me": [{"id": 1}]}})

    client = HardcoverClient(URL, TOKEN, gate=None, transport=httpx2.MockTransport(handler))
    await client.execute(ME)
    [request] = seen
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["user-agent"] == USER_AGENT
    assert str(request.url) == URL


async def test_me_id_is_fetched_once(fake: FakeHardcover) -> None:
    client = client_for(fake)
    assert await client.me_id() == 42
    assert await client.me_id() == 42
    assert [op for op, _ in fake.requests] == ["Me"]


async def test_a_result_reports_its_cost_and_the_rate_headers(fake: FakeHardcover) -> None:
    result = await client_for(fake).execute(ME)
    assert result.cost == 1
    assert result.rate.daily_remaining == 4999
    assert result.rate.daily_limit == 5000
    assert result.rate.remaining == 9


def test_rate_limit_headers_are_read_and_bad_values_ignored() -> None:
    headers = httpx2.Headers(
        {
            "X-Ratelimit-Remaining": "3",
            "X-Ratelimit-Reset": "1792328201",
            "X-Ratelimit-Daily-Remaining": "nope",
            "Retry-After": "5",
            "Ratelimit-Reset": "4",
        }
    )
    rate = RateLimit.from_headers(headers)
    assert (rate.remaining, rate.reset, rate.daily_remaining) == (3, 1792328201.0, None)
    assert (rate.retry_after, rate.ratelimit_reset) == (5.0, 4.0)


# --- the library ------------------------------------------------------------------------


async def test_the_library_returns_every_user_book_and_the_lists(fake: FakeHardcover) -> None:
    fake.add_user_book(42, 7, status_id=3, rating=4.5)
    fake.add_user_book(42, 8)
    fake.add_list(42, "Favourites", [7])
    library = await client_for(fake).library()
    assert [ub["book_id"] for ub in library.user_books] == [7, 8]
    assert library.user_books[0]["rating"] == 4.5
    assert [b["book_id"] for b in library.lists[0]["list_books"]] == [7]
    assert library.cost == 2


async def test_the_library_pages_until_a_short_page(fake: FakeHardcover) -> None:
    for book_id in range(1, 6):
        fake.add_user_book(42, book_id)
    library = await client_for(fake).library(page_size=2)
    assert [ub["book_id"] for ub in library.user_books] == [1, 2, 3, 4, 5]
    assert [op for op, _ in fake.requests] == ["Me", "Library", "LibraryMore", "LibraryMore"]
    assert [v["after"] for _, v in fake.requests[1:]] == [0, 2, 4]  # the last id seen
    assert library.cost == 2 + 1 + 1


async def test_the_library_gets_past_the_silent_1000_row_cap(fake: FakeHardcover) -> None:
    for book_id in range(1, 1002):
        fake.add_user_book(42, book_id)
    library = await client_for(fake).library()
    assert len(library.user_books) == 1001
    assert len({ub["id"] for ub in library.user_books}) == 1001


async def test_a_page_size_over_the_cap_is_clamped_so_no_rows_are_lost(
    fake: FakeHardcover,
) -> None:
    for book_id in range(1, 1102):
        fake.add_user_book(42, book_id)
    library = await client_for(fake).library(page_size=5000)
    assert len(library.user_books) == 1101


async def test_the_fake_caps_a_page_at_1000_rows_without_saying_so(fake: FakeHardcover) -> None:
    for book_id in range(1, 1102):
        fake.add_user_book(42, book_id)
    variables = {"user_id": 42, "limit": 5000, "after": 0, "lists_after": 0}
    result = await client_for(fake).execute(LIBRARY, variables)
    assert len(result.data["user_books"]) == 1000


async def test_queries_always_filter_by_the_callers_own_id(fake: FakeHardcover) -> None:
    await client_for(fake).library()
    assert all(v["user_id"] == 42 for op, v in fake.requests if op != "Me")


# --- failures ---------------------------------------------------------------------------


async def test_a_429_carries_retry_after_for_the_limiter(fake: FakeHardcover) -> None:
    fake.fail_next("Me", "rate_limited", retry_after=11)
    with pytest.raises(HardcoverRateLimited) as excinfo:
        await client_for(fake).execute(ME)
    assert excinfo.value.status == 429
    assert excinfo.value.rate.retry_after == 11
    assert excinfo.value.rate.ratelimit_reset == 11


async def test_request_exceeds_capacity_is_its_own_error(fake: FakeHardcover) -> None:
    fake.fail_next("Me", "capacity")
    with pytest.raises(HardcoverCapacityError):
        await client_for(fake).execute(ME)


@pytest.mark.parametrize("kind", ["unavailable", "timeout"])
async def test_transient_failures_are_unavailable(fake: FakeHardcover, kind: str) -> None:
    fake.fail_next("Me", kind)
    with pytest.raises(HardcoverUnavailable):
        await client_for(fake).execute(ME)


async def test_a_bad_token_is_an_http_error_that_never_names_the_token(
    fake: FakeHardcover,
) -> None:
    with pytest.raises(HardcoverHTTPError) as excinfo:
        await client_for(fake, token="not-a-known-token").execute(ME)
    assert excinfo.value.status == 401
    assert "not-a-known-token" not in str(excinfo.value)


async def test_a_capacity_code_in_an_error_object_is_still_a_capacity_error() -> None:
    body = {"error": {"code": "request_exceeds_capacity", "message": "Too many fields"}}
    transport = httpx2.MockTransport(lambda _r: httpx2.Response(403, json=body))
    with pytest.raises(HardcoverCapacityError):
        await HardcoverClient(URL, TOKEN, gate=None, transport=transport).execute(ME)


async def test_an_undecodable_response_is_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.DecodingError("bad gzip", request=request)

    with pytest.raises(HardcoverUnavailable):
        await HardcoverClient(
            URL, TOKEN, gate=None, transport=httpx2.MockTransport(handler)
        ).execute(ME)


@pytest.mark.parametrize("me", [[], [None], [{"id": "7"}], None])
async def test_an_unexpected_me_is_a_query_error(me: object) -> None:
    body = {"data": {"me": me}}
    transport = httpx2.MockTransport(lambda _r: httpx2.Response(200, json=body))
    with pytest.raises(HardcoverQueryError):
        await HardcoverClient(URL, TOKEN, gate=None, transport=transport).me_id()


async def test_graphql_errors_in_a_200_are_a_query_error() -> None:
    body = {"errors": [{"message": "field 'x' not found", "extensions": {"code": "validation"}}]}
    transport = httpx2.MockTransport(lambda _r: httpx2.Response(200, json=body))
    with pytest.raises(HardcoverQueryError, match="validation"):
        await HardcoverClient(URL, TOKEN, gate=None, transport=transport).execute(ME)


async def test_a_mutation_refused_with_200_raises_through_check_mutation(
    fake: FakeHardcover,
) -> None:
    mutation = "mutation Rate($id: Int!) { update_user_book(id: $id, object: {}) { id error } }"
    fake.fail_next("Rate", "validation", message="Rating must be between 0.5 and 5")
    result = await client_for(fake).execute(mutation, {"id": 1})
    with pytest.raises(HardcoverMutationError, match="Rating must be between"):
        check_mutation(result.data, "update_user_book")


def test_check_mutation_returns_a_clean_result() -> None:
    data = {"update_user_book": {"id": 5, "error": None}}
    assert check_mutation(data, "update_user_book") == {"id": 5, "error": None}


async def test_paging_by_keyset_survives_a_row_removed_between_pages(fake: FakeHardcover) -> None:
    for book_id in range(1, 6):
        fake.add_user_book(42, book_id)
    client = client_for(fake)
    first = await client.execute(LIBRARY, {"user_id": 42, "limit": 2, "after": 0, "lists_after": 0})
    del fake.user_books[42][0]  # removed in Hardcover after page 1 was read
    rest = await client.execute(
        LIBRARY_MORE, {"user_id": 42, "limit": 10, "after": first.data["user_books"][-1]["id"]}
    )
    seen = [ub["book_id"] for ub in first.data["user_books"] + rest.data["user_books"]]
    assert seen == [1, 2, 3, 4, 5]  # offset paging would have skipped book 3


async def test_lists_and_their_books_are_paged_past_the_cap(fake: FakeHardcover) -> None:
    for n in range(5):
        fake.add_list(42, f"List {n}", list(range(100, 105)))
    library = await client_for(fake).library(page_size=2)
    assert len(library.lists) == 5
    assert all([b["book_id"] for b in lst["list_books"]] == list(range(100, 105))
               for lst in library.lists)  # fmt: skip
    ops = [op for op, _ in fake.requests]
    assert ops.count("ListsMore") == 2 and ops.count("ListBooksMore") == 10
