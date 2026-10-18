"""The Cloudflare Access JWT check (SPEC §3.2.3), against a test RSA key and a mocked JWKS."""

from collections.abc import AsyncIterator

import httpx2
import pytest

from syncshelf.auth.access import AccessRefused, AccessVerifier

from access_jwt import FakeAccess, jwk, rsa_key
from clock import FakeClock
from serving import ACCESS_AUD, TEAM_DOMAIN

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake(clock: FakeClock) -> FakeAccess:
    return FakeAccess(clock)


@pytest.fixture
async def verifier(clock: FakeClock, fake: FakeAccess) -> AsyncIterator[AccessVerifier]:
    access = AccessVerifier(
        TEAM_DOMAIN, ACCESS_AUD, clock=clock.time, transport=httpx2.MockTransport(fake)
    )
    async with access.running():
        yield access


async def refused(verifier: AccessVerifier, token: str | None) -> AccessRefused:
    with pytest.raises(AccessRefused) as excinfo:
        await verifier.verify(token)
    return excinfo.value


async def test_a_valid_jwt_gives_its_email_lowercased(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    assert await verifier.verify(fake.token("Alice@Example.COM")) == "alice@example.com"
    assert [str(r.url) for r in fake.requests] == [TEAM_DOMAIN + "/cdn-cgi/access/certs"]


async def test_an_aud_array_need_only_contain_the_tag(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    token = fake.token(aud=["another-app", ACCESS_AUD])
    assert await verifier.verify(token) == "alice@example.com"


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b.c"])
async def test_a_missing_or_malformed_jwt_is_refused(
    verifier: AccessVerifier, token: str | None
) -> None:
    assert (await refused(verifier, token)).reason in ("access_jwt_missing", "access_jwt_malformed")


async def test_a_jwt_signed_by_another_key_is_refused(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    token = fake.token(key=rsa_key("someone-else"))
    assert (await refused(verifier, token)).reason == "access_jwt_bad_signature"


@pytest.mark.parametrize("algorithm", ["HS256", "RS512", "none"])
async def test_only_rs256_is_accepted(
    verifier: AccessVerifier, fake: FakeAccess, algorithm: str
) -> None:
    token = fake.token(algorithm=algorithm)
    assert (await refused(verifier, token)).reason == "access_jwt_bad_alg"
    assert fake.requests == [], "no key is fetched for a token that can't be accepted"


@pytest.mark.parametrize(
    ("claims", "drop", "reason"),
    [
        ({"aud": ["another-app"]}, (), "access_jwt_bad_aud"),
        ({"aud": "another-app"}, (), "access_jwt_bad_aud"),
        ({}, ("aud",), "access_jwt_invalid"),
        ({"iss": "https://other.cloudflareaccess.com"}, (), "access_jwt_bad_iss"),
        ({}, ("iss",), "access_jwt_invalid"),
        ({}, ("exp",), "access_jwt_invalid"),
        ({}, ("iat",), "access_jwt_invalid"),
        ({"type": "org"}, (), "access_jwt_bad_type"),
        ({}, ("type",), "access_jwt_bad_type"),
        ({"email": ""}, (), "access_jwt_no_email"),
        ({"email": 7}, (), "access_jwt_no_email"),
        ({}, ("email",), "access_jwt_no_email"),
    ],
    ids=[
        "wrong-aud",
        "wrong-aud-string",
        "no-aud",
        "wrong-iss",
        "no-iss",
        "no-exp",
        "no-iat",
        "wrong-type",
        "no-type",
        "empty-email",
        "non-string-email",
        "no-email",
    ],
)
async def test_each_claim_is_checked(
    verifier: AccessVerifier,
    fake: FakeAccess,
    claims: dict[str, object],
    drop: tuple[str, ...],
    reason: str,
) -> None:
    token = fake.token(drop=drop, **claims)
    assert (await refused(verifier, token)).reason == reason


async def test_exp_has_30_seconds_of_leeway(
    verifier: AccessVerifier, fake: FakeAccess, clock: FakeClock
) -> None:
    token = fake.token(exp=int(clock.time()))
    clock.advance(29)
    assert await verifier.verify(token) == "alice@example.com"
    clock.advance(1)
    assert (await refused(verifier, token)).reason == "access_jwt_expired"


@pytest.mark.parametrize("claim", ["iat", "nbf"])
async def test_iat_and_nbf_have_30_seconds_of_leeway(
    verifier: AccessVerifier, fake: FakeAccess, clock: FakeClock, claim: str
) -> None:
    now = int(clock.time())
    assert await verifier.verify(fake.token(**{claim: now + 30})) == "alice@example.com"
    token = fake.token(**{claim: now + 31})
    assert (await refused(verifier, token)).reason == "access_jwt_not_yet_valid"


async def test_the_key_set_is_cached(verifier: AccessVerifier, fake: FakeAccess) -> None:
    await verifier.verify(fake.token())
    await verifier.verify(fake.token("bob@example.com"))
    assert len(fake.requests) == 1


async def test_an_unknown_kid_fetches_the_key_set_again(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    await verifier.verify(fake.token())
    fake.published["key-2"] = rsa_key("access-2")  # Access rotates its key
    token = fake.token(key=fake.published["key-2"], kid="key-2")
    assert await verifier.verify(token) == "alice@example.com"
    assert len(fake.requests) == 2


async def test_a_kid_the_fresh_set_still_lacks_is_refused(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    token = fake.token(key=rsa_key("access-2"), kid="key-2")
    assert (await refused(verifier, token)).reason == "access_jwt_unknown_kid"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(503),
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json=["not", "an", "object"]),
        httpx2.Response(200, json={"keys": []}),
    ],
    ids=["503", "not-json", "not-an-object", "no-keys"],
)
async def test_a_failed_key_fetch_is_unavailable_not_a_bad_jwt(
    verifier: AccessVerifier, fake: FakeAccess, response: httpx2.Response
) -> None:
    fake.respond = lambda request: response
    error = await refused(verifier, fake.token())
    assert (error.reason, error.unavailable) == ("jwks_unavailable", True)


async def test_a_failed_refetch_keeps_the_cached_keys(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    await verifier.verify(fake.token())
    fake.respond = lambda request: httpx2.Response(503)
    assert (await refused(verifier, fake.token(kid="key-2"))).unavailable
    assert await verifier.verify(fake.token()) == "alice@example.com"


async def test_it_refuses_when_not_running(clock: FakeClock, fake: FakeAccess) -> None:
    access = AccessVerifier(TEAM_DOMAIN, ACCESS_AUD, clock=clock.time)
    error = await refused(access, fake.token())
    assert (error.reason, error.unavailable) == ("not_running", True)


async def test_a_set_with_no_rs256_key_keeps_the_cached_keys(
    verifier: AccessVerifier, fake: FakeAccess
) -> None:
    await verifier.verify(fake.token())
    other = {**jwk(rsa_key("access-2"), "key-2"), "alg": "RS512"}
    fake.respond = lambda request: httpx2.Response(200, json={"keys": [other]})
    error = await refused(verifier, fake.token(kid="key-2"))
    assert (error.reason, error.unavailable) == ("jwks_unavailable", True)
    assert await verifier.verify(fake.token()) == "alice@example.com"
