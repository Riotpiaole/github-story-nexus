"""Symmetric encryption for sensitive fields stored in MongoDB.

Uses Fernet (AES-128-CBC + HMAC-SHA256) keyed from FLASK_SECRET_KEY.
The key is derived via SHA-256 so any-length secret works as input.
"""

import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet(secret_key: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str, secret_key: str) -> str:
    """Encrypt a plaintext token. Returns a URL-safe base64 ciphertext string."""
    if not token:
        return ""
    return _fernet(secret_key).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret_key: str) -> str:
    """Decrypt a ciphertext produced by encrypt_token. Returns plaintext."""
    if not encrypted:
        return ""
    return _fernet(secret_key).decrypt(encrypted.encode()).decode()
