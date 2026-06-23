"""AES-256-GCM encryption for per-user credential storage."""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _load_key() -> bytes:
    raw = os.getenv("CREDENTIALS_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY env var is not set")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY must be 32 bytes (base64-encoded)")
    return key


def encrypt(data: dict) -> str:
    """Encrypt a dict to a base64-encoded ciphertext string (nonce + tag + ciphertext)."""
    key = _load_key()
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext_with_tag).decode()


def decrypt(encoded: str) -> dict:
    """Decrypt a ciphertext string produced by encrypt() back to a dict."""
    key = _load_key()
    raw = base64.b64decode(encoded)
    nonce = raw[:12]
    ciphertext_with_tag = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    return json.loads(plaintext)
