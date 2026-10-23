"""Login, manual OTP and session protection."""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from tests.conftest import HTML, JSON, csrf_from, login, otp_from


def test_protected_pages_redirect_browsers_and_401_api_clients(portal: TestClient) -> None:
    resp = portal.get("/claims/CLM-2026-0142", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login?next=")
    assert portal.get("/claims", headers=JSON).status_code == 401
    assert portal.get("/admin", headers=JSON).status_code == 401


def test_wrong_password_rejected(portal: TestClient) -> None:
    resp = portal.post("/login", data={"username": "arjun", "password": "nope"}, headers=HTML)
    assert resp.status_code == 401 and "Incorrect username or password" in resp.text


def test_password_alone_does_not_sign_in(portal: TestClient) -> None:
    resp = portal.post("/login", data={"username": "arjun", "password": "demo123"}, headers=HTML)
    assert str(resp.url).endswith("/otp")
    assert "DEMO SMS — SIMULATION" in resp.text and 'id="synthetic-tag"' in resp.text
    assert portal.get("/claims", headers=JSON).status_code == 401


def test_otp_is_not_stored_in_the_session_cookie(portal: TestClient) -> None:
    resp = portal.post("/login", data={"username": "arjun", "password": "demo123"}, headers=HTML)
    code = otp_from(resp.text)
    cookie = portal.cookies.get("portal_session")
    payload = cookie.split(".")[0]
    decoded = base64.b64decode(payload + "=" * (-len(payload) % 4)).decode()
    assert code not in json.dumps(json.loads(decoded))


def test_wrong_then_right_otp(portal: TestClient) -> None:
    resp = portal.post("/login", data={"username": "arjun", "password": "demo123"}, headers=HTML)
    code = otp_from(resp.text)
    wrong = "000000" if code != "000000" else "111111"
    bad = portal.post("/otp", data={"otp": wrong}, headers=HTML)
    assert bad.status_code == 401 and "Incorrect OTP" in bad.text
    good = portal.post("/otp", data={"otp": code}, headers=HTML)
    assert good.status_code == 200 and str(good.url).endswith("/claims")


def test_otp_locks_after_five_wrong_attempts(portal: TestClient) -> None:
    resp = portal.post("/login", data={"username": "arjun", "password": "demo123"}, headers=HTML)
    code = otp_from(resp.text)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        portal.post("/otp", data={"otp": wrong}, headers=HTML)
    locked = portal.post("/otp", data={"otp": code}, headers=HTML)
    assert locked.status_code == 401 and "Too many wrong attempts" in locked.text
    assert portal.get("/claims", headers=JSON).status_code == 401


def test_next_redirect_is_same_site_only(portal: TestClient) -> None:
    resp = portal.post(
        "/login", data={"username": "arjun", "password": "demo123", "next": "//evil.example"}, headers=HTML
    )
    final = portal.post("/otp", data={"otp": otp_from(resp.text)}, headers=HTML)
    assert str(final.url).endswith("/claims")


def test_logout_needs_csrf_then_ends_session(portal: TestClient) -> None:
    csrf = login(portal)
    assert portal.post("/logout", data={"csrf_token": "forged"}, headers=JSON).status_code == 403
    portal.post("/logout", data={"csrf_token": csrf}, headers=HTML)
    assert portal.get("/claims", headers=JSON).status_code == 401


def test_every_page_shows_synthetic_tag(portal: TestClient) -> None:
    csrf = login(portal)
    assert csrf == csrf_from(portal.get("/claims", headers=HTML).text)
    for path in ("/", "/login", "/claims", "/claims/CLM-2026-0142", "/admin"):
        assert 'id="synthetic-tag"' in portal.get(path, headers=HTML).text, path
