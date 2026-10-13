"""Secret containment for the committed `agent.json` (registration_service/build_manifest.py).

The manifest is committed to the shared `agent-directory` repo, while several config
fields are *designed* to hold secrets (the uploaded-tools `extra` dict is passed to the
author's `build_tools(config)` factory — "the closure-safe way to bake in secrets").
These tests are the regression fence around that: they assert the allowlist emits what
it should and that nothing else in the config can reach the rendered document.

Run: FAKE_DB=1 PYTHONPATH=. pytest registration_service/tests/test_build_manifest.py -q
"""

from __future__ import annotations

import json

import pytest

from registration_service.build_manifest import (
    MANIFEST_FILENAME,
    SCHEMA,
    build_agent_manifest,
)
from registration_service.models import LLMConfig, UploadedTools

CANARY = "CANARY_SECRET_zzzzzzzzzz"


def _render(config, *, handler_type="custom", skills=None) -> str:
    return build_agent_manifest(
        name="Test Agent",
        description="A test agent.",
        slug="test-agent",
        handler_type=handler_type,
        config=config,
        oasf_skills=skills if skills is not None else [{"key": "nlp/x/y", "id": 10306}],
    )


# ── The happy path: the authoring fields the build context otherwise loses ────────
def test_emits_the_authoring_fields():
    doc = json.loads(_render({
        "class_name": "MyHandler",
        "entry_file": "handler.py",
        "python_version": "3.12",
        "install_project": False,
        "max_runtime_seconds": 600,
        "requirements": ["httpx==0.27.2"],
        "system_packages": ["curl"],
    }))
    assert doc["schema"] == SCHEMA
    assert doc["class_name"] == "MyHandler"
    assert doc["entry_file"] == "handler.py"
    assert doc["python_version"] == "3.12"
    assert doc["install_project"] is False
    assert doc["max_runtime_seconds"] == 600
    assert doc["requirements"] == ["httpx==0.27.2"]
    assert doc["system_packages"] == ["curl"]
    assert doc["handler_type"] == "custom"
    assert doc["slug"] == "test-agent"


def test_is_deterministic():
    """Re-registering an unchanged agent must not produce a spurious commit diff."""
    config = {"class_name": "H", "entry_file": "handler.py", "requirements": ["a==1"]}
    assert _render(config) == _render(config)


# ── Containment ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("field", [
    "extra",            # the documented "bake in secrets" channel
    "tools",            # arbitrary dicts
    "auth_header",      # may be a literal header value
    "system_prompt",    # free text
    "model",            # free text
    "upstream_url",     # may carry credentials in userinfo
    "base_url",
])
def test_never_emit_scalar_fields_stay_out(field):
    rendered = _render({"class_name": "H", "entry_file": "handler.py", field: CANARY})
    assert CANARY not in rendered
    assert field in json.loads(rendered)["omitted_fields"]


def test_secrets_nested_arbitrarily_deep_in_extra_stay_out():
    rendered = _render({
        "class_name": "H",
        "entry_file": "handler.py",
        "extra": {"a": {"b": [{"c": CANARY}]}, "d": [[CANARY + "-2"]]},
    })
    assert CANARY not in rendered


def test_unknown_future_field_is_not_emitted():
    """A field added to a config model later must stay out until opted in.

    The whole point of the allowlist: the failure mode of a new field is a MISSING
    field, never a leak.
    """
    rendered = _render({
        "class_name": "H",
        "entry_file": "handler.py",
        "some_field_invented_next_year": CANARY,
    })
    assert CANARY not in rendered


def test_credential_label_is_dropped_but_names_are_kept():
    """Declared credentials are reviewable as type + env-var NAME + required.

    `label` is author-written free text, so it is not committed.
    """
    doc = json.loads(_render({
        "class_name": "H",
        "entry_file": "handler.py",
        "credentials": [{
            "type": "openai_api_key",
            "label": CANARY,
            "required": True,
            "env_var": "OPENAI_API_KEY",
        }],
    }))
    assert doc["credentials"] == [
        {"type": "openai_api_key", "required": True, "env_var": "OPENAI_API_KEY"}
    ]
    assert CANARY not in json.dumps(doc)


@pytest.mark.parametrize("field,value,expected", [
    ("env_var", "OPENAI_KEY=" + CANARY, "<unrecognised>"),
    ("env_var", "sk-live-" + "8" * 20, "<unrecognised>"),
    ("type", "custom:" + CANARY.lower(), "custom:<redacted>"),   # open-ended escape
    ("type", "Authorization: Bearer " + CANARY, "<unrecognised>"),
    ("type", "not_in_the_catalog_" + CANARY.lower(), "<unrecognised>"),
])
def test_credential_strings_are_shape_checked_not_trusted(field, value, expected):
    """`type` and `env_var` are unvalidated `str` at the model layer, and committed.

    A declaration that does not look like a name must be reported as unrecognised
    rather than published verbatim.
    """
    doc = json.loads(_render({
        "class_name": "H",
        "entry_file": "handler.py",
        "credentials": [{"type": "openai_api_key", "required": True,
                         "env_var": "OPENAI_API_KEY", **{field: value}}],
    }))
    dumped = json.dumps(doc)
    assert CANARY not in dumped and CANARY.lower() not in dumped
    assert "8" * 20 not in dumped
    assert doc["credentials"][0][field] == expected


