import json
import logging
import os
import secrets
import threading
from datetime import UTC

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from clients.plaud_client import PlaudClient
from config import DEFAULTS, load_config, save_config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

HISTORY_FILE = os.environ.get("HISTORY_FILE", "/app/config/history.json")
PLAUD_STATUS_FILE = os.environ.get("PLAUD_STATUS_FILE", "/app/config/plaud_status.json")
MAX_HISTORY = 50

# One-at-a-time processing lock
_processing_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))


# --- History helpers ---


def _load_history():
    """Load processing history from JSON file."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def add_history_entry(filename, status, message=""):
    """Append a processing event to history (called from pipeline)."""
    from datetime import datetime

    history = _load_history()
    history.insert(
        0,
        {
            "filename": filename,
            "status": status,
            "message": message,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        },
    )
    history = history[:MAX_HISTORY]

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# --- Plaud status helpers ---


def _load_plaud_status():
    """Load Plaud connection status."""
    if not os.path.exists(PLAUD_STATUS_FILE):
        return None
    try:
        with open(PLAUD_STATUS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_plaud_status(ok, message):
    """Write Plaud connection status for display on settings page."""
    from datetime import datetime

    os.makedirs(os.path.dirname(PLAUD_STATUS_FILE), exist_ok=True)
    with open(PLAUD_STATUS_FILE, "w") as f:
        json.dump(
            {
                "ok": ok,
                "message": message,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
            f,
        )


def _test_plaud_token(token, base_url):
    """Test a Plaud token and write the result to the status file."""
    if not token:
        _write_plaud_status(False, "No Plaud token configured")
        return
    client = PlaudClient(token=token, base_url=base_url)
    result = client.test_connection()
    _write_plaud_status(result["ok"], result["message"])


# --- Settings page ---


@app.route("/")
def settings_page():
    config = load_config()
    # Mask secrets for display
    key = config.get("anthropic_api_key", "")
    masked_key = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ("Set" if key else "Not set")
    token = config.get("plaud_token", "")
    masked_token = f"{token[:12]}...{token[-4:]}" if len(token) > 16 else ("Set" if token else "Not set")
    plaud_status = _load_plaud_status()
    gdrive_connected = config.get("gdrive_enabled", False) and config.get("gdrive_refresh_token", "")
    return render_template(
        "settings.html",
        config=config,
        masked_key=masked_key,
        masked_token=masked_token,
        plaud_status=plaud_status,
        plaud_configured=bool(token),
        anthropic_configured=bool(key),
        gdrive_connected=gdrive_connected,
        default_prompt=DEFAULTS["claude_prompt"],
    )


@app.route("/settings", methods=["POST"])
def save_settings():
    data = {}

    current = load_config()

    # Only update secrets if a new value was provided
    new_key = request.form.get("anthropic_api_key", "").strip()
    data["anthropic_api_key"] = new_key if new_key else current.get("anthropic_api_key", "")

    new_token = request.form.get("plaud_token", "").strip()
    data["plaud_token"] = new_token if new_token else current.get("plaud_token", "")

    data["output_dir"] = current.get("output_dir", "/tmp/claudioscribe")
    data["log_level"] = request.form.get("log_level", "INFO").strip()
    data["claude_model"] = request.form.get("claude_model", "").strip() or current.get(
        "claude_model", "claude-sonnet-4-6"
    )
    data["claude_prompt"] = request.form.get("claude_prompt", "").strip()
    data["plaud_base_url"] = request.form.get("plaud_base_url", "https://api.plaud.ai").strip()
    data["plaud_poll_interval"] = current.get("plaud_poll_interval", 60)

    # Preserve Google Drive settings
    for gdrive_key in (
        "gdrive_enabled",
        "gdrive_access_token",
        "gdrive_refresh_token",
        "gdrive_token_expiry",
        "gdrive_folder_id",
        "gdrive_documents_folder_id",
        "gdrive_recordings_folder_id",
    ):
        data[gdrive_key] = current.get(gdrive_key, "")

    save_config(data)
    logger.info("Settings saved via web UI")

    # Test Plaud connection immediately so the status banner updates on redirect
    _test_plaud_token(data["plaud_token"], data["plaud_base_url"])

    flash("Settings saved successfully.", "success")
    return redirect(url_for("settings_page"))


# --- Plaud Recordings routes ---


@app.route("/recordings")
def recordings_list():
    """Return JSON list of Plaud recordings with pipeline status."""
    from pipeline import ARCHIVE_DIR, _sanitize_filename, load_pipeline_status

    config = load_config()
    token = config.get("plaud_token", "")
    if not token:
        return jsonify([])

    client = PlaudClient(
        token=token,
        base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
    )
    recordings = client.list_recordings()
    pipeline_status = load_pipeline_status()

    result = []
    for rec in recordings:
        file_id = rec.get("id", "")
        if not file_id:
            continue
        status_entry = pipeline_status.get(file_id, {})
        # Duration from Plaud API is in milliseconds
        duration_ms = rec.get("duration", 0)
        duration_str = ""
        if duration_ms:
            total_secs = duration_ms // 1000
            mins, secs = divmod(total_secs, 60)
            duration_str = f"{mins}:{secs:02d}"
        # Check if file exists locally (download dir or archive)
        raw_name = rec.get("filename", rec.get("name", f"{file_id}.mp3"))
        safe_name = _sanitize_filename(raw_name)
        if not safe_name.lower().endswith((".mp3", ".ogg", ".m4a", ".wav", ".flac")):
            safe_name += ".mp3"
        local = os.path.exists(os.path.join("/watch/input", safe_name)) or os.path.exists(
            os.path.join(ARCHIVE_DIR, safe_name)
        )
        # Recording start time (ms epoch from Plaud API)
        start_time_ms = rec.get("start_time", 0)
        start_time_iso = ""
        if start_time_ms:
            from datetime import datetime

            start_time_iso = datetime.fromtimestamp(start_time_ms / 1000, tz=UTC).isoformat()

        result.append(
            {
                "id": file_id,
                "filename": raw_name,
                "status": status_entry.get("status", "new"),
                "duration": duration_str,
                "start_time": start_time_iso,
                "local": local,
            }
        )
    return jsonify(result)


@app.route("/process/<file_id>", methods=["POST"])
def process_recording(file_id):
    """Start background processing for a Plaud recording (one at a time)."""
    from pipeline import load_pipeline_status, set_pipeline_status

    # Block if actively processing (but allow reprocessing of completed/errored)
    pipeline_status = load_pipeline_status()
    current = pipeline_status.get(file_id, {}).get("status", "new")
    if current not in ("new", "error", "processed"):
        return jsonify({"ok": False, "error": "Already processing"}), 409

    # Check if the lock is held (something else is processing)
    if _processing_lock.locked():
        return jsonify({"ok": False, "error": "Another recording is being processed. Please wait."}), 409

    config = load_config()
    token = config.get("plaud_token", "")
    if not token:
        return jsonify({"ok": False, "error": "No Plaud token configured"}), 400

    # Find the filename from the recordings list
    client = PlaudClient(
        token=token,
        base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
    )
    raw_filename = f"{file_id}.mp3"
    recordings = client.list_recordings()
    for rec in recordings:
        if rec.get("id") == file_id:
            raw_filename = rec.get("filename", rec.get("name", raw_filename))
            break

    # Mark as queued immediately so the UI updates
    set_pipeline_status(file_id, "queued", raw_filename)

    # Start background thread
    def _run():
        with _processing_lock:
            from pipeline import process_plaud_recording

            # Create a fresh client for the thread
            thread_client = PlaudClient(
                token=token,
                base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
            )
            process_plaud_recording(file_id, raw_filename, thread_client)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"ok": True, "status": "queued"})


# --- Recording file management ---


@app.route("/recording/<file_id>/audio", methods=["DELETE"])
def delete_recording_audio(file_id):
    """Delete the locally downloaded audio file for a recording."""
    from pipeline import ARCHIVE_DIR, _sanitize_filename

    config = load_config()
    token = config.get("plaud_token", "")
    if not token:
        return jsonify({"ok": False, "error": "No Plaud token configured"}), 400

    # Resolve filename from Plaud API
    client = PlaudClient(
        token=token,
        base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
    )
    raw_name = f"{file_id}.mp3"
    for rec in client.list_recordings():
        if rec.get("id") == file_id:
            raw_name = rec.get("filename", rec.get("name", raw_name))
            break

    safe_name = _sanitize_filename(raw_name)
    if not safe_name.lower().endswith((".mp3", ".ogg", ".m4a", ".wav", ".flac")):
        safe_name += ".mp3"

    deleted = []
    for directory in ("/watch/input", ARCHIVE_DIR):
        path = os.path.join(directory, safe_name)
        if os.path.exists(path):
            os.remove(path)
            deleted.append(path)
            logger.info("Deleted audio file: %s", path)

    return jsonify({"ok": True, "deleted": len(deleted)})


@app.route("/recording/<file_id>/documents", methods=["DELETE"])
def delete_recording_documents(file_id):
    """Delete local transcripts and summaries for a recording."""
    import glob

    from pipeline import _sanitize_filename

    config = load_config()
    token = config.get("plaud_token", "")
    if not token:
        return jsonify({"ok": False, "error": "No Plaud token configured"}), 400

    # Resolve filename from Plaud API
    client = PlaudClient(
        token=token,
        base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
    )
    raw_name = f"{file_id}.mp3"
    for rec in client.list_recordings():
        if rec.get("id") == file_id:
            raw_name = rec.get("filename", rec.get("name", raw_name))
            break

    safe_name = _sanitize_filename(raw_name)
    if not safe_name.lower().endswith((".mp3", ".ogg", ".m4a", ".wav", ".flac")):
        safe_name += ".mp3"
    base_name = os.path.splitext(safe_name)[0]

    deleted = []

    # Delete transcripts matching this recording
    transcript_dir = "/app/config/transcripts"
    for path in glob.glob(os.path.join(transcript_dir, f"{base_name}_*.txt")):
        # Extract timestamp from transcript filename to find matching docx
        ts = os.path.splitext(os.path.basename(path))[0].replace(f"{base_name}_", "", 1)
        os.remove(path)
        deleted.append(path)
        logger.info("Deleted transcript: %s", path)

        # Delete any docx summaries with the same timestamp
        docx_dir = "/tmp/claudioscribe"
        for docx_path in glob.glob(os.path.join(docx_dir, f"*_{ts}.docx")):
            os.remove(docx_path)
            deleted.append(docx_path)
            logger.info("Deleted summary: %s", docx_path)

    return jsonify({"ok": True, "deleted": len(deleted)})


# --- Google Drive OAuth routes ---


@app.route("/auth/google")
def google_auth():
    """Initiate Google OAuth flow."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        logger.error("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set")
        flash("Google OAuth not configured", "error")
        return redirect(url_for("settings_page"))

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/drive.file"],
        redirect_uri=request.url_root.rstrip("/") + "/oauth/callback",
    )

    state = secrets.token_urlsafe(32)
    # Store state in a file for CSRF verification (no DB available)
    state_file = os.path.join(os.environ.get("CONFIG_DIR", "/app/config"), "oauth_state")
    with open(state_file, "w") as f:
        f.write(state)

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )

    return redirect(authorization_url)


