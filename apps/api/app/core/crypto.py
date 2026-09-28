"""Encryption at rest for stored credentials (Fernet: AES-128-CBC + HMAC-SHA256).

ENCRYPTION_KEY holds one or more comma-separated Fernet keys. The first encrypts; all of
them decrypt, so a key can be rotated by prepending a new one (then re-saving
credentials) without breaking existing rows.

Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings


class CredentialDecryptionError(Exception):
    """Ciphertext couldn't be decrypted with any configured key (key changed or data corrupt)."""


class CredentialCipher:
    def __init__(self, keys: str):
        parts = [key.strip() for key in keys.split(",") if key.strip()]
        if not parts:
            raise ValueError("ENCRYPTION_KEY is empty")
        fernets = []
        for index, key in enumerate(parts, start=1):
            try:
                fernets.append(Fernet(key.encode()))
            except (ValueError, TypeError):
                # Never echo the key itself.
                raise ValueError(
                    f"ENCRYPTION_KEY entry {index} is not a valid Fernet key (32 url-safe base64 bytes). "
                    'Generate one with: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                ) from None
        self._fernet = MultiFernet(fernets)

    def encrypt(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            plaintext = self._fernet.decrypt(token.encode())
        except InvalidToken:
            raise CredentialDecryptionError("stored credential can't be decrypted with ENCRYPTION_KEY") from None
        value = json.loads(plaintext)
        if not isinstance(value, dict):
            raise CredentialDecryptionError("stored credential has an unexpected format")
        return value


@lru_cache
def get_cipher() -> CredentialCipher:
    return CredentialCipher(settings.ENCRYPTION_KEY.get_secret_value())
