"""Renders `agent.json` — the authoring half of a committed agent, minus secrets.

Why this exists
---------------
`Supervisor._commit_to_github` commits a *build context*: a Dockerfile, the OASF
card, the uploaded `src/` tree, and a vendored `agent_skeleton/`. Between them they
carry everything a Docker build needs — and nothing else. In particular the
committed tree does NOT say which class is the handler (`class_name`), what the
per-request cap is (`max_runtime_seconds`), or which user-owned credentials the
agent consumes. Those live only in the Postgres `config` JSONB and are injected at
spawn time as `AGENT_CONFIG_JSON`.

Three costs follow: the tree is not self-describing (reconstructing an agent from
the repo needs an AST pass to find the handler class), a built image cannot be run
without hand-building that env var, and the credential declarations — the most
security-relevant field an agent has — never appear in a pull request.

This module closes that gap with one more committed file while keeping secrets out.

Why an allowlist, not a redaction pass
--------------------------------------
Some config fields are *designed* to hold secrets. `ToolLoopConfig.extra` is passed
to the author's `build_tools(config)` factory, which that class's own docstring
describes as "the closure-safe way to bake in secrets without exposing them as tool
parameters". `LLMConfig.tools` holds arbitrary dicts. `APIProxyConfig.auth_header`
may be a literal header value. Free-text fields (`system_prompt`, `model`) can hold
anything an author pastes in.

So this renders a fixed, enumerated set of fields and ignores everything else. A
field added to a config model in future does **not** appear here until someone adds
it to `_ALLOWED` on purpose — the failure mode is a missing field, never a leak.

The emitted values are additionally constrained to two kinds:
  * values already present verbatim elsewhere in the committed tree (name and
    description are in the card; requirements, system_packages, python_version,
    entry_file and install_project are in the Dockerfile), or
  * scalars and identifiers validated HERE, not trusted from elsewhere: `class_name`
    and `python_version` must be identifier-shaped, `entry_file` a relative path with
    no traversal or control characters, requirement/package entries free of control
    characters and quotes, `max_runtime_seconds` an int, and credential entries a
    catalog type + an env-var NAME + a bool — never a value.

Two guards back that up. `assert_allowlist_sane()` runs at import and refuses an
allowlist that names a known secret-bearing field. `_selftest()` runs on every render
and refuses to return a document containing any config string that did not come from
one of the fields the allowlist deliberately read — so a bad coercion or a stray
interpolation aborts the commit instead of publishing.
"""

from __future__ import annotations

import json
from typing import Any

MANIFEST_FILENAME = "agent.json"
SCHEMA = "agenticnetwork/agent-manifest@1"

# ── The allowlist ────────────────────────────────────────────────────────────────
# (config attribute, json key, coercion). Anything absent from this tuple is not
# emitted, whatever a config model grows in future.
_ALLOWED: tuple[tuple[str, str, str], ...] = (
    ("class_name", "class_name", "identifier"),
    ("entry_file", "entry_file", "path"),
    ("python_version", "python_version", "identifier"),
    ("install_project", "install_project", "bool"),
    ("max_runtime_seconds", "max_runtime_seconds", "int"),
    ("requirements", "requirements", "str_list"),
    ("system_packages", "system_packages", "str_list"),
)

# Fields that may legitimately contain secrets, or free text that could. Never
# emitted; only their NAMES can appear, under "omitted_fields". Kept explicit so the
# self-test below has something concrete to check the rendered document against.
_NEVER_EMIT: frozenset[str] = frozenset({
    "extra",            # tool_loop: the documented "bake in secrets" channel
    "tools",            # llm: arbitrary dicts
    "auth_header",      # api_proxy: may be a literal header value
    "auth_env",         # endpoint_ops: an env var name, but not needed here
    "system_prompt",    # free text
    "model",            # free text
    "operations",       # endpoint_ops: nested dicts (params, headers)
    "upstream_url",     # may embed credentials in userinfo
    "base_url",         # ditto
    "preset_tools",     # not needed to rebuild an uploaded-code agent
})

_MIN_LEAK_LEN = 8   # ignore very short strings when leak-testing (see _selftest)

# Field names are echoed (names only, never values) under "omitted_fields". In
# production the names come from a pydantic model, but a dict config would supply
# user-controlled keys, so only conservative identifiers are echoed.
_SAFE_NAME_MAX = 64