@app.route("/oauth/callback")
def google_callback():
    """Handle Google OAuth callback."""
    error = request.args.get("error")
    if error:
        logger.warning("Google OAuth error: %s", error)
        flash(f"Google denied access: {error}", "error")
        return redirect(url_for("settings_page"))

    # Verify CSRF state
    state = request.args.get("state", "")
    state_file = os.path.join(os.environ.get("CONFIG_DIR", "/app/config"), "oauth_state")
    try:
        with open(state_file) as f:
            expected_state = f.read().strip()
        os.remove(state_file)
    except OSError:
        expected_state = ""

    if not state or state != expected_state:
        logger.warning("OAuth state mismatch — possible CSRF")
        flash("OAuth state mismatch", "error")
        return redirect(url_for("settings_page"))

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/drive.file"],
        redirect_uri=request.url_root.rstrip("/") + "/oauth/callback",
    )

    flow.fetch_token(code=request.args.get("code"))
    creds = flow.credentials

    # Save tokens to config
    config = load_config()
    config["gdrive_enabled"] = True
    config["gdrive_access_token"] = creds.token
    config["gdrive_refresh_token"] = creds.refresh_token or config.get("gdrive_refresh_token", "")
    if creds.expiry:
        config["gdrive_token_expiry"] = creds.expiry.replace(tzinfo=UTC).isoformat()

    # Create folder structure on Drive
    try:
        from clients.gdrive_client import GDriveClient

        client = GDriveClient(
            access_token=creds.token,
            refresh_token=config["gdrive_refresh_token"],
            token_expiry=config.get("gdrive_token_expiry", ""),
        )
        folders = client.ensure_folder_structure()
        config.update(folders)
        logger.info("Google Drive folders created: %s", folders)
    except Exception:
        logger.exception("Failed to create Google Drive folders")
        flash("Connected but failed to create folders", "error")
        return redirect(url_for("settings_page"))

    save_config(config)
    logger.info("Google Drive connected successfully")
    flash("Google Drive connected successfully.", "success")
    return redirect(url_for("settings_page"))


