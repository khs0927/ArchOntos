import pytest

from archontos.identity import (
    UNNAMED_KEY_ACTOR,
    InvalidActor,
    Principal,
    current_actor,
    match_key,
    parse_api_keys,
    reset_principal,
    set_principal,
)


def test_parse_api_keys_named_and_unnamed():
    assert parse_api_keys(" alice:k1 , k2 ,,") == {"k1": "alice", "k2": UNNAMED_KEY_ACTOR}


def test_parse_api_keys_rejects_bad_actor():
    with pytest.raises(InvalidActor):
        parse_api_keys("bad actor:k1")


def test_match_key():
    keys = {"k1": "alice", "k2": "api-key"}
    assert match_key("k1", keys) == "alice"
    assert match_key("k3", keys) is None
    assert match_key("", keys) is None


def test_principal_context_is_scoped():
    assert current_actor() == "system"
    token = set_principal(Principal("alice", True))
    try:
        assert current_actor() == "alice"
    finally:
        reset_principal(token)
    assert current_actor() == "system"
