"""agentnet.identity: the SSO identity headers and the ADMIN_EMAILS allowlist.

Also checks that the modules both services import from agentnet stay standard
library only, and that agentnet imports neither service nor agent_skeleton.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from agentnet.identity import (
    IDENTITY_HEADERS,
    admin_emails,
    identity_from_headers,
    is_admin,
    normalize_identity,
    without_identity_headers,
)

_AGENTNET = Path(__file__).resolve().parents[1]


# ── Reading the identity ──────────────────────────────────────────────────────

def test_header_order():
    assert IDENTITY_HEADERS == ("mail", "eppn", "remote-user")
    every = {"remote-user": "carol", "eppn": "bob@x.edu", "mail": "alice@x.edu"}
    assert identity_from_headers(every) == "alice@x.edu"
    assert identity_from_headers({"remote-user": "carol", "eppn": "bob@x.edu"}) == "bob@x.edu"
    assert identity_from_headers({"remote-user": "carol"}) == "carol"


def test_a_blank_header_falls_through_to_the_next():
    assert identity_from_headers({"mail": "  ", "eppn": "Bob@X.EDU"}) == "bob@x.edu"
    assert identity_from_headers({"mail": "", "eppn": "", "remote-user": " Carol "}) == "carol"


def test_the_identity_is_stripped_and_lowercased():
    assert identity_from_headers({"mail": "  Alice@X.EDU "}) == "alice@x.edu"


@pytest.mark.parametrize("headers", [
    {},
    {"mail": "", "eppn": " ", "remote-user": "\t"},
    {"remote_user": "carol"},       # only the hyphenated spelling counts
    {"x-mail": "alice@x.edu"},
])
def test_no_identity(headers):
    assert identity_from_headers(headers) is None


@pytest.mark.parametrize("value, expected", [
    (" Alice@X.EDU ", "alice@x.edu"), ("carol", "carol"), ("", None), ("  ", None), (None, None),
])
def test_normalize_identity(value, expected):
    assert normalize_identity(value) == expected


# ── The admin list ────────────────────────────────────────────────────────────

def test_is_admin_helper(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "Admin@EXAMPLE.com, boss@example.com")
    assert is_admin("admin@example.com")            # case-insensitive
    assert is_admin(" ADMIN@example.com ")
    assert is_admin("boss@example.com")
    assert not is_admin("nobody@example.com")
    assert not is_admin(None)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert not is_admin("admin@example.com")        # empty allowlist → no admins


def test_admin_emails_parsing():
    env = {"ADMIN_EMAILS": " Admin@X.edu , ,boss@x.edu,,"}
    assert admin_emails(env) == {"admin@x.edu", "boss@x.edu"}
    assert admin_emails({"ADMIN_EMAILS": ""}) == frozenset()
    assert admin_emails({"ADMIN_EMAILS": " , "}) == frozenset()
    assert admin_emails({}) == frozenset()


@pytest.mark.parametrize("identity", [None, "", " "])
def test_a_blank_identity_is_never_admin(identity):
    assert not is_admin(identity, {"ADMIN_EMAILS": "admin@x.edu, ,"})


# ── Dropping identity headers ─────────────────────────────────────────────────

def test_without_identity_headers():
    items = [
        ("Mail", "a@x.edu"), ("mail", "b@x.edu"), ("EPPN", "c@x.edu"), ("x-other", "kept"),
        ("Remote-User", "d"), ("remote_user", "e"), ("REMOTE_USER", "f"), ("mailbox", "kept too"),
        ("x-other", "kept again"),
    ]
    assert without_identity_headers(items) == [
        ("x-other", "kept"), ("mailbox", "kept too"), ("x-other", "kept again"),
    ]


# ── Copies that must stay import-free ─────────────────────────────────────────

def test_the_dev_identity_proxy_strips_the_same_headers():
    # The proxy is standard library only and runs where agentnet is not mounted
    # (the Compose helper containers), so it keeps its own copy.
    from devstack import dev_identity_proxy

    assert dev_identity_proxy.IDENTITY_HEADERS == frozenset(IDENTITY_HEADERS)


# ── Imports ───────────────────────────────────────────────────────────────────

# Modules both services import from agentnet, which also run in their images and
# (the catalog) in locally spawned agents. Importing any of them runs the package's
# __init__.py first, so it is held to the same rule.
_STDLIB_ONLY = ("__init__.py", "identity.py", "credential_catalog.py", "settings.py")
_SERVICES = frozenset({"chat_app", "registration_service", "agent_skeleton"})


def _imported_roots(path: Path) -> set[str]:
    """The top-level module of every import in ``path``; ``.`` for a relative one."""
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add("." if node.level else node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("name", _STDLIB_ONLY)
def test_shared_modules_import_only_the_standard_library(name):
    roots = _imported_roots(_AGENTNET / name)
    assert roots or name == "__init__.py", name  # the package's is only a docstring
    assert roots <= sys.stdlib_module_names, sorted(roots - sys.stdlib_module_names)


def test_agentnet_never_imports_the_services():
    package = [
        p for p in _AGENTNET.rglob("*.py")
        if not {"tests", ".venv", "__pycache__"} & set(p.relative_to(_AGENTNET).parts)
    ]
    assert len(package) > 10
    wrong = {str(p.relative_to(_AGENTNET)): sorted(_imported_roots(p) & _SERVICES) for p in package}
    assert not {where: roots for where, roots in wrong.items() if roots}
