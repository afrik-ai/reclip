"""ReClip download engine.

Everything that actually touches yt-dlp lives here, so the web UI, the agent
REST API (app.py) and the MCP server (mcp_server.py) share one implementation:

* a persistent job store (SQLite) so jobs survive restarts and multiple threads
* a bounded worker pool so a burst of agent requests can't fork 100 yt-dlps
* URL validation that refuses private/internal addresses (SSRF guard)
* structured, machine-readable error codes instead of raw stderr
* automatic expiry of downloaded files
* optional transcripts of downloaded media (Whisper, see transcribe.py)
"""

import glob
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

FORMATS = {"mp4", "mp3"}
FORMAT_ALIASES = {"video": "mp4", "audio": "mp3", "mp4": "mp4", "mp3": "mp3"}
ACTIVE_STATUSES = ("queued", "downloading")
TRANSCRIPT_ACTIVE = ("pending", "queued", "transcribing")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass
class Settings:
    download_dir: str = field(
        default_factory=lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    )
    ytdlp_bin: str = "yt-dlp"
    max_concurrent: int = 3
    max_active_per_owner: int = 10
    file_ttl_seconds: int = 24 * 3600
    download_timeout: int = 600
    info_timeout: int = 60
    max_duration: int = 0  # seconds, 0 = unlimited
    max_filesize: str = ""  # yt-dlp syntax, e.g. "500M"; empty = unlimited
    max_wait: int = 120  # longest a client may block waiting for a job
    max_playlist_items: int = 200
    allow_private_urls: bool = False
    api_keys: tuple = ()
    secret: str = ""
    public_url: str = ""
    enable_ui: bool = True
    whisper_model: str = "turbo"
    whisper_device: str = "auto"  # auto | cuda | cpu
    whisper_compute_type: str = "auto"  # auto = float16 on GPU, int8 on CPU

    @classmethod
    def from_env(cls):
        s = cls()
        s.download_dir = os.environ.get("RECLIP_DOWNLOAD_DIR", s.download_dir)
        s.ytdlp_bin = os.environ.get("RECLIP_YTDLP", s.ytdlp_bin)
        s.max_concurrent = _env_int("RECLIP_MAX_CONCURRENT", s.max_concurrent)
        s.max_active_per_owner = _env_int("RECLIP_MAX_ACTIVE_PER_KEY", s.max_active_per_owner)
        # 0 keeps files forever.
        s.file_ttl_seconds = int(float(os.environ.get("RECLIP_FILE_TTL_HOURS", "24")) * 3600)
        s.download_timeout = _env_int("RECLIP_DOWNLOAD_TIMEOUT", s.download_timeout)
        s.max_duration = _env_int("RECLIP_MAX_DURATION", s.max_duration)
        s.max_filesize = os.environ.get("RECLIP_MAX_FILESIZE", s.max_filesize).strip()
        s.max_wait = _env_int("RECLIP_MAX_WAIT", s.max_wait)
        s.max_playlist_items = _env_int("RECLIP_MAX_PLAYLIST_ITEMS", s.max_playlist_items)
        s.allow_private_urls = _env_bool("RECLIP_ALLOW_PRIVATE_URLS", s.allow_private_urls)
        keys = os.environ.get("RECLIP_API_KEYS", "")
        s.api_keys = tuple(k.strip() for k in keys.split(",") if k.strip())
        s.secret = os.environ.get("RECLIP_SECRET", "")
        s.public_url = os.environ.get("RECLIP_PUBLIC_URL", "").rstrip("/")
        # The web UI has no login, so once API keys are set it would be an
        # unauthenticated side door. Keep it off unless explicitly enabled.
        s.enable_ui = _env_bool("RECLIP_ENABLE_UI", not s.api_keys)
        s.whisper_model = os.environ.get("RECLIP_WHISPER_MODEL", s.whisper_model).strip() or s.whisper_model
        s.whisper_device = os.environ.get("RECLIP_WHISPER_DEVICE", s.whisper_device).strip().lower() or "auto"
        s.whisper_compute_type = (os.environ.get("RECLIP_WHISPER_COMPUTE_TYPE", s.whisper_compute_type)
                                  .strip() or "auto")
        return s


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ReclipError(Exception):
    """An error with a stable machine-readable code, safe to show to clients."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self):
        return {"code": self.code, "message": self.message}


# Ordered: first match wins. Patterns are matched against yt-dlp's output.
_ERROR_PATTERNS = [
    ("unsupported_url", r"Unsupported URL|is not a valid URL|No video formats found"),
    ("login_required", r"[Pp]rivate video|[Ss]ign in|log ?in|[Ll]ogin required|cookies|authenticat|members[- ]only"),
    ("geo_restricted", r"available in your (country|region)|geo[- ]?restrict|from your location"),
    ("age_restricted", r"age[- ]restrict|confirm your age|inappropriate for some users"),
    ("rate_limited", r"HTTP Error 429|[Tt]oo [Mm]any [Rr]equests|rate[- ]limit"),
    ("unavailable", r"[Vv]ideo unavailable|has been removed|no longer available|does not exist|HTTP Error 404|HTTP Error 410|not found"),
    ("network_error", r"Unable to download webpage|urlopen error|Connection (refused|reset)|timed out|Name or service not known|Temporary failure in name resolution"),
]


def classify_ytdlp_error(output):
    """Turn yt-dlp's noisy output into (code, human message)."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    error_lines = [l for l in lines if l.startswith("ERROR:")]
    message = (error_lines[-1] if error_lines else (lines[-1] if lines else "yt-dlp failed"))
    message = re.sub(r"^ERROR:\s*", "", message)
    for code, pattern in _ERROR_PATTERNS:
        if re.search(pattern, message):
            return code, message
    return "download_failed", message