def _is_safe_field_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= _SAFE_NAME_MAX
        and (name[0].isalpha() or name[0] == "_")
        and all(c.isalnum() or c == "_" for c in name)
    )


def assert_allowlist_sane() -> None:
    """The allowlist must never name a field that may hold a secret.

    A static guard, checked at import and by the tests. It exists because
    :func:`_selftest` structurally *cannot* catch this case: anything the allowlist
    emits becomes part of the manifest, so it counts as intended content. Widening
    the allowlist is therefore the one mistake that needs stopping up front.
    """
    overlap = sorted({field for field, _, _ in _ALLOWED} & _NEVER_EMIT)
    if overlap:
        raise RuntimeError(
            f"build_manifest._ALLOWED names secret-bearing field(s) {overlap}. These "
            f"fields may hold credentials or free text and must never be committed."
        )


def _get(config: Any, field: str) -> Any:
    if isinstance(config, dict):
        return config.get(field)
    return getattr(config, field, None)


def _build_fields(config: Any) -> dict[str, Any]:
    """The uploaded-code build parameters, wherever the config keeps them.

    Prefers ``models.upload_spec(config)`` — the service's own canonical accessor,
    which knows whether the fields sit on the config root (``CustomConfig``) or
    inside a nested tools union member (``LLMConfig`` + ``UploadedTools``), and so
    tracks taxonomy changes for free. Falls back to reading the root directly for
    config shapes that predate that helper. ``extra`` is deliberately not taken from
    the spec even though the spec carries it.
    """
    try:
        from registration_service.models import upload_spec  # local import: optional
    except Exception:
        upload_spec = None  # type: ignore[assignment]

    if upload_spec is not None:
        try:
            spec = upload_spec(config)
        except Exception:
            spec = None
        if spec is not None:
            return {
                "entry_file": getattr(spec, "entry_file", None),
                "python_version": getattr(spec, "python_version", None),
                "requirements": getattr(spec, "requirements", None),
                "system_packages": getattr(spec, "system_packages", None),
                "install_project": getattr(spec, "install_project", None),
            }
    return {
        "entry_file": _get(config, "entry_file"),
        "python_version": _get(config, "python_version"),
        "requirements": _get(config, "requirements"),
        "system_packages": _get(config, "system_packages"),
        "install_project": _get(config, "install_project"),
    }


def _config_field_names(config: Any) -> list[str]:
    if isinstance(config, dict):
        return sorted(config)
    fields = getattr(type(config), "model_fields", None)
    if fields:
        return sorted(fields)
    return sorted(k for k in vars(config) if not k.startswith("_"))


def _is_identifier(value: str) -> bool:
    """A Python-identifier-ish or dotted-version token: no whitespace, no punctuation.

    Covers ``class_name`` and ``python_version``. Deliberately strict — these are
    unvalidated strings at the model layer, and this file commits them.
    """
    return (
        0 < len(value) <= 128
        and (value[0].isalnum() or value[0] == "_")
        and all(c.isalnum() or c in "_." for c in value)
    )


def _is_relative_path(value: str) -> bool:
    """A relative path with no traversal, no absolute root and no control characters.

    ``entry_file`` is unvalidated at the model layer; committing it verbatim would
    publish whatever text a registrant supplied (and a newline would corrupt the
    file it lands in).
    """
    if not (0 < len(value) <= 256) or value.startswith(("/", "\\")):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return False
    parts = value.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _coerce(kind: str, value: Any) -> Any:
    """Coerce to the declared kind or return None. None means "do not emit"."""
    if value is None:
        return None
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if kind == "identifier":
        return value if isinstance(value, str) and _is_identifier(value) else None
    if kind == "path":
        return value if isinstance(value, str) and _is_relative_path(value) else None
    if kind == "str_list":
        if not isinstance(value, (list, tuple)):
            return None
        items = [
            v for v in value
            if isinstance(v, str) and v and len(v) <= 256
            and not any(ord(c) < 0x20 or ord(c) == 0x7F or c == '"' for c in v)
        ]
        return items or None
    return None


