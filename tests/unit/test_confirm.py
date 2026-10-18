"""Confirm tokens (SPEC §3.3.9): bound to one action over one snapshot, 10 minutes, spent on
use."""

from syncshelf.tools.confirm import TTL, ConfirmTokens, Redeemed, snapshot_of

from clock import FakeClock


def test_a_token_confirms_its_own_action_once(clock: FakeClock) -> None:
    tokens = ConfirmTokens(clock.time)
    token = tokens.issue("alice", "remove_book", 7, "snap")
    assert tokens.redeem(token, "alice", "remove_book", 7, "snap") is Redeemed.OK
    assert tokens.redeem(token, "alice", "remove_book", 7, "snap") is Redeemed.UNKNOWN


def test_a_token_expires_after_ten_minutes(clock: FakeClock) -> None:
    tokens = ConfirmTokens(clock.time)
    early = tokens.issue("alice", "remove_book", 7, "snap")
    late = tokens.issue("alice", "remove_book", 7, "snap")
    clock.advance(TTL - 1)
    assert tokens.redeem(early, "alice", "remove_book", 7, "snap") is Redeemed.OK
    clock.advance(1)
    assert TTL == 600
    assert tokens.redeem(late, "alice", "remove_book", 7, "snap") is Redeemed.UNKNOWN


def test_a_changed_snapshot_spends_the_token(clock: FakeClock) -> None:
    tokens = ConfirmTokens(clock.time)
    token = tokens.issue("alice", "remove_book", 7, "snap")
    assert tokens.redeem(token, "alice", "remove_book", 7, "other") is Redeemed.CHANGED
    assert tokens.redeem(token, "alice", "remove_book", 7, "snap") is Redeemed.UNKNOWN


def test_a_token_is_bound_to_its_profile_tool_and_subject(clock: FakeClock) -> None:
    tokens = ConfirmTokens(clock.time)
    token = tokens.issue("alice", "correct_read", (7, 1), "snap")
    for profile, tool, subject in [
        ("bob", "correct_read", (7, 1)),
        ("alice", "remove_book", (7, 1)),
        ("alice", "correct_read", (7, 2)),
        ("alice", "correct_read", 7),
    ]:
        assert tokens.redeem(token, profile, tool, subject, "snap") is Redeemed.UNKNOWN
    # A mix-up doesn't spend it: it still confirms what it was issued for.
    assert tokens.redeem(token, "alice", "correct_read", (7, 1), "snap") is Redeemed.OK


def test_an_unknown_token_confirms_nothing(clock: FakeClock) -> None:
    assert ConfirmTokens(clock.time).redeem("nope", "alice", "remove_book", 7, "s") is (
        Redeemed.UNKNOWN
    )


def test_a_snapshot_ignores_key_order() -> None:
    assert snapshot_of({"a": 1, "b": [2]}) == snapshot_of({"b": [2], "a": 1})
    assert snapshot_of({"a": 1}) != snapshot_of({"a": 2})
