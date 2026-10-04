"""Bearer token for state-changing operations (start/stop/policy). Stored 0600 in the gateway state dir, created on first run.

Read-only status and inference stay unauthenticated on localhost; anything that changes what is running needs the token.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path


def load_or_create(path: Path) -> str:
    path = Path(path)
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)       # never world-readable, not even briefly
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def bearer(header: str | None) -> str | None:
    """'Bearer abc' (case-insensitive scheme) -> 'abc'; anything else -> None."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def valid(token: str, header: str | None) -> bool:
    given = bearer(header)
    return given is not None and hmac.compare_digest(given.encode(), token.encode())
