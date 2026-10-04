# Using ReClip from AI agents

ReClip exposes the same download engine three ways:

| Interface | Best for | Returns |
|---|---|---|
| **MCP server** (`mcp_server.py`) | Claude Desktop, Claude Code, Cursor, any MCP client | `file_path` on your machine, or `file_url` in remote mode |
| **REST API** (`/api/v1/...`) | Any agent framework, scripts, n8n, GPT Actions | JSON job + signed `file_url` |
| **OpenAPI spec** (`/openapi.json`) | Frameworks that import tools from a spec | Same as REST |

## 1. MCP server

```bash
pip install -r requirements.txt -r requirements-mcp.txt   # also needs ffmpeg on PATH
```

Claude Desktop (`claude_desktop_config.json`) or any MCP client:

```json
{
  "mcpServers": {
    "reclip": {
      "command": "python",
      "args": ["/absolute/path/to/reclip/mcp_server.py"]
    }
  }
}
```

Claude Code: `claude mcp add reclip -- python /absolute/path/to/reclip/mcp_server.py`

Files are saved under `~/Downloads/ReClip/<job-id>/` (override with `RECLIP_DOWNLOAD_DIR`)
and are never auto-deleted in this mode.

To use a ReClip server you host elsewhere instead, add to the server's `env`:

```json
"env": { "RECLIP_API_URL": "https://reclip.example.com", "RECLIP_API_KEY": "<key>" }
```

### Tools

| Tool | What it does |
|---|---|
| `download_media(url, format="mp4"\|"mp3", quality="best"\|"1080"\|"720"…, wait_seconds=90)` | Downloads and waits; returns status, title, size and `file_path`/`file_url` |
| `get_download(id, wait_seconds=60)` | Checks, or keeps waiting on, a download |
| `get_media_info(url)` | Title, uploader, duration, available qualities; no download |
| `expand_playlist(url, limit=25)` | Individual video URLs in a playlist or channel |
| `list_downloads(limit=20)` | Recent downloads |

## 2. REST API

Start the server with keys:

```bash
RECLIP_API_KEYS=$(openssl rand -hex 24) docker compose up -d
```

One call is usually enough: ask the server to wait for the download.

```bash
curl -s -X POST http://localhost:8899/api/v1/downloads \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "format": "mp3", "wait": 60}'
```

```json
{
  "id": "7db7b59fc8294dfa",
  "status": "done",
  "format": "mp3",
  "quality": "best",
  "title": "Me at the zoo",
  "progress": 100.0,
  "filename": "Me at the zoo.mp3",
  "size_bytes": 312345,
  "expires_at": "2026-10-05T03:26:02Z",
  "error": null,
  "file_url": "https://reclip.example.com/files/7db7b59fc8294dfa?expires=1791170762&sig=2c67…"
}
```

`file_url` is a signed link that works **without** the API key until `expires_at`, so an agent can
hand it to another tool or to a person.

If the response comes back with `status` `queued` or `downloading` (HTTP 202), keep waiting with
`GET /api/v1/downloads/{id}?wait=60`.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/downloads` | Start a download. Body: `url`, `format` (`mp4`/`mp3`), `quality` (`best` or max height), `wait` (seconds), `reuse` (default `true`) |
| `GET /api/v1/downloads/{id}?wait=N` | Status, optionally waiting |
| `GET /api/v1/downloads/{id}/file` | Stream the file (needs the key) |
| `DELETE /api/v1/downloads/{id}` | Delete the file and record |
| `GET /api/v1/downloads?limit=N` | Your downloads, newest first |
| `POST /api/v1/info` | Metadata only |
| `POST /api/v1/playlist` | Expand a playlist/channel into video URLs |
| `GET /api/v1/health` | Health and versions (no key needed) |
| `GET /openapi.json` | OpenAPI 3.1 spec (no key needed) |

Each API key only sees its own downloads. Asking for the same URL/format/quality again returns the
existing file (`"cached": true`) instead of downloading twice.

### Errors

Every error has the shape `{"error": {"code": "...", "message": "..."}}`. A failed download returns
`status: "error"` with the same `error` object. Codes an agent should act on:

| Code | Meaning | Retry? |
|---|---|---|
| `unsupported_url` | yt-dlp doesn't know this site/page | No |
| `login_required` | Private, members-only or needs sign-in | No |
| `geo_restricted` / `age_restricted` | Blocked for the server's location/account | No |
| `unavailable` | Removed, deleted or 404 | No |
| `too_long` / `too_large` | Over the server's duration/size limit | No |
| `rate_limited` | The site is throttling the server | Later |
| `network_error` / `timeout` / `interrupted` | Transient | Yes |
| `too_many_jobs` (HTTP 429) | Your key has too many downloads running | After one finishes |
| `invalid_url` / `url_not_allowed` / `invalid_format` / `invalid_quality` | Fix the request | No |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RECLIP_API_KEYS` | (none) | Comma-separated keys. When unset the API is open; only do that on localhost |
| `RECLIP_ENABLE_UI` | on without keys, off with keys | The web UI has no login, so it's off by default once keys are set |
| `RECLIP_PUBLIC_URL` | request host | Base URL used in `file_url` (set this behind a proxy) |
| `RECLIP_DOWNLOAD_DIR` | `./downloads` | Where files and the job database live |
| `RECLIP_FILE_TTL_HOURS` | `24` | Delete files after this long; `0` keeps them |
| `RECLIP_MAX_CONCURRENT` | `3` | yt-dlp processes running at once (others queue) |
| `RECLIP_MAX_ACTIVE_PER_KEY` | `10` | Queued + running downloads allowed per key |
| `RECLIP_MAX_WAIT` | `120` | Longest a request may block with `wait` |
| `RECLIP_DOWNLOAD_TIMEOUT` | `600` | Kill a single download after this many seconds |
| `RECLIP_MAX_DURATION` | `0` | Refuse media longer than this many seconds (`0` = no limit) |
| `RECLIP_MAX_FILESIZE` | (none) | Refuse files larger than this, yt-dlp syntax e.g. `500M` |
| `RECLIP_MAX_PLAYLIST_ITEMS` | `200` | Cap for playlist expansion |
| `RECLIP_ALLOW_PRIVATE_URLS` | `0` | Allow URLs on private/local networks (off to prevent SSRF) |
| `RECLIP_SECRET` | random, saved in the download dir | Key for signing `file_url` links |

## Security notes

- Put the server behind HTTPS if it's reachable from the internet; API keys travel in a header.
- URLs that resolve to private, loopback or link-local addresses are refused. yt-dlp still follows
  redirects on its own, so for a public deployment also restrict outbound traffic at the network level.
- Respect copyright and each platform's terms of service.
