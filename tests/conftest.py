import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import Engine, Settings  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("yt-dlp")),
    reason="end-to-end tests need ffmpeg and yt-dlp on PATH",
)


@pytest.fixture(scope="session")
def media_server(tmp_path_factory):
    """Serve a tiny generated MP4 over HTTP so yt-dlp can 'download' it offline."""
    root = tmp_path_factory.mktemp("media")
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(root / "clip.mp4")],
            check=True,
        )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def make_settings(tmp_path, **overrides):
    s = Settings()
    s.download_dir = str(tmp_path / "downloads")
    s.allow_private_urls = True
    s.download_timeout = 60
    s.secret = "test-secret"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def engine(tmp_path):
    return Engine(make_settings(tmp_path), start_janitor=False)
