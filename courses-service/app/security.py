"""JWT issuing/verification and the auth decorators. The same HS256 secret is configured in
Kong (jwt plugin) and in the other services: Kong rejects bad tokens at the edge, each
service checks the token again (defence in depth)."""
from __future__ import annotations

import hmac
import time
import uuid
from functools import wraps
from typing import Any, Optional

import jwt
from flask import current_app, g, jsonify, request

ACCESS = "access"
REFRESH = "refresh"


def error_response(status: int, code: str, message: str, details: Optional[list] = None):
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return jsonify(body), status


def issue_token(secret: str, issuer: str, user_id: int, role: str, kind: str, ttl_s: int,
                now: Optional[int] = None) -> str:
    issued = int(now if now is not None else time.time())
    claims = {"iss": issuer, "sub": str(user_id), "role": role, "type": kind,
              "iat": issued, "exp": issued + ttl_s, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_token(secret: str, issuer: str, token: Any, kind: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError for anything that is not a valid, unexpired token of `kind`."""
    claims = jwt.decode(token, secret, algorithms=["HS256"], issuer=issuer,
                        options={"require": ["exp", "iss", "sub"]})
    if claims.get("type") != kind:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def issue_pair(settings, user_id: int, role: str) -> dict[str, Any]:
    return {
        "access_token": issue_token(settings.jwt_secret, settings.jwt_issuer, user_id, role, ACCESS,
                                    settings.access_ttl_s),
        "refresh_token": issue_token(settings.jwt_secret, settings.jwt_issuer, user_id, role, REFRESH,
                                     settings.refresh_ttl_s),
        "token_type": "Bearer",
        "expires_in": settings.access_ttl_s,
    }


def auth_required(*roles: str):
    """Requires `Authorization: Bearer <access token>`; with `roles` the role must match.
    Puts {"id": int, "role": str} into flask.g.user."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.lower().startswith("bearer "):
                return error_response(401, "unauthorized", "a bearer token is required")
            settings = current_app.config["SETTINGS"]
            try:
                claims = decode_token(settings.jwt_secret, settings.jwt_issuer, header[7:].strip(), ACCESS)
                g.user = {"id": int(claims["sub"]), "role": str(claims.get("role", ""))}
            except (jwt.PyJWTError, ValueError):
                return error_response(401, "unauthorized", "the token is invalid or expired")
            if roles and g.user["role"] not in roles:
                return error_response(403, "forbidden", "your role may not do this")
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def internal_required(fn):
    """/internal/* is not routed by Kong; the shared token is a second line of defence."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config["SETTINGS"].internal_token
        given = request.headers.get("X-Internal-Token", "")
        if not expected or not hmac.compare_digest(given, expected):
            return error_response(403, "forbidden", "internal endpoint")
        return fn(*args, **kwargs)
    return wrapper
