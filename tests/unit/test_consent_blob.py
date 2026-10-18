"""The consent form's signed blob (SPEC §3.2.3): HMAC-SHA256 under
``server.consent_hmac_key``, valid for 10 minutes, refused when altered."""

import base64
import json

import pytest

from syncshelf.auth.cimd import CLAUDE_CODE
from syncshelf.auth.oauth import CONSENT_TTL, ConsentInvalid, ConsentRequest, sign_consent
from syncshelf.auth.oauth import verify_consent as verify

KEY = "fake-consent-key"
NOW = 4_102_444_800
CONSENT = ConsentRequest(
    client_id=CLAUDE_CODE,
    redirect_uri="http://127.0.0.1:53111/callback",
    code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    resource="http://localhost:8000/mcp",
    state="some state",
    scopes=("syncshelf", "offline_access"),
    email="alice@example.com",
    refresh_allowed=True,
)


def reason(blob: str, *, key: str = KEY, now: int = NOW) -> str:
    with pytest.raises(ConsentInvalid) as excinfo:
        verify(blob, key, now)
    return excinfo.value.reason


def test_a_signed_blob_verifies_to_the_same_request() -> None:
    assert verify(sign_consent(CONSENT, KEY, NOW), KEY, NOW) == CONSENT


def test_a_request_without_state_round_trips() -> None:
    consent = ConsentRequest(**{**CONSENT.__dict__, "state": None, "scopes": ("syncshelf",)})
    assert verify(sign_consent(consent, KEY, NOW), KEY, NOW) == consent


def test_it_expires_ten_minutes_after_signing() -> None:
    blob = sign_consent(CONSENT, KEY, NOW)
    assert CONSENT_TTL == 600
    assert verify(blob, KEY, NOW + CONSENT_TTL - 1) == CONSENT
    assert reason(blob, now=NOW + CONSENT_TTL) == "expired"


def test_another_key_does_not_verify() -> None:
    assert reason(sign_consent(CONSENT, KEY, NOW), key="another-key") == "bad_signature"


def test_an_altered_payload_does_not_verify() -> None:
    body, _, signature = sign_consent(CONSENT, KEY, NOW).partition(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["email"] = "bob@example.com"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert reason(forged + "." + signature) == "bad_signature"


def test_an_extended_expiry_does_not_verify() -> None:
    later = sign_consent(CONSENT, KEY, NOW + 3600)
    early = sign_consent(CONSENT, KEY, NOW)
    spliced = later.partition(".")[0] + "." + early.partition(".")[2]
    assert reason(spliced) == "bad_signature"


@pytest.mark.parametrize("blob", ["", "no-dot", "é.x", "a.!!!", "a.b+c"])
def test_a_malformed_blob_is_refused(blob: str) -> None:
    assert reason(blob) == "malformed"
