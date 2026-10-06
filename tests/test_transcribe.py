import os
import time
from types import SimpleNamespace

import pytest

import transcribe
from app import create_app
from conftest import make_settings, needs_ffmpeg
from core import Engine

KEY = "key-alice"
A = {"Authorization": f"Bearer {KEY}"}


class FakeModel:
    """Stands in for faster_whisper.WhisperModel (same transcribe() contract)."""

    def __init__(self, device="cpu", fail_on_decode=False):
        self.device = device
        self.fail_on_decode = fail_on_decode
        self.calls = []

    def transcribe(self, path, language=None, **kwargs):
        self.calls.append((path, language))
        if self.fail_on_decode:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        segs = [SimpleNamespace(start=0.0, end=1.25, text=" Hello there."),
                SimpleNamespace(start=1.25, end=2.0, text="  "),
                SimpleNamespace(start=61.5, end=3725.042, text=" General Kenobi!")]
        return iter(segs), SimpleNamespace(language=language or "en", duration=3725.042)


def fake_transcriber(**kwargs):
    models = []

    def factory(name, device, compute_type):
        models.append((name, device, compute_type))
        return FakeModel(device)

    t = transcribe.Transcriber("tiny", device="cpu", model_factory=factory, **kwargs)
    t.loaded = models
    return t


def test_transcriber_output_and_formats(tmp_path):
    t = fake_transcriber()
    progress = []
    result = t.transcribe("a.mp3", on_progress=progress.append)
    assert t.loaded == [("tiny", "cpu", "int8")]
    assert result["language"] == "en"
    assert result["text"] == "Hello there.\nGeneral Kenobi!"
    assert len(result["segments"]) == 2  # blank segment dropped
    assert progress[-1] == 99.0

    paths = transcribe.write_outputs(result, str(tmp_path))
    srt = open(paths["srt"], encoding="utf-8").read()
    assert "1\n00:00:00,000 --> 00:00:01,250\nHello there.\n" in srt
    assert "2\n00:01:01,500 --> 01:02:05,040\nGeneral Kenobi!\n" in srt
    assert open(paths["vtt"], encoding="utf-8").read().startswith("WEBVTT\n\n00:00:00.000 --> ")
    assert open(paths["txt"], encoding="utf-8").read() == "Hello there.\nGeneral Kenobi!\n"
    for path in paths.values():  # LF everywhere, Windows included
        assert b"\r" not in open(path, "rb").read()


@needs_ffmpeg
def test_faster_whisper_decodes_mp3(tmp_path):
    """Catches PyAV releases that break faster-whisper's audio loading."""
    audio = pytest.importorskip("faster_whisper.audio")
    import subprocess

    mp3 = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", "1", str(mp3)], check=True)
    samples = audio.decode_audio(str(mp3))
    assert 15000 < len(samples) < 17000  # 1 s at 16 kHz


def test_gpu_falls_back_to_cpu_when_cuda_libraries_are_missing(monkeypatch):
    monkeypatch.setattr(transcribe, "_cuda_device_count", lambda: 1)
    loaded = []

    def factory(name, device, compute_type):
        loaded.append((device, compute_type))
        return FakeModel(device, fail_on_decode=device == "cuda")

    t = transcribe.Transcriber("turbo", model_factory=factory)
    assert t.transcribe("a.mp3")["text"]
    assert loaded == [("cuda", "float16"), ("cpu", "int8")]
    assert t.device == "cpu"


def test_pinned_gpu_does_not_fall_back(monkeypatch):
    t = transcribe.Transcriber("turbo", device="cuda",
                               model_factory=lambda n, d, c: FakeModel(d, fail_on_decode=True))
    with pytest.raises(RuntimeError, match="cublas"):
        t.transcribe("a.mp3")


@pytest.fixture
def stt_engine(tmp_path):
    return Engine(make_settings(tmp_path), start_janitor=False, transcriber=fake_transcriber())


@needs_ffmpeg
def test_transcript_queued_while_downloading(stt_engine, media_server):
    job = stt_engine.submit(media_server + "/clip.mp4", fmt="mp3")
    t = stt_engine.request_transcript(job["id"], language="FR")
    assert t["status"] in ("pending", "queued", "transcribing", "done")
    t = stt_engine.wait_transcript(job["id"], 60)
    assert t["status"] == "done", t
    assert t["language"] == "fr" and t["device"] == "cpu"
    assert stt_engine.read_transcript_text(job["id"]) == "Hello there.\nGeneral Kenobi!"
    assert all(os.path.exists(p) for p in stt_engine.transcript_paths(job["id"]).values())
    # Asking again reuses the finished transcript.
    again = stt_engine.request_transcript(job["id"])
    assert again["created_at"] == t["created_at"]
    stt_engine.delete(job["id"])
    assert stt_engine.store.get_transcript(job["id"]) is None


