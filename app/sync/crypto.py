"""Encryption for Phase 20 Encrypted Sync - AES-256-GCM (authenticated
encryption, via the ``cryptography`` package this codebase already depends
on transitively through ``pypdf``, so no new dependency is added) and
scrypt for passphrase/recovery-key derivation (also already available in
``cryptography.hazmat.primitives.kdf.scrypt`` - Argon2id would need a new
``argon2-cffi`` dependency this environment does not have installed;
scrypt with strong parameters is the documented fallback the phase brief
itself allows, and is never plain SHA-256(password)).

Threat model (Part 22): assume the sync storage - a folder, a future
object-storage/WebDAV backend - is fully compromised. An attacker with
only the encrypted files must not be able to read content (AES-256-GCM's
confidentiality) or modify ciphertext undetected (AES-256-GCM's built-in
authentication tag - any bit flipped anywhere in the ciphertext, nonce, or
associated data makes decryption fail, never silently return corrupted
plaintext). Associated data binds each package to the exact record it
claims to be (record_type, global_id, deleted-flag, version) so a captured
package cannot be replayed under a different id, and a version number lets
the caller (see app/storage/sync_store.SyncVersionStore) refuse a replay
of an older version of the same record.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.sync.types import EncryptedPackage

#: 256-bit keys throughout - the master key, and any key derived from a
#: passphrase/recovery code to wrap it.
KEY_LENGTH = 32
#: AES-GCM's standard, recommended nonce length. A fresh random nonce is
#: drawn for every single encryption (never reused for the same key) - see
#: encrypt_payload.
NONCE_LENGTH = 12
#: Scrypt parameters conservative enough to meaningfully slow down an
#: offline brute force of a recovery passphrase, while still completing in
#: well under a second on ordinary hardware - this runs once per pairing/
#: recovery action, never per sync record.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_LENGTH = 16

SCHEMA_VERSION = 1
CONTENT_TYPE_JSON = "application/json"


class CryptoError(Exception):
    """Decryption failed - wrong key, or the ciphertext/AAD was tampered
    with. Deliberately one exception for both causes: distinguishing them
    to a caller would let an attacker use the difference as an oracle."""


def generate_master_key() -> bytes:
    """A fresh, random 256-bit key - this device's own copy of the one key
    that encrypts every synced record. Never derived from anything
    guessable; see wrap_master_key for how it survives being backed up."""
    return secrets.token_bytes(KEY_LENGTH)


def generate_recovery_key() -> str:
    """A user-facing recovery code: 20 random bytes, base32-encoded and
    grouped for readability (e.g. ``ABCD-EFGH-...``). Not a BIP39 word
    list (out of scope for this phase) - just enough entropy (160 bits)
    that guessing it is infeasible, in a shape a person can write down."""
    raw = secrets.token_bytes(20)
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(encoded[i:i + 4] for i in range(0, len(encoded), 4))


def _normalise_recovery_key(recovery_key: str) -> bytes:
    return (recovery_key or "").strip().replace("-", "").replace(" ", "").upper().encode("ascii")


def derive_key_from_passphrase(passphrase: str, salt: bytes, *,
                               n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    """scrypt(passphrase, salt) -> 32-byte key. Never plain SHA-256(password)
    (Part 3's explicit warning) - scrypt's memory-hardness is what makes an
    offline brute force of a human-chosen passphrase or recovery code
    actually expensive."""
    kdf = Scrypt(salt=salt, length=KEY_LENGTH, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_payload(key: bytes, payload: dict, *, aad: bytes = b"") -> EncryptedPackage:
    """Encrypt a JSON-serializable payload with AES-256-GCM under ``key``,
    binding ``aad`` (associated data - never secret, always authenticated)
    so the ciphertext cannot be silently repurposed for a different
    record. Raises normal exceptions (ValueError, TypeError) for a
    non-serializable payload - a programmer error, not a security event."""
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(NONCE_LENGTH)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return EncryptedPackage(schema_version=SCHEMA_VERSION, content_type=CONTENT_TYPE_JSON,
                            nonce=nonce, ciphertext=ciphertext)


def decrypt_payload(key: bytes, package: EncryptedPackage, *, aad: bytes = b"") -> dict:
    """The inverse of encrypt_payload. Raises CryptoError - never returns
    a partially-decrypted or unauthenticated result - if the key is wrong,
    ``aad`` does not match what was encrypted (e.g. this package is being
    presented for a different record than the one it was made for), or
    any byte of the ciphertext was altered."""
    try:
        plaintext = AESGCM(key).decrypt(package.nonce, package.ciphertext, aad)
        return json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, ValueError, KeyError) as exc:
        raise CryptoError("Could not decrypt: wrong key, or the data was altered.") from exc


def storage_key(record_type: str, global_id: str) -> str:
    """The provider-visible key a record is stored under - also the
    associated data its encryption is bound to (see encrypt_record), so a
    ciphertext copied into a different record's storage slot fails to
    decrypt there rather than being silently accepted."""
    return f"{record_type}:{global_id}"


def encrypt_record(key: bytes, record: "SyncRecord") -> EncryptedPackage:
    """Encrypt an entire SyncRecord (payload plus its version/deleted/
    modified_at/device_id metadata - all authenticated together by AES-GCM,
    whether inside the ciphertext or the AAD makes no difference to that
    guarantee) bound to its own storage key."""
    aad = storage_key(record.record_type, record.global_id).encode("utf-8")
    wire = {
        "record_type": record.record_type, "global_id": record.global_id,
        "payload": record.payload, "modified_at": record.modified_at,
        "device_id": record.device_id, "deleted": record.deleted,
        "version": record.version,
    }
    return encrypt_payload(key, wire, aad=aad)


def decrypt_record(key: bytes, package: EncryptedPackage, *, expected_key: str) -> "SyncRecord":
    """The inverse of encrypt_record. Raises CryptoError for a wrong key,
    tampered ciphertext, OR a ciphertext that decrypts fine but claims a
    different (record_type, global_id) than ``expected_key`` names - the
    "swapped into the wrong storage slot" attack the AAD binding is meant
    to catch even in the (should-be-impossible-anyway) case a future bug
    computed the wrong AAD at encrypt time."""
    from app.sync.types import SyncRecord

    wire = decrypt_payload(key, package, aad=expected_key.encode("utf-8"))
    actual_key = storage_key(wire["record_type"], wire["global_id"])
    if actual_key != expected_key:
        raise CryptoError(
            f"Decrypted record claims key '{actual_key}', expected '{expected_key}'.")
    return SyncRecord(
        record_type=wire["record_type"], global_id=wire["global_id"], payload=wire["payload"],
        modified_at=wire["modified_at"], device_id=wire["device_id"], deleted=wire["deleted"],
        version=wire["version"])


@dataclass(frozen=True)
class WrappedKey:
    """A master key, wrapped (encrypted) under a key derived from a
    recovery code or passphrase - what actually gets written to the sync
    folder (Part 5/23) so a brand-new device with only the recovery code
    can recover the master key without it ever having been transmitted or
    stored in plaintext anywhere."""

    salt: bytes
    nonce: bytes
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        return self.salt + self.nonce + self.ciphertext

    @classmethod
    def from_bytes(cls, data: bytes) -> "WrappedKey":
        if len(data) < SALT_LENGTH + NONCE_LENGTH:
            raise ValueError("wrapped key too short")
        salt = data[:SALT_LENGTH]
        nonce = data[SALT_LENGTH:SALT_LENGTH + NONCE_LENGTH]
        ciphertext = data[SALT_LENGTH + NONCE_LENGTH:]
        return cls(salt=salt, nonce=nonce, ciphertext=ciphertext)


def wrap_master_key(master_key: bytes, recovery_key: str) -> WrappedKey:
    """Encrypt ``master_key`` under a key derived from the recovery code,
    for safekeeping in the sync folder/backup - never the master key
    itself in plaintext (Part 3)."""
    salt = os.urandom(SALT_LENGTH)
    wrapping_key = derive_key_from_passphrase("", salt) if not recovery_key else \
        derive_key_from_passphrase(_normalise_recovery_key(recovery_key).decode("ascii"), salt)
    nonce = os.urandom(NONCE_LENGTH)
    ciphertext = AESGCM(wrapping_key).encrypt(nonce, master_key, b"pybrowser-sync-master-key")
    return WrappedKey(salt=salt, nonce=nonce, ciphertext=ciphertext)


def unwrap_master_key(wrapped: WrappedKey, recovery_key: str) -> bytes:
    """The recovery path (Part 23): given only the recovery code and the
    wrapped key from the sync folder, recover the master key. Raises
    CryptoError for a wrong recovery code - the same "no oracle" reasoning
    as decrypt_payload."""
    wrapping_key = derive_key_from_passphrase(
        _normalise_recovery_key(recovery_key).decode("ascii"), wrapped.salt)
    try:
        return AESGCM(wrapping_key).decrypt(
            wrapped.nonce, wrapped.ciphertext, b"pybrowser-sync-master-key")
    except InvalidTag as exc:
        raise CryptoError("Wrong recovery key.") from exc
