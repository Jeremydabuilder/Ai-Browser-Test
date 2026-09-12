"""Sensitivity classification for MCP tools - independent of anything the
server itself says.

This is the piece of the existing safety model that does NOT transfer for
free from app/browser/safety.py. That module classifies real DOM/URL facts
(a `type=password` field, a `download` attribute); an MCP tool has no DOM.
What it has instead is a name and a JSON Schema, both supplied by the
server - and a compromised or merely careless server can describe a
destructive tool as "search". So this classifier never reads a server's own
``description`` text as evidence of safety, only:

1. The tool's own ``name`` (verb-pattern matching).
2. Its input schema's shape (a `confirm` flag, a body/content field, a
   field name that suggests money or credentials).

Anything that does not match a known-safe pattern is UNKNOWN, and UNKNOWN
fails closed - never treated as read-only, never auto-exposed to the agent.
This mirrors app/agent/tools.py's own stated rule for its native tools: "a
tool nobody thought to classify is treated as a write, not as harmless."
"""

from __future__ import annotations

import re
from typing import Any

from app.mcp.types import Sensitivity

# Ordered so a more specific/dangerous pattern is checked before a more
# general safe-looking one - "delete_search_history" must not be caught by
# a bare "search" match, so DESTRUCTIVE and SENSITIVE patterns are tried
# first, then WRITE, then READ_ONLY last.

#: Money movement, irreversible external effects. Checked first because a
#: name like "purchase_and_list_receipt" must not fall through to WRITE.
_SENSITIVE_VERBS = (
    "pay", "purchase", "buy", "charge", "refund", "transfer", "withdraw",
    "checkout", "order",
)

#: Deletion / destruction - recoverable in principle, but not by PyBrowser.
_DESTRUCTIVE_VERBS = (
    "delete", "remove", "drop", "purge", "revoke", "destroy", "erase",
    "truncate", "wipe", "uninstall", "deactivate", "terminate",
)

#: Any other write. Broad on purpose - the READ_ONLY list below is the
#: narrow, explicit one; WRITE is checked before READ_ONLY specifically to
#: avoid a name like "update_search_index" ever reading as read-only.
_WRITE_VERBS = (
    "create", "update", "upload", "send", "submit", "write", "append",
    "edit", "modify", "set", "insert", "add", "push", "publish", "post",
    "put", "patch", "move", "rename", "copy", "share", "invite", "grant",
    "merge", "commit", "run", "execute", "trigger", "start", "stop",
    "cancel", "approve", "reject", "sign",
)

#: The only tools eligible for READ_ONLY. Deliberately a narrower net than
#: the write lists above - a name that matches nothing here is UNKNOWN, not
#: assumed safe.
_READ_ONLY_VERBS = (
    "get", "list", "search", "read", "fetch", "find", "describe", "show",
    "view", "query", "lookup", "look_up", "inspect", "check", "count",
    "download",  # reading a file's bytes out, not writing anywhere
    "echo",      # the test server's own no-op tool
    "ping", "health", "status", "whoami", "diff", "log", "logs", "history",
)

#: Schema-shape signals that override a name-based READ_ONLY match - a tool
#: named "get_report" that also accepts a `confirm` boolean or a body/content
#: field is not actually read-only, whatever it is called.
_WRITE_SHAPED_FIELD_NAMES = (
    "confirm", "content", "body", "message", "data", "payload", "value",
    "text", "html",
)
_SENSITIVE_SHAPED_FIELD_NAMES = (
    "password", "token", "secret", "credential", "amount", "card",
    "account_number", "ssn", "ein",
)

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _words(name: str) -> list[str]:
    """``"searchCode"`` / ``"search_code"`` / ``"search-code"`` -> ``["search", "code"]``."""
    # Split camelCase before lower-casing, so "searchCode" doesn't become
    # one word "searchcode" that matches nothing.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [w for w in _WORD_SPLIT.split(spaced.lower()) if w]


def _matches_any(words: list[str], verbs: tuple[str, ...]) -> bool:
    verb_words = {v for verb in verbs for v in verb.split("_")}
    return any(w in verb_words for w in words)


def _schema_field_names(schema: dict[str, Any]) -> list[str]:
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    return [str(k).lower() for k in props.keys()]


def classify(name: str, schema: dict[str, Any] | None = None) -> str:
    """The sensitivity PyBrowser assigns to an MCP tool, from its name and
    schema shape alone. ``schema`` is the tool's JSON Schema ``inputSchema``;
    a tool's own free-text ``description`` is intentionally not a parameter
    here - see the module docstring.
    """
    schema = schema or {}
    words = _words(name)
    fields = _schema_field_names(schema)

    if _matches_any(words, _SENSITIVE_VERBS):
        return Sensitivity.SENSITIVE
    if any(f in _SENSITIVE_SHAPED_FIELD_NAMES for f in fields):
        return Sensitivity.SENSITIVE

    if _matches_any(words, _DESTRUCTIVE_VERBS):
        return Sensitivity.DESTRUCTIVE

    if _matches_any(words, _WRITE_VERBS):
        return Sensitivity.WRITE
    if any(f in _WRITE_SHAPED_FIELD_NAMES for f in fields):
        return Sensitivity.WRITE

    if _matches_any(words, _READ_ONLY_VERBS):
        return Sensitivity.READ_ONLY

    # No pattern matched at all - fail closed, exactly like an
    # unclassified native browser write. Never assumed safe.
    return Sensitivity.UNKNOWN


def describe_sensitivity(level: str) -> str:
    """One line for the Settings UI - what this classification means."""
    return {
        Sensitivity.READ_ONLY: "Read-only - available to Py.",
        Sensitivity.WRITE: "Writes something - not yet available (Phase 2).",
        Sensitivity.SENSITIVE: "Sensitive (money, credentials) - not yet available.",
        Sensitivity.DESTRUCTIVE: "Destructive - not yet available.",
        Sensitivity.UNKNOWN: "Could not be classified - blocked until it can be.",
    }.get(level, "Blocked.")