@app.route("/auth/google/disconnect", methods=["POST"])
def google_disconnect():
    """Clear stored Google Drive tokens."""
    config = load_config()
    config["gdrive_enabled"] = False
    config["gdrive_access_token"] = ""
    config["gdrive_refresh_token"] = ""
    config["gdrive_token_expiry"] = ""
    config["gdrive_folder_id"] = ""
    config["gdrive_documents_folder_id"] = ""
    config["gdrive_recordings_folder_id"] = ""
    save_config(config)
    logger.info("Google Drive disconnected")
    return redirect(url_for("settings_page"))


@app.route("/privacy")
def privacy_policy():
    """Privacy policy page required for Google OAuth verification."""
    return render_template("privacy.html")


@app.route("/status")
def status_page():
    history = _load_history()
    return jsonify(history)


@app.route("/anthropic/test", methods=["POST"])
def anthropic_test():
    """Test the configured Anthropic API key."""
    config = load_config()
    api_key = config.get("anthropic_api_key", "")
    if not api_key:
        return jsonify({"ok": False, "message": "No API key configured"})
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        if response.id:
            return jsonify({"ok": True, "message": "Connected"})
        return jsonify({"ok": False, "message": "Unexpected response"})
    except Exception as exc:
        logger.warning("Anthropic API test failed: %s", exc)
        msg = str(exc).encode("ascii", errors="replace").decode("ascii")
        return jsonify({"ok": False, "message": msg})