def test_wellformed_credentials_survive_the_shape_check():
    doc = json.loads(_render({
        "class_name": "H",
        "entry_file": "handler.py",
        "credentials": [
            {"type": "openai_api_key", "required": True, "env_var": "OPENAI_API_KEY"},
            {"type": "custom:my-service", "required": False, "env_var": "MY_TOKEN"},
        ],
    }))
    assert doc["credentials"] == [
        # A catalog key is a fixed enumeration value, so it is safe verbatim.
        {"type": "openai_api_key", "required": True, "env_var": "OPENAI_API_KEY"},
        # A custom slug is registrant free text: present, but not published.
        {"type": "custom:<redacted>", "required": False, "env_var": "MY_TOKEN"},
    ]


def test_widening_the_allowlist_to_a_secret_field_is_rejected(monkeypatch):
    """The one mistake the runtime self-test cannot catch, caught statically.

    Anything the allowlist emits is by definition part of the manifest, so the
    walk-the-config guard would consider it legitimate. assert_allowlist_sane() is
    what stops a future edit from adding `extra` (or any other never-emit field).
    """
    import registration_service.build_manifest as bm

    monkeypatch.setattr(bm, "_ALLOWED", bm._ALLOWED + (("extra", "extra", "path"),))
    with pytest.raises(RuntimeError, match="secret-bearing field"):
        bm.assert_allowlist_sane()


def test_allowlist_is_sane_as_shipped():
    from registration_service.build_manifest import assert_allowlist_sane

    assert_allowlist_sane()   # raises if someone widened it


def test_selftest_catches_a_value_reaching_the_doc_outside_the_allowlist(monkeypatch):
    """A config value arriving by any non-allowlist path must abort the commit."""
    import registration_service.build_manifest as bm

    real = bm._coerce

    def leaky(kind, value):
        # Simulate a bug that interpolates `extra` into an allowlisted slot.
        if kind == "path":
            return CANARY
        return real(kind, value)

    monkeypatch.setattr(bm, "_coerce", leaky)
    with pytest.raises(RuntimeError, match="refusing to commit"):
        bm.build_agent_manifest(
            name="Test Agent", description="d", slug="s", handler_type="custom",
            config={"class_name": "H", "entry_file": "handler.py", "extra": {"k": CANARY}},
            oasf_skills=[],
        )


def test_freetext_cannot_launder_a_secret_past_the_selftest():
    """The substring exemption must not extend to registrant-controlled prose.

    name/description are emitted (the OASF card has always carried them), so an exact
    match against them is exempt. But allowing *substring* matches would let someone
    paste a secret into the description and thereby silence the check for the same
    value arriving from `extra`.
    """
    secret = "sk-live-LAUNDER-" + "9" * 12
    with pytest.raises(RuntimeError, match="refusing to commit"):
        build_agent_manifest(
            name="A",
            description=f"prose {secret} prose",
            slug="a",
            handler_type="custom",
            config={"class_name": "H", "entry_file": "handler.py", "extra": {"k": secret}},
            oasf_skills=[],
        )


def test_a_requirement_fragment_in_extra_does_not_false_positive():
    """The substring exemption still holds for allowlisted sources.

    `extra` echoing part of a real requirement is a coincidence, not a leak, and must
    not block a legitimate registration.
    """
    rendered = _render({
        "class_name": "H",
        "entry_file": "handler.py",
        "requirements": ["httpx==0.27.2"],
        "extra": {"pkg": "httpx==0.27"},
    })
    assert '"httpx==0.27.2"' in rendered


@pytest.mark.parametrize("secret", [
    'sk"live"AAAAAAAAAA',      # a quote is escaped in the encoded document
    "sk\\live\\BBBBBBBBBB",      # so is a backslash
    "sk\nlive\nCCCCCCCCCC",      # and a newline
    "sk\u00fc-live-DDDDDDDDDD",   # non-ASCII becomes a \u escape
])
def test_selftest_is_not_fooled_by_json_escaping(monkeypatch, secret):
    """The guard must compare structurally, not against the encoded bytes.

    A raw substring test against json.dumps() output would miss any secret whose
    characters get escaped — which is most of the interesting ones.
    """
    import registration_service.build_manifest as bm

    real = bm._coerce
    monkeypatch.setattr(
        bm, "_coerce",
        lambda kind, value: secret if kind == "path" else real(kind, value),
    )
    with pytest.raises(RuntimeError, match="refusing to commit"):
        bm.build_agent_manifest(
            name="A", description="d", slug="a", handler_type="custom",
            config={"class_name": "H", "entry_file": "handler.py", "extra": {"k": secret}},
            oasf_skills=[],
        )


