import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

_hasher = PasswordHasher()

def hash_password(value: str) -> str:
    return _hasher.hash(value)

def verify_password(value: str, hashed: str) -> bool:
    if not value or not hashed:
        return False
    try:
        return _hasher.verify(hashed, value)
    except (VerificationError, InvalidHashError, TypeError, ValueError):
        return False

def token_urlsafe(size: int = 32) -> str:
    return secrets.token_urlsafe(size)
