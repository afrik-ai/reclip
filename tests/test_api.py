import pytest

from app import create_app
from conftest import make_settings, needs_ffmpeg
from core import Engine

KEY_A, KEY_B = "key-alice", "key-bob"
A = {"Authorization": f"Bearer {KEY_A}"}
B = {"X-API-Key": KEY_B}


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path, api_keys=(KEY_A, KEY_B), enable_ui=False)
    app = create_app(engine=Engine(settings, start_janitor=False))
    return app.test_client()


@pytest.fixture
def ui_client(tmp_path):
    settings = make_settings(tmp_path)
    return create_app(engine=Engine(settings, start_janitor=False)).test_client()


def test_health_and_openapi_are_public(client):
    assert client.get("/api/v1/health").json["auth_required"] is True
    spec = client.get("/openapi.json").json
    assert spec["openapi"].startswith("3.1")
    assert "/api/v1/downloads" in spec["paths"]


def test_auth_required(client):
    r = client.post("/api/v1/downloads", json={"url": "https://example.com/v"})
    assert r.status_code == 401 and r.json["error"]["code"] == "unauthorized"
    r = client.post("/api/v1/downloads", json={"url": "https://example.com/v"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_ui_disabled_when_keys_set(client):
    assert client.get("/").status_code == 404
    assert client.post("/api/download", json={"url": "x"}).status_code == 404


def test_validation_errors(client):
    r = client.post("/api/v1/downloads", json={}, headers=A)
    assert r.status_code == 400 and r.json["error"]["code"] == "invalid_url"
    r = client.post("/api/v1/downloads", json={"url": "https://e.com/v", "format": "gif"}, headers=A)
    assert r.json["error"]["code"] == "invalid_format"
    r = client.get("/api/v1/downloads/doesnotexist", headers=A)
    assert r.status_code == 404 and r.json["error"]["code"] == "not_found"
    assert client.get("/api/v1/nope", headers=A).json["error"]["code"] == "not_found"


@needs_ffmpeg
def test_agent_flow(client, media_server):
    url = media_server + "/clip.mp4"
    r = client.post("/api/v1/downloads", json={"url": url, "format": "mp3", "wait": 60}, headers=A)
    assert r.status_code == 200, r.json
    job = r.json
    assert job["status"] == "done" and job["format"] == "mp3"
    assert job["filename"].endswith(".mp3") and job["error"] is None

    # The signed link works without an API key, so it can be handed to other tools.
    path = job["file_url"].replace("http://localhost", "")
    r = client.get(path)
    assert r.status_code == 200 and len(r.data) == job["size_bytes"]
    assert client.get(path.replace("sig=", "sig=0")).status_code == 403

    # Authenticated stream, status, listing.
    assert client.get(f"/api/v1/downloads/{job['id']}/file", headers=A).status_code == 200
    assert client.get(f"/api/v1/downloads/{job['id']}", headers=A).json["status"] == "done"
    assert [d["id"] for d in client.get("/api/v1/downloads", headers=A).json["downloads"]] == [job["id"]]

    # Another key can't see or fetch it.
    assert client.get(f"/api/v1/downloads/{job['id']}", headers=B).status_code == 404
    assert client.get("/api/v1/downloads", headers=B).json["downloads"] == []

    # Same request again is served from cache.
    again = client.post("/api/v1/downloads", json={"url": url, "format": "mp3"}, headers=A).json
    assert again["id"] == job["id"] and again["cached"] is True

    assert client.delete(f"/api/v1/downloads/{job['id']}", headers=A).status_code == 204
    assert client.get(path).status_code == 404


@needs_ffmpeg
def test_async_then_poll(client, media_server):
    r = client.post("/api/v1/downloads", json={"url": media_server + "/clip.mp4"}, headers=A)
    assert r.status_code in (200, 202)
    job = client.get(f"/api/v1/downloads/{r.json['id']}?wait=60", headers=A).json
    assert job["status"] == "done" and job["filename"].endswith(".mp4")


@needs_ffmpeg
def test_info_and_errors(client, media_server):
    info = client.post("/api/v1/info", json={"url": media_server + "/clip.mp4"}, headers=A).json
    assert info["title"] == "clip"
    r = client.post("/api/v1/downloads", json={"url": media_server + "/missing.mp4", "wait": 60}, headers=A)
    assert r.json["status"] == "error" and r.json["error"]["code"] == "unavailable"


@needs_ffmpeg
def test_web_ui_contract_unchanged(ui_client, media_server):
    assert ui_client.get("/").status_code == 200
    info = ui_client.post("/api/info", json={"url": media_server + "/clip.mp4"}).json
    assert set(info) == {"title", "thumbnail", "duration", "uploader", "formats"}
    job_id = ui_client.post("/api/download", json={"url": media_server + "/clip.mp4", "format": "video",
                                                   "title": "clip"}).json["job_id"]
    import time
    for _ in range(120):
        status = ui_client.get(f"/api/status/{job_id}").json
        if status["status"] != "downloading":
            break
        time.sleep(0.5)
    assert status["status"] == "done", status
    r = ui_client.get(f"/api/file/{job_id}")
    assert r.status_code == 200 and r.headers["Content-Disposition"].startswith("attachment")
    err = ui_client.post("/api/info", json={"url": "ftp://x"}).json
    assert isinstance(err["error"], str)
