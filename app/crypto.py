"""
Credential encryption for tenant git credentials.
Uses Fernet symmetric encryption derived from the app's SECRET_KEY.
"""

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet


def _get_fernet(secret_key: str) -> Fernet:
    """Derive a Fernet key from the app's SECRET_KEY."""
    key = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_credential(plaintext: str, secret_key: str) -> str:
    """Encrypt a credential string for storage."""
    f = _get_fernet(secret_key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str, secret_key: str) -> str:
    """Decrypt a stored credential."""
    f = _get_fernet(secret_key)
    return f.decrypt(ciphertext.encode()).decode()


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        (full_key, key_hash, key_prefix)
        - full_key: shown to user once (e.g. "aops_live_a1b2c3d4...")
        - key_hash: stored in DB for lookup
        - key_prefix: first 12 chars, stored for display
    """
    random_part = secrets.token_hex(24)
    full_key = f"aops_live_{random_part}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = full_key[:16]
    return full_key, key_hash, key_prefix
