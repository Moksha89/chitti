from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    """Encrypts provider credentials with a host-supplied key.

    This protects values from database readers, but not from a host process
    that can read the environment key. The key must remain outside PostgreSQL.
    """

    version = "v1"

    def __init__(self, encoded_key: str) -> None:
        # This key is host-environment protection, not an absolute boundary:
        # any process that can read the environment can decrypt these values.
        if not encoded_key:
            raise ValueError("GOOGLE_CREDENTIALS_KEY is not configured")
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except (ValueError, binascii.Error):
            try:
                key = bytes.fromhex(encoded_key)
            except ValueError as exc:
                raise ValueError("GOOGLE_CREDENTIALS_KEY must be base64 or hex") from exc
        if len(key) not in {16, 24, 32}:
            raise ValueError("GOOGLE_CREDENTIALS_KEY must decode to an AES key")
        self.key = key

    def encrypt(self, value: str | dict[str, Any]) -> str:
        payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        nonce = os.urandom(12)
        encrypted = AESGCM(self.key).encrypt(nonce, payload.encode("utf-8"), None)
        return f"{self.version}:{base64.urlsafe_b64encode(nonce + encrypted).decode('ascii')}"

    def decrypt(self, value: str) -> str:
        version, encoded = value.split(":", 1)
        if version != self.version:
            raise ValueError("unsupported credential ciphertext version")
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