@app.route("/whisper/status")
def whisper_status():
    """Return Whisper model cache info."""
    from pipeline import _WHISPER_CACHE_DIR

    model_path = os.path.join(_WHISPER_CACHE_DIR, "base.pt")
    if not os.path.exists(model_path):
        return jsonify({"cached": False})
    stat = os.stat(model_path)
    from datetime import datetime

    return jsonify(
        {
            "cached": True,
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "downloaded": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        }
    )


_whisper_download_status = {"running": False, "ok": None, "error": ""}


@app.route("/whisper/update", methods=["POST"])
def whisper_update():
    """Delete cached model and kick off background re-download."""
    if _whisper_download_status["running"]:
        return jsonify({"ok": True, "message": "Download already in progress"})

    from pipeline import _WHISPER_CACHE_DIR

    model_path = os.path.join(_WHISPER_CACHE_DIR, "base.pt")
    if os.path.exists(model_path):
        os.remove(model_path)
        logger.info("Deleted cached Whisper model for re-download")

    _whisper_download_status["running"] = True
    _whisper_download_status["ok"] = None
    _whisper_download_status["error"] = ""

    import threading

    def _download():
        try:
            import whisper

            whisper.load_model("base", download_root=_WHISPER_CACHE_DIR)
            logger.info("Whisper model re-downloaded successfully")
            _whisper_download_status["ok"] = True
        except Exception as exc:
            logger.exception("Failed to download Whisper model")
            _whisper_download_status["ok"] = False
            _whisper_download_status["error"] = str(exc)
        finally:
            _whisper_download_status["running"] = False

    threading.Thread(target=_download, daemon=True).start()
    return jsonify({"ok": True, "message": "Download started"})


@app.route("/whisper/update/status")
def whisper_update_status():
    """Poll endpoint for background download progress."""
    return jsonify(_whisper_download_status)


if __name__ == "__main__":
    from pipeline import reset_stale_statuses

    reset_stale_statuses()
    app.run(host="0.0.0.0", port=8080)
