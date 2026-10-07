import jwt
import pytest
from flask import Flask, g, jsonify

from app.config import Settings
from app.security import (ACCESS, REFRESH, auth_required, decode_token, internal_required, issue_pair,
                          issue_token)

SECRET = "x" * 40
S = Settings(jwt_secret=SECRET, internal_token="internal-secret")


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.config["SETTINGS"] = S

    @app.get("/any")
    @auth_required()
    def any_user():
        return jsonify(g.user)

    @app.get("/teachers")
    @auth_required("teacher", "admin")
    def teachers():
        return jsonify(g.user)

    @app.get("/internal")
    @internal_required
    def internal():
        return jsonify(ok=True)

    return app.test_client()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_pair_contains_both_tokens_with_the_right_types():
    pair = issue_pair(S, 7, "student")
    assert decode_token(SECRET, "codecheck", pair["access_token"], ACCESS)["sub"] == "7"
    assert decode_token(SECRET, "codecheck", pair["refresh_token"], REFRESH)["role"] == "student"
    assert pair["token_type"] == "Bearer" and pair["expires_in"] == 900


def test_access_token_is_not_a_refresh_token_and_back():
    pair = issue_pair(S, 7, "student")
    with pytest.raises(jwt.PyJWTError):
        decode_token(SECRET, "codecheck", pair["access_token"], REFRESH)
    with pytest.raises(jwt.PyJWTError):
        decode_token(SECRET, "codecheck", pair["refresh_token"], ACCESS)


def test_expired_foreign_issuer_and_wrong_secret_are_rejected():
    expired = issue_token(SECRET, "codecheck", 1, "student", ACCESS, 60, now=1_000)
    foreign = issue_token(SECRET, "someone-else", 1, "student", ACCESS, 60)
    forged = issue_token("y" * 40, "codecheck", 1, "student", ACCESS, 60)
    for token in (expired, foreign, forged, "garbage", None):
        with pytest.raises(jwt.PyJWTError):
            decode_token(SECRET, "codecheck", token, ACCESS)


def test_kong_matches_the_iss_claim():
    # kong.yml has a jwt credential with key "codecheck": it is looked up by the `iss` claim
    token = issue_pair(S, 1, "student")["access_token"]
    assert jwt.decode(token, options={"verify_signature": False})["iss"] == "codecheck"


def test_auth_required(client):
    assert client.get("/any").status_code == 401
    assert client.get("/any", headers={"Authorization": "Basic abc"}).status_code == 401
    token = issue_pair(S, 5, "student")["access_token"]
    resp = client.get("/any", headers=bearer(token))
    assert resp.status_code == 200 and resp.get_json() == {"id": 5, "role": "student"}
    refresh = issue_pair(S, 5, "student")["refresh_token"]
    assert client.get("/any", headers=bearer(refresh)).status_code == 401


def test_roles(client):
    student = issue_pair(S, 5, "student")["access_token"]
    teacher = issue_pair(S, 6, "teacher")["access_token"]
    assert client.get("/teachers", headers=bearer(student)).status_code == 403
    assert client.get("/teachers", headers=bearer(teacher)).status_code == 200


def test_internal_token(client):
    assert client.get("/internal").status_code == 403
    assert client.get("/internal", headers={"X-Internal-Token": "nope"}).status_code == 403
    assert client.get("/internal", headers={"X-Internal-Token": "internal-secret"}).status_code == 200


def test_internal_is_closed_when_no_token_is_configured():
    app = Flask(__name__)
    app.config["SETTINGS"] = Settings()                       # internal_token == ""

    @app.get("/internal")
    @internal_required
    def internal():
        return "ok"

    assert app.test_client().get("/internal", headers={"X-Internal-Token": ""}).status_code == 403
