"""agentnet.credential_catalog: the credential types agents declare and users store."""

from __future__ import annotations

import pytest

from agentnet.credential_catalog import CATALOG, KNOWN_TYPES, catalog_entry, is_known_type


def test_catalog_known_and_custom_types():
    assert is_known_type("openai_api_key")
    assert is_known_type("custom:my-thing")
    assert is_known_type("custom:a_b")
    assert not is_known_type("bogus")
    assert not is_known_type("custom:")          # slug required
    assert not is_known_type("custom:Bad Slug")  # no spaces/uppercase
    assert catalog_entry("openai_api_key").kind == "api_key"
    assert catalog_entry("custom:x") is None     # not a first-class catalog entry


def test_known_types_are_the_catalog_types():
    assert KNOWN_TYPES == {e.type for e in CATALOG}
    assert len(KNOWN_TYPES) == len(CATALOG)      # no type listed twice


@pytest.mark.parametrize(
    "value", ["custom:a\n", "openai_api_key\n", " openai_api_key", "OpenAI_API_Key", "custom:A"]
)
def test_the_check_is_exact(value):
    # Callers strip (the chat app also lowercases) before checking; the check itself
    # accepts nothing around or inside a type, a trailing newline included.
    assert not is_known_type(value)