def test_transcript_fails_with_download_error(stt_engine):
    job = stt_engine.submit("http://127.0.0.1:1/nothing.mp4", fmt="mp3")
    stt_engine.request_transcript(job["id"])
    t = stt_engine.wait_transcript(job["id"], 60)
    assert t["status"] == "error"
    assert t["error_message"].startswith("The download failed")


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path, api_keys=(KEY,), enable_ui=False)
    engine = Engine(settings, start_janitor=False, transcriber=fake_transcriber())
    return create_app(engine=engine).test_client()


@needs_ffmpeg
def test_api_download_and_transcribe(client, media_server):
    r = client.post("/api/v1/downloads", headers=A, json={
        "url": media_server + "/clip.mp4", "format": "mp3", "transcribe": True, "wait": 60})
    assert r.status_code == 200, r.json
    t = r.json["transcript"]
    assert t["status"] == "done" and t["text"] == "Hello there.\nGeneral Kenobi!"
    assert set(t["files"]) == {"txt", "srt", "vtt", "json"}
    job_id = r.json["id"]

    srt = client.get(t["files"]["srt"])
    assert srt.status_code == 200 and b"00:00:00,000 --> 00:00:01,250" in srt.data
    assert client.get(t["files"]["srt"].replace("sig=", "sig=0")).status_code == 403
    assert client.get(f"/api/v1/downloads/{job_id}/transcript.txt").status_code == 401
    assert client.get(f"/api/v1/downloads/{job_id}/transcript.txt", headers=A).data == \
        b"Hello there.\nGeneral Kenobi!\n"

    got = client.get(f"/api/v1/downloads/{job_id}/transcript?include_text=false", headers=A).json
    assert got["status"] == "done" and "text" not in got

    r = client.post(f"/api/v1/downloads/{job_id}/transcript", headers=A, json={"wait": 5})
    assert r.status_code == 200 and r.json["created_at"] == t["created_at"]


def test_api_transcript_errors(client, tmp_path):
    r = client.get("/api/v1/downloads/nope/transcript", headers=A)
    assert r.status_code == 404
    r = client.post("/api/v1/downloads", headers=A, json={
        "url": "https://example.com/v", "transcribe": True, "language": "english please"})
    assert r.status_code == 400 and r.json["error"]["code"] == "invalid_language"


def test_api_transcription_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "is_available", lambda: False)
    settings = make_settings(tmp_path, api_keys=(KEY,), enable_ui=False)
    engine = Engine(settings, start_janitor=False)
    c = create_app(engine=engine).test_client()
    assert c.get("/api/v1/health").json["transcription"] is False
    r = c.post("/api/v1/downloads", headers=A, json={"url": "https://example.com/v", "transcribe": True})
    assert r.status_code == 501 and r.json["error"]["code"] == "transcription_unavailable"
    assert engine.list() == []  # no download was started


@needs_ffmpeg
def test_mcp_local_backend_transcribe(tmp_path, media_server, monkeypatch):
    import mcp_server

    backend = mcp_server.LocalBackend.__new__(mcp_server.LocalBackend)
    backend.engine = Engine(make_settings(tmp_path), start_janitor=False, transcriber=fake_transcriber())
    backend._public = None
    res = backend.transcribe(media_server + "/clip.mp4", "", None, 60, 12)
    assert res["status"] == "done", res
    assert res["text"] == "Hello there." and res["text_truncated"] is True
    assert os.path.isfile(res["files"]["srt"])
    assert backend.get_transcript(res["download_id"], 0, 0)["text"].endswith("Kenobi!")


@pytest.mark.skipif(not os.environ.get("RECLIP_TEST_WHISPER_MODEL"),
                    reason="set RECLIP_TEST_WHISPER_MODEL (e.g. tiny) to run a real Whisper model")
@needs_ffmpeg
def test_real_whisper_model(tmp_path, media_server):
    settings = make_settings(tmp_path, whisper_model=os.environ["RECLIP_TEST_WHISPER_MODEL"])
    engine = Engine(settings, start_janitor=False)
    job = engine.submit(media_server + "/clip.mp4", fmt="mp3")
    engine.request_transcript(job["id"])
    started = time.time()
    while engine.get_transcript(job["id"])["status"] not in ("done", "error") and time.time() - started < 600:
        engine.wait_transcript(job["id"], 60)
    t = engine.get_transcript(job["id"])
    assert t["status"] == "done", t
