"""Cryptographic primitives: password hashing, opaque session tokens, CSRF, secret encryption.

Design notes
------------
* Passwords: Argon2id with tuned cost, per-user random salt (handled by argon2-cffi), plus a
  server-side pepper mixed in via HMAC. The pepper never appears in the database, so a stolen
  dump alone cannot be attacked offline without also stealing the application secret.
* Sessions: opaque 256-bit random tokens. Only a SHA-256 digest is stored, so a database leak
  cannot be replayed as a session. Comparison is constant-time.
* CSRF: independent random token per session, double-submit pattern.
* Secrets (OAuth refresh/access tokens): AES-256-GCM via `cryptography`, with a key id so keys
  can be rotated without re-encrypting everything at once. AAD binds a ciphertext to its purpose
  and owner, so a token blob cannot be transplanted between channels.

Nothing here logs its inputs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings

# --- tunables (OWASP-aligned; memory cost is the dominant factor on Argon2id) -------------
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16


def _hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        salt_len=ARGON2_SALT_LEN,
    )


class PasswordTooLongError(ValueError):
    """Argon2 has no meaningful limit, but unbounded input invites DoS."""

    def __init__(self, limit: int = 1024) -> None:
        super().__init__(f"password exceeds {limit} bytes")
        self.limit = limit


MAX_PASSWORD_BYTES = 1024


class PasswordService:
    """Argon2id hashing with an HMAC pepper applied before hashing."""

    def __init__(self, pepper: str, hasher: PasswordHasher | None = None) -> None:
        if not pepper:
            raise ValueError("password pepper must not be empty")
        self._pepper = pepper.encode()
        self._hasher = hasher or _hasher()

    def _peppered(self, password: str) -> bytes:
        raw = password.encode("utf-8")
        if len(raw) > MAX_PASSWORD_BYTES:
            raise PasswordTooLongError(MAX_PASSWORD_BYTES)
        # HMAC-normalises length and mixes the pepper; base64 keeps it printable for argon2.
        digest = hmac.new(self._pepper, raw, hashlib.sha256).digest()
        return base64.b64encode(digest)

    def hash(self, password: str) -> str:
        return self._hasher.hash(self._peppered(password))

    def verify(self, stored_hash: str, password: str) -> bool:
        """Constant-time-ish verification; returns False for any malformed input."""
        try:
            return self._hasher.verify(stored_hash, self._peppered(password))
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except (InvalidHashError, ValueError):
            return True

    @staticmethod
    def dummy_verify() -> None:
        """Burn comparable time for unknown users so login timing does not reveal existence."""
        _hasher().hash(b"timing-equalisation-placeholder")


# --- opaque tokens -----------------------------------------------------------------------


def generate_token(nbytes: int = 32) -> str:
    """URL-safe opaque token (default 256 bits of entropy)."""
    return secrets.token_urlsafe(nbytes)


def token_digest(token: str) -> bytes:
    """SHA-256 of a token. Only this value is persisted."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def constant_time_equals(a: str | bytes, b: str | bytes) -> bool:
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


# --- AES-256-GCM secret encryption -------------------------------------------------------


@dataclass(frozen=True)
class SealedSecret:
    """Ciphertext bundle: nonce + tag are stored inside `payload` by AESGCM."""

    payload: bytes
    key_version: int

    def __post_init__(self) -> None:
        if len(self.payload) < 12 + 16:  # nonce + tag minimum
            raise ValueError("sealed payload is too short to contain nonce+tag")


class SecretBox:
    """AES-256-GCM encryption for stored credentials (OAuth tokens).

    AAD binds the ciphertext to a purpose and owner id, preventing a token blob from being
    moved between rows or tables and still decrypting.
    """

    def __init__(self, key_b64: str, key_version: int = 1) -> None:
        raw = base64.b64decode(key_b64, validate=True)
        if len(raw) != 32:
            raise ValueError("encryption key must decode to 32 bytes")
        self._aes = AESGCM(raw)
        self.key_version = key_version

    @staticmethod
    def _aad(purpose: str, owner_id: str) -> bytes:
        return f"autoclip:v1:{purpose}:{owner_id}".encode()

    def seal(self, plaintext: str, *, purpose: str, owner_id: str) -> SealedSecret:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), self._aad(purpose, owner_id))
        return SealedSecret(payload=nonce + ct, key_version=self.key_version)

    def open(self, sealed: SealedSecret, *, purpose: str, owner_id: str) -> str:
        if sealed.key_version != self.key_version:
            raise ValueError(
                f"sealed secret uses key_version {sealed.key_version}, "
                f"box holds {self.key_version}; rotate keys instead of guessing"
            )
        nonce, ct = sealed.payload[:12], sealed.payload[12:]
        try:
            return self._aes.decrypt(nonce, ct, self._aad(purpose, owner_id)).decode("utf-8")
        except InvalidTag as exc:
            raise ValueError(
                "secret failed authentication: wrong key, corrupted data, or AAD mismatch"
            ) from exc


# --- convenience accessors bound to settings ----------------------------------------------


def password_service(settings: Settings | None = None) -> PasswordService:
    s = settings or get_settings()
    return PasswordService(s.password_pepper)


def secret_box(settings: Settings | None = None) -> SecretBox:
    s = settings or get_settings()
    return SecretBox(s.secret_encryption_key)
