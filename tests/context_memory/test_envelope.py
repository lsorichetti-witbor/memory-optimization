from datetime import datetime, timezone

import pytest

from src.memory.envelope import Envelope, decode_envelope, encode_envelope
from src.memory.scopes import Scope


def _envelope(**overrides) -> Envelope:
    base = dict(
        scope=Scope.REPOSITORY,
        scope_key="memory-optimization",
        kind="discovery",
        lifecycle="durable",
        confidence=0.8,
        importance=0.6,
        topic="server.default_provider",
        source="session:2026-09-13",
        created_at=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Envelope(**base)


def test_encode_produces_flat_json_safe_metadata():
    encoded = encode_envelope(_envelope())
    assert encoded["scope"] == "repository"
    assert encoded["lifecycle"] == "durable"
    assert encoded["confidence"] == 0.8
    assert encoded["created_at"] == "2026-09-13T12:00:00+00:00"
    assert all(isinstance(v, (str, int, float, bool)) for v in encoded.values())


def test_round_trip_is_lossless():
    original = _envelope(tags=("provider", "gemini"))
    assert decode_envelope(encode_envelope(original)) == original


def test_tags_survive_as_one_key_per_member_not_a_delimited_blob():
    # A delimited multi-value would make "tags contains gemini" match only rows
    # whose tag list was exactly "provider|gemini".
    encoded = encode_envelope(_envelope(tags=("provider", "gemini")))
    assert encoded["tag_provider"] is True
    assert encoded["tag_gemini"] is True
    assert "tags" not in encoded


def test_decode_tolerates_foreign_metadata():
    decoded = decode_envelope({"scope": "global", "scope_key": "eng", "written_by": "someone else"})
    assert decoded.scope is Scope.GLOBAL
    assert decoded.extra == {"written_by": "someone else"}


def test_decode_rejects_an_unknown_lifecycle():
    with pytest.raises(ValueError, match="unknown lifecycle"):
        decode_envelope({"scope": "global", "scope_key": "eng", "lifecycle": "archived"})


def test_confidence_and_importance_are_clamped_to_unit_interval():
    with pytest.raises(ValueError, match="confidence"):
        _envelope(confidence=1.4)
    with pytest.raises(ValueError, match="importance"):
        _envelope(importance=-0.1)


def test_missing_metadata_decodes_to_a_usable_default_envelope():
    decoded = decode_envelope({})
    assert decoded.scope is Scope.GLOBAL
    assert decoded.lifecycle == "candidate"
