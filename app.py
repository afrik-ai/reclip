import hashlib
import os
import secrets
import sys

from flask import Flask, abort, jsonify, render_template, request, send_file

from core import Engine, ReclipError, Settings, public_job
from openapi import build_openapi

API_VERSION = "1.0.0"


def create_app(settings=None, engine=None):
    settings = settings or (engine.settings if engine else Settings.from_env())
    engine = engine or Engine(settings)
    app = Flask(__name__)
    app.config["RECLIP_ENGINE"] = engine
    app.json.sort_keys = False

    key_owners = {k: "key_" + hashlib.sha256(k.encode()).hexdigest()[:12] for k in settings.api_keys}

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def api_error(code, message, status):
        return jsonify({"error": {"code": code, "message": message}}), status

    @app.errorhandler(ReclipError)
    def handle_reclip_error(e):
        if request.path.startswith("/api/v1/") or request.path.startswith("/files/"):
            return api_error(e.code, e.message, e.http_status)
        # The bundled web UI expects {"error": "<message>"}.
        return jsonify({"error": e.message}), e.http_status

    def current_owner():
        """Resolve the caller from its API key. Returns the owner id."""
        if not key_owners:
            return "local"
        token = request.headers.get("X-API-Key", "")
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        for key, owner in key_owners.items():
            if token and secrets.compare_digest(token.encode(), key.encode()):
                return owner
        raise ReclipError("unauthorized",
                          "Missing or invalid API key (send 'Authorization: Bearer <key>')",
                          http_status=401)

    def base_url():
        return settings.public_url or request.host_url.rstrip("/")

    def file_url(job):
        if job["status"] != "done":
            return None
        # Files kept forever (TTL 0) still get links that expire after a year.
        exp = int(job["expires_at"] or (job["finished_at"] or 0) + 365 * 24 * 3600)
        return f"{base_url()}/files/{job['id']}?expires={exp}&sig={engine.sign(job['id'], exp)}"

    def job_json(job):
        return public_job(job, file_url(job))

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def wait_seconds(value):
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            raise ReclipError("invalid_request", "wait must be a number of seconds")

    def send_job_file(job):
        path = job.get("file_path")
        if job["status"] != "done" or not path or not os.path.exists(path):
            raise ReclipError("not_ready", f"File is not available (status: {job['status']})",
                              http_status=409 if job["status"] in ("queued", "downloading") else 410)
        return send_file(path, as_attachment=True, download_name=job["filename"])

    # ------------------------------------------------------------------ #
    # Agent API (v1)
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/health")
    def v1_health():
        return jsonify({"ok": True, "version": API_VERSION, "auth_required": bool(key_owners),
                        **engine.versions()})

    @app.get("/openapi.json")
    @app.get("/api/v1/openapi.json")
    def v1_openapi():
        return jsonify(build_openapi(base_url(), API_VERSION))

    @app.post("/api/v1/info")
    def v1_info():
        current_owner()
        return jsonify(engine.get_info(body().get("url")))

    @app.post("/api/v1/playlist")
    def v1_playlist():
        current_owner()
        data = body()
        return jsonify(engine.expand_playlist(data.get("url"), data.get("limit")))

    @app.post("/api/v1/downloads")
    def v1_create_download():
        owner = current_owner()
        data = body()
        wait = wait_seconds(data.get("wait", request.args.get("wait")))
        job = engine.submit(
            data.get("url"),
            fmt=data.get("format", "mp4"),
            quality=data.get("quality", "best"),
            owner=owner,
            reuse=data.get("reuse", True) is not False,
        )
        if wait:
            job = {**engine.wait(job["id"], wait, owner), "cached": job.get("cached")}
        status = 200 if job["status"] in ("done", "error") else 202
        return jsonify(job_json(job)), status

    @app.get("/api/v1/downloads")
    def v1_list_downloads():
        owner = current_owner()
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            raise ReclipError("invalid_request", "limit must be an integer")
        return jsonify({"downloads": [job_json(j) for j in engine.list(owner, limit)]})

    @app.get("/api/v1/downloads/<job_id>")
    def v1_get_download(job_id):
        owner = current_owner()
        wait = wait_seconds(request.args.get("wait"))
        job = engine.wait(job_id, wait, owner) if wait else engine.get(job_id, owner)
        return jsonify(job_json(job))

    @app.get("/api/v1/downloads/<job_id>/file")
    def v1_download_file(job_id):
        return send_job_file(engine.get(job_id, current_owner()))

    @app.delete("/api/v1/downloads/<job_id>")
    def v1_delete_download(job_id):
        engine.delete(job_id, current_owner())
        return "", 204

    @app.get("/files/<job_id>")
    def signed_file(job_id):
        if not engine.verify_signature(job_id, request.args.get("expires"), request.args.get("sig")):
            raise ReclipError("invalid_signature", "This link is invalid or has expired", http_status=403)
        return send_job_file(engine.get(job_id))

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/v1/"):
            return api_error("not_found", "No such endpoint", 404)
        return e

    @app.errorhandler(405)
    def method_not_allowed(e):
        if request.path.startswith("/api/v1/"):
            return api_error("method_not_allowed", "Method not allowed", 405)
        return e

    # ------------------------------------------------------------------ #
    # Web UI + the endpoints it calls (unchanged contract)
    # ------------------------------------------------------------------ #

    if settings.enable_ui:
        @app.route("/")
        def index():
            return render_template("index.html")

        @app.post("/api/info")
        def get_info():
            info = engine.get_info(body().get("url", ""))
            return jsonify({k: info[k] for k in ("title", "thumbnail", "duration", "uploader", "formats")})

        @app.post("/api/playlist")
        def get_playlist_info():
            result = engine.expand_playlist(body().get("url", ""))
            return jsonify({"urls": [item["url"] for item in result["items"]]})

        @app.post("/api/download")
        def start_download():
            data = body()
            job = engine.submit(
                data.get("url", ""),
                fmt=data.get("format", "video"),
                owner="ui",
                format_id=data.get("format_id") or None,
                title=data.get("title") or None,
                reuse=False,
                enforce_limit=False,
            )
            return jsonify({"job_id": job["id"]})

        @app.get("/api/status/<job_id>")
        def check_status(job_id):
            job = engine.get(job_id, "ui")
            status = {"queued": "downloading", "expired": "error"}.get(job["status"], job["status"])
            error = job["error_message"] if job["status"] == "error" else (
                "File expired" if job["status"] == "expired" else None)
            return jsonify({"status": status, "error": error, "filename": job["filename"],
                            "progress": job["progress"]})

        @app.get("/api/file/<job_id>")
        def download_file(job_id):
            try:
                return send_job_file(engine.get(job_id, "ui"))
            except ReclipError:
                return jsonify({"error": "File not ready"}), 404

    return app


def __getattr__(name):
    # `gunicorn app:app` keeps working, but importing this module (tests, the
    # MCP server) doesn't spin up a download engine as a side effect.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    application = create_app()
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("RECLIP_API_KEYS"):
        print("WARNING: ReClip is listening on a public interface without RECLIP_API_KEYS; "
              "anyone who can reach it can use it.", file=sys.stderr)
    application.run(host=host, port=port, threaded=True)