def _is_env_var_name(value: Any) -> bool:
    """A POSIX-ish environment variable NAME, not arbitrary text.

    ``CredentialRequirement.env_var`` is typed ``str | None`` with no validator, so
    "it only holds a name" is an assumption, not a guarantee — and this value is
    committed. Shape-check it rather than trust it.
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and (value[0].isalpha() or value[0] == "_")
        and all(c.isalnum() or c == "_" for c in value)
    )


def _credential_type_label(value: Any) -> str:
    """A committable label for a declared credential type.

    ``type`` is an unvalidated ``str`` at this layer, so the only values safe to
    publish verbatim are the ones drawn from a fixed enumeration: the catalog in
    ``credential_catalog``. The documented ``custom:<slug>`` escape is open-ended —
    a charset check cannot tell a slug from an API key, since most keys are
    alphanumeric — so a custom type is recorded as present but redacted. A reviewer
    still sees that the agent asks for a custom credential; which one is in the
    database and the UI.
    """
    if not isinstance(value, str) or not value:
        return "<unrecognised>"
    try:
        from registration_service.credential_catalog import catalog_entry
    except Exception:
        return "<unrecognised>"
    if catalog_entry(value) is not None:
        return value
    if value.startswith("custom:"):
        return "custom:<redacted>"
    return "<unrecognised>"


def _credentials(config: Any) -> list[dict[str, Any]]:
    """Declared credential requirements as type + env-var NAME + required.

    Drops ``label`` (author-written free text). The two remaining strings are
    unvalidated at the model layer, so neither is trusted: ``type`` is published only
    when it is a catalog entry (a fixed enumeration), and ``env_var`` only when it is
    shaped like an environment variable name. Anything else is recorded as present but
    redacted, so a reviewer still sees that the agent asks for *something*.
    """
    out: list[dict[str, Any]] = []
    for cred in _get(config, "credentials") or []:
        ctype = _get(cred, "type")
        entry: dict[str, Any] = {"required": bool(_get(cred, "required"))}
        entry["type"] = _credential_type_label(ctype)
        env_var = _get(cred, "env_var")
        if _is_env_var_name(env_var):
            entry["env_var"] = env_var
        elif env_var is not None:
            entry["env_var"] = "<unrecognised>"
        out.append(entry)
    return out


def _skills(oasf_skills: Any) -> list[dict[str, Any]]:
    """Routing key + numeric id per skill — both already in the committed card."""
    out: list[dict[str, Any]] = []
    for skill in oasf_skills or []:
        key = _get(skill, "key") or _get(skill, "name")
        entry: dict[str, Any] = {}
        if isinstance(key, str) and key:
            entry["key"] = key
        sid = _get(skill, "id")
        if isinstance(sid, int) and not isinstance(sid, bool):
            entry["id"] = sid
        if entry:
            out.append(entry)
    return out


def _walk_strings(value: Any):
    """Every string anywhere inside an arbitrarily nested structure — keys included.

    Dict KEYS are yielded as well as values: for a dict-shaped config the keys are
    registrant-controlled too, so "every string in the config" has to mean both.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _walk_strings(v)
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        # A pydantic model (or similar). model_dump() gives the whole tree including
        # nested union members, which plain vars() can miss.
        dumped = None
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump()
            except Exception:
                dumped = None
        if dumped is None:
            dumped = {k: v for k, v in (vars(value) or {}).items() if not k.startswith("_")}
        yield from _walk_strings(dumped)


