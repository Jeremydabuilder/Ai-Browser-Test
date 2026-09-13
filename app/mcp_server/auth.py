"""Pairing-token authentication for the PyBrowser MCP server.

A pairing token is a `secrets.token_urlsafe(32)` value shown to the user
exactly once, at pairing time - only its SHA-256 hash is ever persisted
(see app/storage/mcp_server_store.py). This is the same trust model as a
password: the OS keyring (used elsewhere in PyBrowser for *retrievable*
outbound credentials like a provider API key) is the wrong tool here,
since we only ever need to verify a presented token against a hash, never
read a stored one back out.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.mcp_server.types import PairedClient
    from app.storage.mcp_server_store import McpServerAccessStore


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def pair_client(
    store: "McpServerAccessStore", *, display_name: str, capabilities: list[str],
) -> tuple["PairedClient", str]:
    """Create a new client with the given capabilities (empty by default -
    a caller must actively opt a client into each one). Returns the stored
    client record and the plaintext token - the ONLY time the plaintext
    token exists outside the user's clipboard."""
    token = generate_token()
    client_id = uuid.uuid4().hex
    client = store.create_client(
        client_id, display_name=display_name, token_hash=_hash_token(token),
        capabilities=capabilities)
    return client, token


def verify_token(store: "McpServerAccessStore", token: str) -> "PairedClient | None":
    """Fail closed: no token, no match, or a revoked client all return
    None. Never raises on a malformed token - malformed input is simply
    not a match."""
    if not token:
        return None
    client = store.find_by_token_hash(_hash_token(token))
    if client is None or client.revoked:
        return None
    return client


def revoke_client(store: "McpServerAccessStore", client_id: str) -> None:
    store.revoke(client_id)
