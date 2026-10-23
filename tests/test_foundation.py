"""Phase 1: both apps start, health checks respond, config and errors behave."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import config as agent_config
from agent.server import create_app as create_agent

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Agent client with a temp DB and a portal URL nothing listens on."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("PORTAL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("BROWSER", "local")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with TestClient(create_agent(agent_config.load_settings())) as client:
        yield client


def test_agent_health(agent: TestClient) -> None:
    resp = agent.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.headers["x-request-id"]


def test_agent_landing_identifies_synthetic_demo(agent: TestClient) -> None:
    resp = agent.get("/")
    assert resp.status_code == 200
    assert "ClaimSaathi" in resp.text and "All people, policies and claims are synthetic" in resp.text


def test_agent_ready_creates_schema_and_reports_unreachable_portal(agent: TestClient) -> None:
    body = agent.get("/health/ready").json()
    assert body["database"] == {"ok": True, "detail": "ok"}
    assert body["portal"]["ok"] is False
    assert body["status"] == "degraded"
    assert body["gemini_configured"] is False


def test_portal_health_and_landing(portal: TestClient) -> None:
    assert portal.get("/health").json() == {"status": "ok", "app": "claimsaathi-portal"}
    page = portal.get("/", headers={"accept": "text/html"})
    assert 'id="synthetic-tag"' in page.text and "synthetic" in page.text.lower()


@pytest.mark.parametrize("client_fixture", ["agent", "portal"])
def test_error_envelope_is_consistent(client_fixture: str, request: pytest.FixtureRequest) -> None:
    client: TestClient = request.getfixturevalue(client_fixture)
    resp = client.get("/does-not-exist", headers={"accept": "application/json"})
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found" and err["request_id"]


def test_invalid_browser_setting_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER", "netscape")
    with pytest.raises(ValueError):
        agent_config.load_settings()


def test_env_example_has_no_secrets_and_env_is_ignored() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("GEMINI_API_KEY", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"):
        line = next(l for l in example.splitlines() if l.startswith(f"{key}="))
        assert line.split("=", 1)[1].split("#")[0].strip() == "", f"{key} must be empty in .env.example"
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
