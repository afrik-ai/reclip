"""OpenAPI 3.1 description of the agent API.

Served at /openapi.json so agent frameworks (OpenAI GPT Actions, LangChain
OpenAPI toolkits, n8n, etc.) can import ReClip as a tool without hand-written
glue.
"""

_ERROR = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Stable machine-readable code",
                    "enum": [
                        "invalid_url", "url_not_allowed", "invalid_format", "invalid_quality",
                        "invalid_request", "unauthorized", "not_found", "not_ready",
                        "too_many_jobs", "job_active", "invalid_signature",
                        "unsupported_url", "login_required", "geo_restricted", "age_restricted",
                        "rate_limited", "unavailable", "network_error", "timeout", "too_long",
                        "too_large", "download_failed", "interrupted", "internal_error",
                    ],
                },
                "message": {"type": "string"},
            },
            "required": ["code", "message"],
        }
    },
}

_JOB = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["queued", "downloading", "done", "error", "expired"]},
        "url": {"type": "string"},
        "format": {"type": "string", "enum": ["mp4", "mp3"]},
        "quality": {"type": "string", "description": "'best' or max height, e.g. '720'"},
        "title": {"type": ["string", "null"]},
        "progress": {"type": ["number", "null"], "description": "0-100"},
        "filename": {"type": ["string", "null"]},
        "size_bytes": {"type": ["integer", "null"]},
        "file_url": {
            "type": "string",
            "description": "Present when status is 'done'. Signed link; no API key needed. "
                           "Valid until expires_at.",
        },
        "created_at": {"type": ["string", "null"], "format": "date-time"},
        "finished_at": {"type": ["string", "null"], "format": "date-time"},
        "expires_at": {"type": ["string", "null"], "format": "date-time"},
        "cached": {"type": "boolean", "description": "True when an earlier identical download was reused"},
        "error": {"oneOf": [{"type": "null"}, _ERROR["properties"]["error"]]},
    },
}


def _err(desc):
    return {"description": desc, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def build_openapi(server_url, version):
    job_ref = {"$ref": "#/components/schemas/Download"}
    job_resp = {"content": {"application/json": {"schema": job_ref}}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "ReClip",
            "version": version,
            "description": (
                "Download video (MP4) or audio (MP3) from YouTube, TikTok, Instagram, X/Twitter "
                "and 1000+ other sites supported by yt-dlp. Typical flow: POST /api/v1/downloads "
                "with wait=60, then fetch file_url from the response. If status is still "
                "'queued' or 'downloading', call GET /api/v1/downloads/{id}?wait=60 until done."
            ),
        },
        "servers": [{"url": server_url}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Download": _JOB, "Error": _ERROR},
        },
        "paths": {
            "/api/v1/downloads": {
                "post": {
                    "operationId": "createDownload",
                    "summary": "Download a video or its audio from a URL",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["url"],
                            "properties": {
                                "url": {"type": "string", "description": "Page URL of the video/audio"},
                                "format": {"type": "string", "enum": ["mp4", "mp3"], "default": "mp4",
                                           "description": "mp4 = video, mp3 = audio only"},
                                "quality": {"type": "string", "default": "best",
                                            "description": "'best' or max video height: 2160, 1080, 720, 480, 360"},
                                "wait": {"type": "number", "default": 0,
                                         "description": "Seconds to wait for completion before responding (server caps this)"},
                                "reuse": {"type": "boolean", "default": True,
                                          "description": "Return an earlier identical download if its file still exists"},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Finished (done or error)", **job_resp},
                        "202": {"description": "Accepted; still running", **job_resp},
                        "400": _err("Invalid request"),
                        "401": _err("Missing or invalid API key"),
                        "403": _err("URL not allowed"),
                        "429": _err("Too many active downloads"),
                    },
                },
                "get": {
                    "operationId": "listDownloads",
                    "summary": "List your recent downloads",
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}}],
                    "responses": {"200": {"description": "Downloads", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"downloads": {"type": "array", "items": job_ref}}}}}}},
                },
            },
            "/api/v1/downloads/{id}": {
                "get": {
                    "operationId": "getDownload",
                    "summary": "Get a download's status, optionally waiting for it to finish",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "wait", "in": "query", "schema": {"type": "number", "default": 0},
                         "description": "Seconds to wait for completion"},
                    ],
                    "responses": {"200": {"description": "Download", **job_resp}, "404": _err("Not found")},
                },
                "delete": {
                    "operationId": "deleteDownload",
                    "summary": "Delete a finished download and its file",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"204": {"description": "Deleted"}, "404": _err("Not found"),
                                  "409": _err("Still running")},
                },
            },
            "/api/v1/downloads/{id}/file": {
                "get": {
                    "operationId": "getDownloadFile",
                    "summary": "Stream the downloaded file",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "The media file",
                                          "content": {"application/octet-stream": {}}},
                                  "409": _err("Not finished yet"), "410": _err("Expired or failed")},
                },
            },
            "/api/v1/info": {
                "post": {
                    "operationId": "getMediaInfo",
                    "summary": "Look up title, duration and available qualities without downloading",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {
                        "type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}}}},
                    "responses": {"200": {"description": "Media info", "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"}, "uploader": {"type": "string"},
                            "duration": {"type": ["number", "null"]}, "thumbnail": {"type": "string"},
                            "webpage_url": {"type": "string"}, "extractor": {"type": "string"},
                            "has_video": {"type": "boolean"},
                            "qualities": {"type": "array", "items": {"type": "string"}},
                        }}}}}, "422": _err("The site refused or the URL is unsupported")},
                },
            },
            "/api/v1/playlist": {
                "post": {
                    "operationId": "expandPlaylist",
                    "summary": "List the individual video URLs in a playlist or channel",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {
                        "type": "object", "required": ["url"],
                        "properties": {"url": {"type": "string"}, "limit": {"type": "integer",
                                                       "description": "Max items (server caps this, default 200)"}}}}}},
                    "responses": {"200": {"description": "Playlist items", "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "count": {"type": "integer"},
                                       "items": {"type": "array", "items": {"type": "object", "properties": {
                                           "url": {"type": "string"}, "title": {"type": "string"},
                                           "duration": {"type": ["number", "null"]}}}}}}}}}},
                },
            },
            "/api/v1/health": {
                "get": {"operationId": "health", "summary": "Service health", "security": [],
                        "responses": {"200": {"description": "OK"}}},
            },
        },
    }
