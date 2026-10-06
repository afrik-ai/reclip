"""ReClip MCP server: lets AI agents (Claude, Cursor, etc.) download media.

Two modes:

* Local (default): downloads run inside this process with the same engine as
  the web app, and files land on this machine (default ~/Downloads/ReClip).
  Tools return the absolute ``file_path``.

* Remote: set RECLIP_API_URL (and RECLIP_API_KEY) to use a ReClip server you
  host elsewhere. Tools return a signed ``file_url`` instead.

Run:  python mcp_server.py            (stdio, for Claude Desktop / Claude Code)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import anyio

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """\
Download video (MP4) or audio (MP3) from YouTube, TikTok, Instagram, X/Twitter,
Reddit, Vimeo, SoundCloud and 1000+ other sites.

- Call download_media with the page URL. Use format="mp3" when only audio is needed.
- If the result's status is "queued" or "downloading", call get_download with its
  id and wait_seconds until status is "done" or "error".
- On "done" the result has file_path (local mode) or file_url (remote mode).
- On "error", error.code says why (e.g. login_required, unsupported_url,
  geo_restricted, too_long). Don't retry those unchanged; tell the user.
- For playlists/channels, call expand_playlist first, then download items.
"""


class LocalBackend:
    def __init__(self):
        from core import Engine, Settings, public_job

        os.environ.setdefault("RECLIP_DOWNLOAD_DIR", os.path.join(os.path.expanduser("~"), "Downloads", "ReClip"))
        # Files on your own machine are yours: don't auto-delete them.
        os.environ.setdefault("RECLIP_FILE_TTL_HOURS", "0")
        self.engine = Engine(Settings.from_env())
        self._public = public_job

    def _job(self, job):
        out = self._public(job)
        if job["status"] == "done":
            out["file_path"] = job["file_path"]
        return out

    def download(self, url, fmt, quality, wait):
        job = self.engine.submit(url, fmt=fmt, quality=quality, owner="mcp")
        cached = job.get("cached")
        if wait:
            job = self.engine.wait(job["id"], wait, "mcp")
        out = self._job(job)
        if cached:
            out["cached"] = True
        return out

    def get(self, job_id, wait):
        return self._job(self.engine.wait(job_id, wait, "mcp"))

    def info(self, url):
        info = self.engine.get_info(url)
        info.pop("formats", None)
        return info

    def playlist(self, url, limit):
        return self.engine.expand_playlist(url, limit)

    def list(self, limit):
        return {"downloads": [self._job(j) for j in self.engine.list("mcp", limit)]}


class RemoteBackend:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _call(self, method, path, payload=None, timeout=60):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except ValueError:
                return {"error": {"code": "http_error", "message": f"HTTP {e.code}"}}
        except urllib.error.URLError as e:
            return {"error": {"code": "server_unreachable", "message": str(e.reason)}}

    def download(self, url, fmt, quality, wait):
        return self._call("POST", "/api/v1/downloads",
                          {"url": url, "format": fmt, "quality": quality, "wait": wait},
                          timeout=wait + 30)

    def get(self, job_id, wait):
        q = urllib.parse.urlencode({"wait": wait})
        return self._call("GET", f"/api/v1/downloads/{urllib.parse.quote(job_id)}?{q}", timeout=wait + 30)

    def info(self, url):
        return self._call("POST", "/api/v1/info", {"url": url}, timeout=90)

    def playlist(self, url, limit):
        return self._call("POST", "/api/v1/playlist", {"url": url, "limit": limit}, timeout=90)

    def list(self, limit):
        return self._call("GET", f"/api/v1/downloads?limit={int(limit)}")


def make_backend():
    api_url = os.environ.get("RECLIP_API_URL", "").strip()
    if api_url:
        return RemoteBackend(api_url, os.environ.get("RECLIP_API_KEY", "").strip())
    return LocalBackend()


def build_server(backend=None):
    backend = backend or make_backend()
    server = _Server("reclip", instructions=INSTRUCTIONS)

    async def run(fn, *args):
        from core import ReclipError

        def call():
            try:
                return fn(*args)
            except ReclipError as e:
                return {"error": e.to_dict()}

        return await anyio.to_thread.run_sync(call)

    @server.tool()
    async def download_media(url: str, format: str = "mp4", quality: str = "best",
                             wait_seconds: int = 90) -> dict:
        """Download a video (format="mp4") or just its audio (format="mp3") from a URL.

        url: page URL on YouTube, TikTok, Instagram, X/Twitter, Vimeo, SoundCloud, etc.
        quality: "best" or a max video height like "1080", "720", "480" (ignored for mp3).
        wait_seconds: how long to wait for the download to finish before returning.
        Returns the download with status, title, size_bytes and file_path/file_url when done.
        """
        return await run(backend.download, url, format, quality, max(0, int(wait_seconds)))

    @server.tool()
    async def get_download(id: str, wait_seconds: int = 60) -> dict:
        """Check a download started by download_media, waiting up to wait_seconds for it to finish."""
        return await run(backend.get, id, max(0, int(wait_seconds)))

    @server.tool()
    async def get_media_info(url: str) -> dict:
        """Look up title, uploader, duration and available video qualities without downloading."""
        return await run(backend.info, url)

    @server.tool()
    async def expand_playlist(url: str, limit: int = 25) -> dict:
        """List the individual video URLs (with titles) in a playlist or channel URL."""
        return await run(backend.playlist, url, max(1, int(limit)))

    @server.tool()
    async def list_downloads(limit: int = 20) -> dict:
        """List recent downloads, newest first."""
        return await run(backend.list, max(1, int(limit)))

    return server


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    build_server().run("stdio")


if __name__ == "__main__":
    main()
