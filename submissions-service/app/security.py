"""Access-token verification. courses-service issues the tokens (HS256, shared secret); Kong
checks them at the edge and every service checks them again (defence in depth)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt


@dataclass(frozen=True)
class User:
    id: int
    role: str

    @property
    def is_staff(self) -> bool:
        return self.role in ("teacher", "admin")


def decode_access_token(secret: str, issuer: str, token: Any) -> User:
    """Raises jwt.PyJWTError for anything that is not a valid, unexpired access token."""
    claims = jwt.decode(token, secret, algorithms=["HS256"], issuer=issuer,
                        options={"require": ["exp", "iss", "sub"]})
    if claims.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    try:
        return User(id=int(claims["sub"]), role=str(claims.get("role", "")))
    except ValueError as exc:
        raise jwt.InvalidTokenError("bad subject") from exc
