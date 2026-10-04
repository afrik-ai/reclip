import time

import pytest

from conftest import make_settings, needs_ffmpeg
from core import Engine, ReclipError, classify_ytdlp_error, normalize_format, normalize_quality, validate_url


@pytest.mark.parametrize("output,code", [
    ("ERROR: Unsupported URL: https://example.com/x", "unsupported_url"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "login_required"),
    ("ERROR: [youtube] abc: Video unavailable", "unavailable"),
    ("ERROR: The uploader has not made this video available in your country", "geo_restricted"),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: something odd", "download_failed"),
])
def test_classify_errors(output, code):
    got, message = classify_ytdlp_error("WARNING: noise\n" + output)
    assert got == code
    assert not message.startswith("ERROR:")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost:8080/", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/a.mp4", "http://[::1]/", "http://0.0.0.0/",
])
def test_private_urls_blocked(url):
    with pytest.raises(ReclipError) as e:
        validate_url(url)
    assert e.value.code == "url_not_allowed"


@pytest.mark.parametrize("url", ["ftp://example.com/a", "file:///etc/passwd", "", "notaurl", "javascript:alert(1)"])
def test_bad_urls(url):
    with pytest.raises(ReclipError) as e:
        validate_url(url)
    assert e.value.code == "invalid_url"


def test_private_urls_allowed_when_configured():
    assert validate_url("http://127.0.0.1/x", allow_private=True) == "http://127.0.0.1/x"


def test_normalizers():
    assert normalize_format("audio") == "mp3"
    assert normalize_format("VIDEO") == "mp4"
    assert normalize_quality("720p") == "720"
    assert normalize_quality(None) == "best"
    with pytest.raises(ReclipError):
        normalize_format("gif")
    with pytest.raises(ReclipError):
        normalize_quality("hd; rm -rf /")


def test_quality_builds_height_filter(engine):
    job = {"format": "mp4", "format_id": None, "quality": "720", "url": "https://x.test/v"}
    cmd = engine.build_command(job, "/tmp/out")
    assert "bestvideo[height<=720]+bestaudio/best[height<=720]/best" in cmd
    assert cmd[-2:] == ["--", "https://x.test/v"]  # URL can never be parsed as an option


def test_signatures(engine):
    exp = int(time.time()) + 60
    sig = engine.sign("abc", exp)
    assert engine.verify_signature("abc", exp, sig)
    assert not engine.verify_signature("abd", exp, sig)
    assert not engine.verify_signature("abc", exp + 1, sig)
    assert not engine.verify_signature("abc", int(time.time()) - 1, engine.sign("abc", int(time.time()) - 1))


def test_per_owner_limit(tmp_path):
    eng = Engine(make_settings(tmp_path, max_active_per_owner=1, max_concurrent=1,
                               ytdlp_bin="sleep"), start_janitor=False)
    # 'sleep' as the downloader keeps jobs busy without touching the network.
    eng.build_command = lambda job, out: ["sleep", "5"]
    eng.submit("http://127.0.0.1/a", owner="alice")
    with pytest.raises(ReclipError) as e:
        eng.submit("http://127.0.0.1/b", owner="alice")
    assert e.value.code == "too_many_jobs"
    eng.submit("http://127.0.0.1/b", owner="bob")  # other owners unaffected


def test_restart_marks_running_jobs_interrupted(tmp_path):
    s = make_settings(tmp_path)
    eng = Engine(s, start_janitor=False)
    eng.store.insert({"id": "stuck", "owner": "x", "url": "u", "format": "mp4", "quality": "best",
                      "status": "downloading", "created_at": time.time()})
    eng2 = Engine(s, start_janitor=False)
    job = eng2.get("stuck")
    assert job["status"] == "error" and job["error_code"] == "interrupted"


@needs_ffmpeg
def test_download_mp4_and_mp3(engine, media_server):
    url = media_server + "/clip.mp4"
    video = engine.wait(engine.submit(url, "mp4", owner="t")["id"], 60)
    assert video["status"] == "done", video
    assert video["filename"].endswith(".mp4") and video["size_bytes"] > 0
    assert video["title"]

    audio = engine.wait(engine.submit(url, "mp3", owner="t")["id"], 60)
    assert audio["status"] == "done", audio
    assert audio["filename"].endswith(".mp3")

    again = engine.submit(url, "mp4", owner="t")
    assert again["id"] == video["id"] and again["cached"]


@needs_ffmpeg
def test_download_missing_file_reports_code(engine, media_server):
    job = engine.wait(engine.submit(media_server + "/nope.mp4", "mp4")["id"], 60)
    assert job["status"] == "error"
    assert job["error_code"] == "unavailable"


@needs_ffmpeg
def test_expiry_cleanup(engine, media_server):
    job = engine.wait(engine.submit(media_server + "/clip.mp4", "mp3")["id"], 60)
    engine.store.update(job["id"], expires_at=time.time() - 1)
    assert engine.cleanup_expired() == 1
    job = engine.get(job["id"])
    assert job["status"] == "expired" and job["file_path"] is None
