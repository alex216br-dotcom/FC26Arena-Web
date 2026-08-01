from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

def hash_password(value: str) -> str:
    return _hasher.hash(value)

def verify_password(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, value)
    except VerifyMismatchError:
        return False