# --------------------------------------------------------------------------- #
# URL validation (SSRF guard)
# --------------------------------------------------------------------------- #

def _ip_is_public(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url, allow_private=False):
    """Return a cleaned URL or raise ReclipError.

    Without this, anyone who can reach the API could make the server fetch
    internal addresses (cloud metadata endpoints, admin panels on localhost)
    because yt-dlp's generic extractor will happily download any URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise ReclipError("invalid_url", "No URL provided")
    url = url.strip()
    if len(url) > 4096:
        raise ReclipError("invalid_url", "URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ReclipError("invalid_url", "Only http:// and https:// URLs are supported")
    host = parsed.hostname
    if not host:
        raise ReclipError("invalid_url", "URL has no host")
    if allow_private:
        return url
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        raise ReclipError("invalid_url", f"Could not resolve host '{host}'")
    for info in infos:
        if not _ip_is_public(info[4][0]):
            raise ReclipError(
                "url_not_allowed",
                "URLs pointing to private or local network addresses are not allowed",
                http_status=403,
            )
    return url


def normalize_format(fmt):
    value = FORMAT_ALIASES.get(str(fmt or "mp4").strip().lower())
    if not value:
        raise ReclipError("invalid_format", "format must be 'mp4' (video) or 'mp3' (audio)")
    return value


def normalize_quality(quality):
    """'best' or a max video height such as 1080 / '720p'."""
    if quality in (None, "", "best"):
        return "best"
    text = str(quality).strip().lower().rstrip("p")
    if text.isdigit() and 100 <= int(text) <= 8640:
        return text
    raise ReclipError("invalid_quality", "quality must be 'best' or a height such as 1080, 720, 480")


def normalize_language(language):
    """None/'auto' for auto-detect, else a Whisper language code such as 'en' or 'fr'."""
    if language in (None, "", "auto"):
        return None
    text = str(language).strip().lower()
    if re.fullmatch(r"[a-z]{2,3}", text):
        return text
    raise ReclipError("invalid_language", "language must be 'auto' or a code such as 'en', 'fr', 'es'")


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


# --------------------------------------------------------------------------- #
# Job store
# --------------------------------------------------------------------------- #

_COLUMNS = (
    "id", "owner", "url", "format", "quality", "format_id", "status", "progress",
    "title", "filename", "file_path", "size_bytes", "duration", "error_code",
    "error_message", "created_at", "started_at", "finished_at", "expires_at",
)


class JobStore:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT, url TEXT, format TEXT, quality TEXT,
                format_id TEXT, status TEXT, progress REAL, title TEXT, filename TEXT,
                file_path TEXT, size_bytes INTEGER, duration REAL, error_code TEXT,
                error_message TEXT, created_at REAL, started_at REAL, finished_at REAL,
                expires_at REAL)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_at)")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS transcripts (
                job_id TEXT PRIMARY KEY, status TEXT, progress REAL, language_requested TEXT,
                language TEXT, model TEXT, device TEXT, duration REAL, error_code TEXT,
                error_message TEXT, created_at REAL, started_at REAL, finished_at REAL)"""
        )

    def insert(self, job):
        cols = ",".join(_COLUMNS)
        marks = ",".join("?" for _ in _COLUMNS)
        with self._lock:
            self._db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", [job.get(c) for c in _COLUMNS])

    def update(self, job_id, **fields):
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id])

    def get(self, job_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_transcript(self, job_id):
        rows = self.query("SELECT * FROM transcripts WHERE job_id=?", (job_id,))
        return rows[0] if rows else None

    def put_transcript(self, row):
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO transcripts ({cols}) VALUES ({marks})", list(row.values()))

    def update_transcript(self, job_id, **fields):
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE transcripts SET {sets} WHERE job_id=?", [*fields.values(), job_id])

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self._lock:
            self._db.execute(sql, params)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

_PROGRESS_PREFIX = "[reclip-progress]"


class Engine:
    def __init__(self, settings=None, start_janitor=True, transcriber=None):
        self.settings = settings or Settings.from_env()
        os.makedirs(self.settings.download_dir, exist_ok=True)
        self.store = JobStore(os.path.join(self.settings.download_dir, "jobs.db"))
        self.secret = self.settings.secret or self._load_or_create_secret()
        self._pool = ThreadPoolExecutor(max_workers=max(1, self.settings.max_concurrent),
                                        thread_name_prefix="reclip-dl")
        # Transcription saturates a GPU (or every CPU core), so run one at a time.
        self._transcribe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reclip-stt")
        self._transcriber = transcriber
        self._transcript_lock = threading.Lock()
        self._changed = threading.Condition()
        self._submit_lock = threading.Lock()
        # Jobs that were running when the process died will never finish.
        self.store.execute(
            "UPDATE jobs SET status='error', error_code='interrupted', "
            "error_message='Server restarted before the download finished; please retry', "
            "finished_at=? WHERE status IN ('queued','downloading')",
            (time.time(),),
        )
        self.store.execute(
            "UPDATE transcripts SET status='error', error_code='interrupted', "
            "error_message='Server restarted before the transcript finished; please retry', "
            "finished_at=? WHERE status IN ('pending','queued','transcribing')",
            (time.time(),),
        )
        self.cleanup_expired()
        if start_janitor:
            t = threading.Thread(target=self._janitor, name="reclip-janitor", daemon=True)
            t.start()

    # -- helpers ----------------------------------------------------------- #

    def _load_or_create_secret(self):
        path = os.path.join(self.settings.download_dir, ".secret")
        try:
            with open(path) as f:
                value = f.read().strip()
                if value:
                    return value
        except FileNotFoundError:
            pass
        value = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        return value

    def _notify(self):
        with self._changed:
            self._changed.notify_all()

    def _ytdlp(self):
        return self.settings.ytdlp_bin

    def versions(self):
        try:
            ytdlp = subprocess.run([self._ytdlp(), "--version"], capture_output=True,
                                   text=True, timeout=15).stdout.strip() or None
        except Exception:
            ytdlp = None
        return {"yt_dlp": ytdlp, "ffmpeg": bool(shutil.which("ffmpeg")),
                "transcription": self.transcription_available()}

    # -- info / playlist --------------------------------------------------- #

    def get_info(self, url):
        url = validate_url(url, self.settings.allow_private_urls)
        cmd = [self._ytdlp(), "--no-playlist", "--no-warnings", "-j", url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.settings.info_timeout)
        except subprocess.TimeoutExpired:
            raise ReclipError("timeout", "Timed out fetching media info", http_status=504)
        if result.returncode != 0:
            code, message = classify_ytdlp_error(result.stderr)
            raise ReclipError(code, message, http_status=422)
        try:
            info = parse_ytdlp_json(result.stdout)
        except ValueError:
            raise ReclipError("download_failed", "yt-dlp returned no data", http_status=422)

        best_by_height = {}
        for f in info.get("formats") or []:
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f
        formats = [
            {"id": f["format_id"], "label": f"{h}p", "height": h}
            for h, f in sorted(best_by_height.items(), key=lambda kv: kv[0], reverse=True)
        ]
        return {
            "url": url,
            "title": info.get("title") or "",
            "uploader": info.get("uploader") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail") or "",
            "webpage_url": info.get("webpage_url") or url,
            "extractor": info.get("extractor_key") or info.get("extractor") or "",
            "has_video": bool(formats) or info.get("vcodec") not in (None, "none"),
            "formats": formats,
            "qualities": [str(f["height"]) for f in formats],
        }

    def expand_playlist(self, url, limit=None):
        url = validate_url(url, self.settings.allow_private_urls)
        limit = min(int(limit or self.settings.max_playlist_items), self.settings.max_playlist_items)
        cmd = [self._ytdlp(), "--flat-playlist", "--no-warnings", "-J",
               "--playlist-end", str(limit), url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.settings.info_timeout)
        except subprocess.TimeoutExpired:
            raise ReclipError("timeout", "Timed out fetching playlist info", http_status=504)
        if result.returncode != 0:
            code, message = classify_ytdlp_error(result.stderr)
            raise ReclipError(code, message, http_status=422)
        info = json.loads(result.stdout)
        entries = info.get("entries") or []
        items = []
        for e in entries[:limit]:
            entry_url = e.get("url") or e.get("webpage_url")
            if entry_url:
                items.append({"url": entry_url, "title": e.get("title") or "",
                              "duration": e.get("duration")})
        if not entries and info.get("webpage_url"):
            # Not a playlist: yt-dlp returns the single video itself.
            items.append({"url": info["webpage_url"], "title": info.get("title") or "",
                          "duration": info.get("duration")})
        return {"title": info.get("title") or "", "count": len(items), "items": items}

    # -- jobs -------------------------------------------------------------- #

    def submit(self, url, fmt="mp4", quality="best", owner="local", format_id=None, title=None,
               reuse=True, enforce_limit=True):
        url = validate_url(url, self.settings.allow_private_urls)
        fmt = normalize_format(fmt)
        quality = normalize_quality(quality)
        if format_id is not None and not re.fullmatch(r"[A-Za-z0-9_.\-+]{1,64}", str(format_id)):
            raise ReclipError("invalid_format", "Invalid format_id")

        with self._submit_lock:
            if reuse and not format_id:
                cached = self.store.query(
                    "SELECT * FROM jobs WHERE owner=? AND url=? AND format=? AND quality=? "
                    "AND format_id IS NULL AND status IN ('queued','downloading','done') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (owner, url, fmt, quality),
                )
                if cached and (cached[0]["status"] != "done" or
                               (cached[0]["file_path"] and os.path.exists(cached[0]["file_path"]))):
                    job = cached[0]
                    job["cached"] = True
                    return job

            active = self.store.query(
                "SELECT COUNT(*) AS n FROM jobs WHERE owner=? AND status IN ('queued','downloading')",
                (owner,),
            )[0]["n"]
            if enforce_limit and active >= self.settings.max_active_per_owner:
                raise ReclipError(
                    "too_many_jobs",
                    f"You already have {active} downloads in progress; wait for some to finish",
                    http_status=429,
                )

            now = time.time()
            job = {c: None for c in _COLUMNS}
            job.update(id=uuid.uuid4().hex[:16], owner=owner, url=url, format=fmt, quality=quality,
                       format_id=format_id, status="queued", progress=0.0, title=title or None,
                       created_at=now)
            self.store.insert(job)
        self._pool.submit(self._run, job["id"])
        self._notify()
        return self.store.get(job["id"])

    def get(self, job_id, owner=None):
        job = self.store.get(job_id) if job_id else None
        if not job or (owner is not None and job["owner"] != owner):
            raise ReclipError("not_found", "No such download", http_status=404)
        return job

    def wait(self, job_id, timeout, owner=None):
        """Block until the job leaves queued/downloading or timeout passes."""
        deadline = time.time() + max(0, min(float(timeout or 0), self.settings.max_wait))
        job = self.get(job_id, owner)
        while job["status"] in ACTIVE_STATUSES:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            with self._changed:
                self._changed.wait(timeout=min(remaining, 1.0))
            job = self.get(job_id, owner)
        return job

    def list(self, owner=None, limit=50):
        limit = max(1, min(int(limit), 500))
        if owner is None:
            return self.store.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return self.store.query(
            "SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, limit)
        )

    def delete(self, job_id, owner=None):
        job = self.get(job_id, owner)
        if job["status"] in ACTIVE_STATUSES:
            raise ReclipError("job_active", "Download is still running", http_status=409)
        transcript = self.store.get_transcript(job_id)
        if transcript and transcript["status"] in TRANSCRIPT_ACTIVE:
            raise ReclipError("job_active", "Transcript is still running", http_status=409)
        self._remove_files(job_id)
        self.store.execute("DELETE FROM transcripts WHERE job_id=?", (job_id,))
        self.store.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # -- transcripts ------------------------------------------------------- #

    def transcription_available(self):
        if self._transcriber is not None:
            return True
        import transcribe

        return transcribe.is_available()

    def _get_transcriber(self):
        if self._transcriber is None:
            import transcribe

            s = self.settings
            self._transcriber = transcribe.Transcriber(s.whisper_model, s.whisper_device,
                                                       s.whisper_compute_type)
        return self._transcriber

    def request_transcript(self, job_id, owner=None, language=None, reuse=True):
        """Start (or reuse) a transcript of a download.

        Works on a download that is still running too: the transcript waits as
        "pending" and starts as soon as the file is ready.
        """
        language = normalize_language(language)
        if not self.transcription_available():
            raise ReclipError(
                "transcription_unavailable",
                "Transcription isn't installed on this server "
                "(pip install -r requirements-transcribe.txt)",
                http_status=501,
            )
        with self._transcript_lock:
            job = self.get(job_id, owner)
            if job["status"] not in ("done", *ACTIVE_STATUSES):
                raise ReclipError("not_ready", f"Download is not available (status: {job['status']})",
                                  http_status=410 if job["status"] == "expired" else 409)
            existing = self.store.get_transcript(job_id)
            if existing:
                same_language = language in (None, existing["language_requested"], existing["language"])
                if existing["status"] in TRANSCRIPT_ACTIVE:
                    if not same_language:
                        raise ReclipError("job_active", "A transcript in another language is still running",
                                          http_status=409)
                    return existing
                if reuse and existing["status"] == "done" and same_language and \
                        os.path.exists(self.transcript_paths(job_id)["txt"]):
                    return existing
            now = time.time()
            ready = job["status"] == "done"
            self.store.put_transcript({
                "job_id": job_id, "status": "queued" if ready else "pending", "progress": 0.0,
                "language_requested": language, "language": None, "model": None, "device": None,
                "duration": None, "error_code": None, "error_message": None, "created_at": now,
                "started_at": None, "finished_at": None,
            })
            if ready:
                self._transcribe_pool.submit(self._run_transcript, job_id)
        self._notify()
        return self.store.get_transcript(job_id)

    def get_transcript(self, job_id, owner=None):
        self.get(job_id, owner)  # ownership check
        transcript = self.store.get_transcript(job_id)
        if not transcript:
            raise ReclipError("not_found", "No transcript for this download; request one first",
                              http_status=404)
        return transcript

    def wait_transcript(self, job_id, timeout, owner=None):
        deadline = time.time() + max(0, min(float(timeout or 0), self.settings.max_wait))
        transcript = self.get_transcript(job_id, owner)
        while transcript["status"] in TRANSCRIPT_ACTIVE:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            with self._changed:
                self._changed.wait(timeout=min(remaining, 1.0))
            transcript = self.get_transcript(job_id, owner)
        return transcript

    def transcript_paths(self, job_id):
        import transcribe

        return {fmt: os.path.join(self._job_dir(job_id), f"transcript.{fmt}")
                for fmt in transcribe.TRANSCRIPT_FORMATS}

    def read_transcript_text(self, job_id):
        try:
            with open(self.transcript_paths(job_id)["txt"], encoding="utf-8") as f:
                return f.read().rstrip("\n")
        except OSError:
            return None

    def _download_finished(self, job_id):
        """Start or fail any transcript that was waiting on this download."""
        with self._transcript_lock:
            transcript = self.store.get_transcript(job_id)
            if not transcript or transcript["status"] != "pending":
                return
            job = self.store.get(job_id)
            if job and job["status"] == "done":
                self.store.update_transcript(job_id, status="queued")
                self._transcribe_pool.submit(self._run_transcript, job_id)
            else:
                # Pass the download's own error on: "login_required" says more than "failed".
                self.store.update_transcript(
                    job_id, status="error", error_code=(job or {}).get("error_code") or "download_failed",
                    error_message="The download failed, so there is nothing to transcribe: "
                                  + ((job or {}).get("error_message") or "unknown error"),
                    finished_at=time.time())

    def _run_transcript(self, job_id):
        import transcribe

        transcript = self.store.get_transcript(job_id)
        job = self.store.get(job_id)
        if not transcript or transcript["status"] != "queued":
            return
        fields = {}
        try:
            if not job or job["status"] != "done" or not job["file_path"] or \
                    not os.path.exists(job["file_path"]):
                raise ReclipError("not_ready", "The downloaded file is no longer available")
            transcriber = self._get_transcriber()
            self.store.update_transcript(job_id, status="transcribing", started_at=time.time(),
                                         model=getattr(transcriber, "model_name", None))
            self._notify()
            last = [0.0]

            def on_progress(pct):
                if time.time() - last[0] > 0.5:
                    last[0] = time.time()
                    self.store.update_transcript(job_id, progress=pct)
                    self._notify()

            result = transcriber.transcribe(job["file_path"], transcript["language_requested"], on_progress)
            transcribe.write_outputs(result, self._job_dir(job_id))
            fields = {"status": "done", "progress": 100.0, "language": result["language"],
                      "duration": result["duration"], "device": getattr(transcriber, "device", None)}
        except ReclipError as e:
            fields = {"status": "error", "error_code": e.code, "error_message": e.message}
        except Exception as e:  # never leave a transcript stuck in "transcribing"
            fields = {"status": "error", "error_code": "transcription_failed", "error_message": str(e)}
        fields["finished_at"] = time.time()
        self.store.update_transcript(job_id, **fields)
        self._notify()

    # -- signed file links ------------------------------------------------- #

    def sign(self, job_id, expires_at):
        msg = f"{job_id}:{int(expires_at)}".encode()
        return hashlib.blake2b(msg, key=self.secret.encode()[:64], digest_size=16).hexdigest()

    def verify_signature(self, job_id, expires, signature):
        try:
            expires = int(expires)
        except (TypeError, ValueError):
            return False
        if expires < time.time():
            return False
        return secrets.compare_digest(self.sign(job_id, expires), str(signature or ""))

    # -- worker ------------------------------------------------------------ #

    def _job_dir(self, job_id):
        return os.path.join(self.settings.download_dir, job_id)

    def _remove_files(self, job_id):
        shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
        for f in glob.glob(os.path.join(self.settings.download_dir, f"{job_id}.*")):
            try:
                os.remove(f)
            except OSError:
                pass

    def build_command(self, job, out_dir):
        s = self.settings
        cmd = [
            self._ytdlp(), "--no-playlist", "--no-warnings", "--newline", "--windows-filenames",
            # --print implies --quiet/--simulate; undo both but keep progress lines.
            "--no-simulate", "--progress",
            "--trim-filenames", "150",
            "--progress-template",
            f"download:{_PROGRESS_PREFIX} %(progress.downloaded_bytes)s "
            f"%(progress.total_bytes)s %(progress.total_bytes_estimate)s",
            "--print", "after_move:[reclip-file] %(filepath)s",
            "--print", "before_dl:[reclip-meta] %(title)j",
            "-o", os.path.join(out_dir, "%(title).150B.%(ext)s"),
        ]
        if s.max_duration:
            cmd += ["--match-filter", f"!duration | duration <= {int(s.max_duration)}"]
        if s.max_filesize:
            cmd += ["--max-filesize", s.max_filesize]
        if job["format"] == "mp3":
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        elif job["format_id"]:
            cmd += ["-f", f"{job['format_id']}+bestaudio/best", "--merge-output-format", "mp4"]
        elif job["quality"] and job["quality"] != "best":
            h = int(job["quality"])
            cmd += ["-f", f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best",
                    "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
        cmd += ["--", job["url"]]
        return cmd

    def _run(self, job_id):
        job = self.store.get(job_id)
        if not job or job["status"] != "queued":
            return
        out_dir = self._job_dir(job_id)
        os.makedirs(out_dir, exist_ok=True)
        self.store.update(job_id, status="downloading", started_at=time.time())
        self._notify()

        try:
            fields = self._download(job, out_dir)
        except ReclipError as e:
            self._remove_files(job_id)
            fields = {"status": "error", "error_code": e.code, "error_message": e.message}
        except Exception as e:  # never leave a job stuck in "downloading"
            self._remove_files(job_id)
            fields = {"status": "error", "error_code": "internal_error", "error_message": str(e)}
        fields["finished_at"] = time.time()
        self.store.update(job_id, **fields)
        self._download_finished(job_id)
        self._notify()

    def _download(self, job, out_dir):
        cmd = self.build_command(job, out_dir)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        timed_out = threading.Event()

        def _kill():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(self.settings.download_timeout, _kill)
        timer.daemon = True
        timer.start()
        tail, final_paths, last_progress_write = [], [], 0.0
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith(_PROGRESS_PREFIX):
                    pct = _parse_progress(line[len(_PROGRESS_PREFIX):])
                    if pct is not None and time.time() - last_progress_write > 0.5:
                        last_progress_write = time.time()
                        self.store.update(job["id"], progress=pct)
                        self._notify()
                    continue
                if line.startswith("[reclip-file] "):
                    final_paths.append(line[len("[reclip-file] "):].strip())
                    continue
                if line.startswith("[reclip-meta] ") and not job.get("title"):
                    try:
                        title = json.loads(line[len("[reclip-meta] "):])
                        if isinstance(title, str):
                            job["title"] = title
                            self.store.update(job["id"], title=title)
                    except ValueError:
                        pass
                    continue
                tail.append(line)
                del tail[:-50]
            proc.wait()
        finally:
            timer.cancel()

        output = "\n".join(tail)
        if timed_out.is_set():
            raise ReclipError("timeout", f"Download took longer than {self.settings.download_timeout}s")
        if proc.returncode != 0:
            raise ReclipError(*classify_ytdlp_error(output))

        want = ".mp3" if job["format"] == "mp3" else ".mp4"
        files = [p for p in final_paths if os.path.isfile(p)]
        if not files:
            files = [p for p in glob.glob(os.path.join(out_dir, "*"))
                     if os.path.isfile(p) and not p.endswith((".part", ".ytdl"))]
        if not files:
            if "does not pass filter" in output:
                raise ReclipError("too_long",
                                  f"Media is longer than the {self.settings.max_duration}s limit")
            if "max-filesize" in output or "larger than max" in output.lower():
                raise ReclipError("too_large",
                                  f"Media is larger than the {self.settings.max_filesize} limit")
            raise ReclipError("download_failed", "Download finished but no file was produced")

        matching = [f for f in files if f.lower().endswith(want)]
        chosen = os.path.abspath(matching[0] if matching else files[0])
        for f in glob.glob(os.path.join(out_dir, "*")):
            if os.path.abspath(f) != chosen and not os.path.basename(f).startswith("transcript."):
                try:
                    os.remove(f)
                except OSError:
                    pass
        now = time.time()
        return {
            "status": "done",
            "progress": 100.0,
            "file_path": chosen,
            "filename": os.path.basename(chosen),
            "size_bytes": os.path.getsize(chosen),
            "expires_at": (now + self.settings.file_ttl_seconds) if self.settings.file_ttl_seconds > 0 else None,
        }

    # -- housekeeping ------------------------------------------------------ #

    def cleanup_expired(self):
        now = time.time()
        expired = self.store.query(
            "SELECT id FROM jobs WHERE status='done' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        for row in expired:
            self._remove_files(row["id"])
            self.store.update(row["id"], status="expired", file_path=None)
            self.store.execute("DELETE FROM transcripts WHERE job_id=?", (row["id"],))
        # Forget failed/expired job records after a week.
        self.store.execute(
            "DELETE FROM jobs WHERE status IN ('error','expired') AND created_at < ?",
            (now - 7 * 24 * 3600,),
        )
        self.store.execute("DELETE FROM transcripts WHERE job_id NOT IN (SELECT id FROM jobs)")
        return len(expired)

    def _janitor(self):
        while True:
            time.sleep(300)
            try:
                self.cleanup_expired()
            except Exception:
                pass


def _parse_progress(text):
    parts = text.split()
    if len(parts) != 3:
        return None

    def num(v):
        try:
            return float(v)
        except ValueError:
            return None

    done, total, estimate = (num(p) for p in parts)
    total = total or estimate
    if not done or not total:
        return None
    return round(min(99.0, done * 100.0 / total), 1)


def public_job(job, file_url=None):
    """The job shape returned to API and MCP clients."""
    out = {
        "id": job["id"],
        "status": job["status"],
        "url": job["url"],
        "format": job["format"],
        "quality": job["quality"],
        "title": job["title"],
        "progress": job["progress"],
        "filename": job["filename"],
        "size_bytes": job["size_bytes"],
        "created_at": _iso(job["created_at"]),
        "finished_at": _iso(job["finished_at"]),
        "expires_at": _iso(job["expires_at"]),
        "error": ({"code": job["error_code"], "message": job["error_message"]}
                  if job["status"] == "error" else None),
    }
    if file_url and job["status"] == "done":
        out["file_url"] = file_url
    if job.get("cached"):
        out["cached"] = True
    return out


def public_transcript(transcript, text=None, files=None, max_chars=None):
    """The transcript shape returned to API and MCP clients."""
    out = {
        "download_id": transcript["job_id"],
        "status": transcript["status"],
        "progress": transcript["progress"],
        "language": transcript["language"] or transcript["language_requested"],
        "duration": transcript["duration"],
        "model": transcript["model"],
        "created_at": _iso(transcript["created_at"]),
        "finished_at": _iso(transcript["finished_at"]),
        "error": ({"code": transcript["error_code"], "message": transcript["error_message"]}
                  if transcript["status"] == "error" else None),
    }
    if transcript["status"] == "done":
        if text is not None:
            if max_chars and len(text) > max_chars:
                out["text"] = text[:max_chars]
                out["text_truncated"] = True
            else:
                out["text"] = text
        if files:
            out["files"] = files
    return out


def _iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