def test_error_text_never_echoes_the_offending_value():
    """The exception reaches logs and a 500 response body, so it must stay opaque."""
    import registration_service.build_manifest as bm

    secret = "sk-live-MUSTNOTAPPEAR-1234567890"
    real = bm._coerce
    bm._coerce = lambda kind, value: secret if kind == "path" else real(kind, value)
    try:
        bm.build_agent_manifest(
            name="A", description="d", slug="a", handler_type="custom",
            config={"class_name": "H", "entry_file": "handler.py", "extra": {"k": secret}},
            oasf_skills=[],
        )
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as exc:
        assert secret not in str(exc)
    finally:
        bm._coerce = real


@pytest.mark.parametrize("field,value", [
    ("class_name", "H; echo " + CANARY),           # not identifier-shaped
    ("entry_file", "../../../../etc/passwd"),      # traversal
    ("entry_file", "/absolute/handler.py"),        # absolute
    ("entry_file", "handler.py\n# " + CANARY),     # control character
    ("python_version", '3.11" && curl ' + CANARY),  # quote/space
])
def test_committed_scalars_are_format_validated(field, value):
    """These land in a committed file, so they are validated here, not trusted."""
    base = {"class_name": "H", "entry_file": "handler.py", "python_version": "3.11"}
    base[field] = value
    rendered = _render(base)
    assert CANARY not in rendered
    assert "etc/passwd" not in rendered
    assert field not in json.loads(rendered)


def test_requirement_entries_with_control_characters_are_dropped():
    doc = json.loads(_render({
        "class_name": "H", "entry_file": "handler.py",
        "requirements": ["httpx==0.27.2", 'evil" \n RUN echo ' + CANARY],
    }))
    assert doc["requirements"] == ["httpx==0.27.2"]
    assert CANARY not in json.dumps(doc)


def test_user_controlled_field_names_are_not_echoed():
    """omitted_fields lists NAMES; a dict config's keys are user-controlled."""
    rendered = _render({
        "class_name": "H",
        "entry_file": "handler.py",
        "sk-live-" + "9" * 24: "irrelevant",
        "Authorization: Bearer " + CANARY: "irrelevant",
    })
    assert CANARY not in rendered
    assert "sk-live-" not in rendered


def test_coercion_rejects_wrong_types_instead_of_stringifying():
    """A dict where a string is expected must be dropped, not str()-ed into the doc."""
    rendered = _render({
        "class_name": {"nested": CANARY},          # not a str -> omitted
        "entry_file": "handler.py",
        "requirements": [{"pkg": CANARY}, "ok==1"],  # non-str items filtered
        "max_runtime_seconds": {"n": CANARY},
    })
    assert CANARY not in rendered
    doc = json.loads(rendered)
    assert "class_name" not in doc
    assert doc["requirements"] == ["ok==1"]
    assert "max_runtime_seconds" not in doc


# ── The real config models, both taxonomy shapes ─────────────────────────────────
def test_real_custom_config_with_secret_in_extra():
    from registration_service.models import CustomConfig

    config = CustomConfig(
        class_name="MyHandler",
        entry_file="handler.py",
        requirements=["httpx==0.27.2"],
        extra={"api_key": CANARY},
    )
    rendered = _render(config)
    assert CANARY not in rendered
    doc = json.loads(rendered)
    assert doc["class_name"] == "MyHandler"
    assert "extra" in doc["omitted_fields"]


def test_uploaded_tools_shape():
    """Under the folded taxonomy the build fields live in a nested tools union member."""
    config = LLMConfig(
        system_prompt=f"prompt containing {CANARY}",
        tools=UploadedTools(entry_file="tools.py", python_version="3.12",
                            requirements=["httpx==0.27.2"], extra={"k": CANARY}),
    )
    rendered = _render(config, handler_type="llm")
    assert CANARY not in rendered
    doc = json.loads(rendered)
    # The nested build fields are still recovered, via models.upload_spec.
    assert doc["entry_file"] == "tools.py"
    assert doc["python_version"] == "3.12"
    assert doc["requirements"] == ["httpx==0.27.2"]
    assert "system_prompt" in doc["omitted_fields"]


def test_filename_is_stable():
    """The committed path is part of the contract other tooling keys on."""
    assert MANIFEST_FILENAME == "agent.json"
