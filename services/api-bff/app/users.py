"""Hardcoded demo users for the Phase 2 auth scaffold (FR-14: distinguish an
SRE/admin role from a read-only finance role). Replace with a real
users/identity-provider table before this goes anywhere near production —
passwords are still bcrypt-hashed even for these throwaway dev accounts so
the auth code path is exercised exactly as it will be for real users later.

Uses the `bcrypt` package directly rather than passlib: passlib's bcrypt
backend self-test crashes against bcrypt>=4.1 (a known, unresolved passlib
compatibility bug), and passlib itself is unmaintained.
"""

from __future__ import annotations

import bcrypt

_DEV_PASSWORD_HASH = bcrypt.hashpw(b"hypertrace-dev", bcrypt.gensalt())

_USERS = {
    "sre": {"username": "sre", "role": "sre", "password_hash": _DEV_PASSWORD_HASH},
    "finance": {"username": "finance", "role": "finance", "password_hash": _DEV_PASSWORD_HASH},
}


def authenticate_user(username: str, password: str) -> dict[str, str] | None:
    user = _USERS.get(username)
    if user is None or not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"]):
        return None
    return {"username": user["username"], "role": user["role"]}