def _walk_keys(value: Any):
    """Every dict KEY anywhere in a structure (manifest keys are structural text)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _walk_keys(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _walk_keys(v)


def _selftest(
    rendered: str,
    manifest: dict[str, Any],
    sources: list[Any],
    freetext: list[Any],
    config: Any,
) -> None:
    """Refuse to return a manifest containing any config string we did not choose.

    Structure-agnostic on purpose. Rather than checking the named never-emit fields —
    which would silently narrow if a field moved (``extra`` in fact moved from the
    config root into the nested tools union), this walks EVERY string anywhere in the
    config and asserts that none of them reached the output unless it is part of the
    allowlisted content. So the guarantee does not depend on knowing the config's
    shape, only on the manifest we just built.

    Legitimacy is decided by PROVENANCE, not by the finished document: ``sources``
    holds the raw values the allowlist actually read (plus the name, description,
    slug, handler_type and the derived credential/skill entries). A string that
    reaches the output without coming from one of those is a leak — which is why
    this catches a value landing in an allowlisted *slot* it did not come from
    (a bad coercion, an interpolation), not merely one appearing in a new field.

    What it cannot catch: someone adding a secret-bearing field TO the allowlist,
    since that field's raw value then legitimately becomes a source.
    :func:`assert_allowlist_sane` is the guard for that case; the tests cover both.

    Two exemptions keep a coincidence from blocking a legitimate commit: strings
    shorter than ``_MIN_LEAK_LEN`` (a real credential is longer, and short tokens
    like "3.11" collide by chance), and strings that are a *substring* of a value
    read from an allowlisted config field (e.g. ``extra={"pkg": "httpx==0.27"}``
    beside ``requirements=["httpx==0.27.2"]``).

    The substring exemption deliberately does NOT extend to ``freetext`` — the agent
    name and description. Those are registrant-controlled and emitted, so allowing
    substring matches against them would let someone launder an ``extra`` value past
    this check by also pasting it into the description. Exact matches against
    freetext stay exempt, since such a string is legitimately in the document
    already (the OASF card has carried name and description all along).
    """
    # Structural text (the manifest's own key names, the schema id, the fixed note)
    # is not config-derived, so a config value that merely equals a key name must not
    # be mistaken for a leak.
    structural = (
        set(_walk_strings(sources))
        | set(_walk_keys(manifest))
        | {SCHEMA, str(manifest.get("omitted_note", ""))}
        | set(manifest.get("omitted_fields", []) or [])
    )
    exact_ok = structural | set(_walk_strings(freetext))
    for candidate in _walk_strings(config):
        if len(candidate) < _MIN_LEAK_LEN:
            continue
        if candidate in exact_ok:
            continue
        if any(candidate in known for known in structural):
            continue
        # Compare against the manifest's own strings, not the encoded document: a
        # secret containing a quote, backslash, newline or non-ASCII character is
        # JSON-escaped in `rendered`, so a raw substring test there would miss it.
        if any(candidate in text for text in _walk_strings(manifest)) or candidate in rendered:
            # Deliberately does not echo the offending value — this text reaches logs.
            raise RuntimeError(
                f"refusing to commit {MANIFEST_FILENAME}: a config value that did not "
                f"come from an allowlisted field appeared in the rendered document. "
                f"Either registration_service/build_manifest.py has a bug (the "
                f"allowlist reached a secret-bearing field), or the same string was "
                f"submitted in both a secret-bearing field and the agent name or "
                f"description. Registration is refused rather than publish it."
            )


def build_agent_manifest(
    *,
    name: str,
    description: str,
    slug: str,
    handler_type: str,
    config: Any,
    oasf_skills: Any,
) -> str:
    """The JSON text to commit as ``registered_agents/<slug>/agent.json``.

    Deterministic (sorted keys, stable field order) so re-registering an unchanged
    agent produces no diff.
    """
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "name": name,
        "description": description,
        "slug": slug,
        "handler_type": handler_type,
    }

    # Every raw value the allowlist reads is recorded as a legitimate source, so the
    # self-test can tell "this string came from a field we chose" from "this string
    # arrived some other way".
    # Registrant-controlled prose. Emitted (the card already carries both), but held
    # apart from `sources` so it cannot be used to launder a value past _selftest.
    freetext: list[Any] = [name, description]
    sources: list[Any] = [slug, handler_type]

    build = _build_fields(config)
    for field, key, kind in _ALLOWED:
        raw = build[field] if field in build else _get(config, field)
        coerced = _coerce(kind, raw)
        if coerced is not None:
            # Record the RAW value, and only when the coercion accepted it. Both
            # halves matter: recording it unconditionally would whitelist strings the
            # coercion deliberately threw away, while recording the *coerced* value
            # instead would trust the coercion — and a coercion that fabricates or
            # interpolates a value is precisely what _selftest exists to catch.
            sources.append(raw)
            manifest[key] = coerced

    creds = _credentials(config)
    sources.append(creds)
    if creds:
        manifest["credentials"] = creds
    skills = _skills(oasf_skills)
    sources.append(skills)
    if skills:
        manifest["oasf_skills"] = skills

    # Name (never value) the config fields this manifest deliberately leaves out, so
    # the committed tree documents its own incompleteness.
    emitted = {f for f, _, _ in _ALLOWED} | {"credentials"}
    omitted = [
        f for f in _config_field_names(config)
        if f not in emitted and _is_safe_field_name(f)
    ]
    if omitted:
        manifest["omitted_fields"] = omitted
    manifest["omitted_note"] = (
        "Fields listed in omitted_fields are intentionally not committed: they may "
        "hold secrets (extra, tools, auth_header) or free text. The runtime reads "
        "them from the registration database via AGENT_CONFIG_JSON."
    )

    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    _selftest(rendered, manifest, sources, freetext, config)
    return rendered


assert_allowlist_sane()
