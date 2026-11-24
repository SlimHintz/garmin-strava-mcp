"""Auth, OAuth metadata, the training log, and the Garmin login retry."""

import sys
import types

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server

AUTH = {"Authorization": "Bearer test-key-0123456789abcdef"}


@pytest.fixture(scope="module")
def client():
    # one client per module: the MCP session manager's lifespan can only start once per process
    with TestClient(server.app) as c:
        yield c


def test_health_needs_no_auth(client):
    assert client.get("/").json() == {"status": "ok"}


def test_rest_rejects_missing_and_wrong_keys(client):
    assert client.get("/strava/stats").status_code in (401, 403)
    assert client.get("/strava/stats", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_mcp_endpoint_requires_auth_and_points_to_oauth_metadata(client):
    r = client.post("/tools/", json={})
    assert r.status_code == 401
    assert "https://running.example.com/.well-known/oauth-protected-resource" in r.headers["www-authenticate"]


def test_oauth_metadata_uses_public_url(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == "https://running.example.com"
    assert meta["token_endpoint"] == "https://running.example.com/oauth/token"
    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == "https://running.example.com/tools"


def test_fmt_pace():
    assert server._fmt_pace(458) == "7:38"
    assert server._fmt_pace(None) is None


def test_training_log_round_trip():
    out = server.log_run(
        activity_id="a1", week=3, phase="base", date="2026-01-10", name="Easy run",
        distance_mi=6.2, moving_time_sec=3100, avg_hr=140, max_hr=152, avg_pace_sec_per_mi=500,
        avg_cadence=172,
    )
    assert out["status"] == "logged" and out["pace"] == "8:20"
    # logging the same activity again updates it rather than duplicating it
    server.log_run(
        activity_id="a1", week=3, phase="base", date="2026-01-10", name="Easy run (edited)",
        distance_mi=6.2, moving_time_sec=3100, avg_hr=141, max_hr=152, avg_pace_sec_per_mi=500,
    )
    rows = server.get_training_log(week_number=3)
    assert len(rows) == 1
    assert rows[0]["name"] == "Easy run (edited)"
    assert rows[0]["pace_formatted"] == "8:20"


def _fake_garmin(display_name):
    class FakeGarmin:
        logins = 0

        def __init__(self, email, password):
            self.display_name = None

        def login(self):
            FakeGarmin.logins += 1
            self.display_name = display_name

    return FakeGarmin


def test_garmin_client_does_not_cache_an_incomplete_login(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "GARMIN_CREDS", tmp_path / "garmin_creds.json")
    (tmp_path / "garmin_creds.json").write_text('{"email": "a@example.com", "password": "x"}')
    monkeypatch.setattr(server, "_garmin_client", None)

    broken = _fake_garmin(display_name=None)
    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=broken))
    with pytest.raises(HTTPException) as exc:
        server.garmin_client()
    assert exc.value.status_code == 503
    assert server._garmin_client is None  # next request retries

    healthy = _fake_garmin(display_name="runner")
    monkeypatch.setitem(sys.modules, "garminconnect", types.SimpleNamespace(Garmin=healthy))
    first = server.garmin_client()
    assert server.garmin_client() is first and healthy.logins == 1
