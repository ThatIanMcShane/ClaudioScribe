import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import whisper

import doc_writer
from config import load_config

logger = logging.getLogger(__name__)

MAX_AUDIO_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
MAX_TRANSCRIPT_LENGTH = 500_000  # chars
MAX_TITLE_LENGTH = 200
ARCHIVE_DIR = "/watch/input/processed"
PIPELINE_STATUS_FILE = os.environ.get("PIPELINE_STATUS_FILE", "/app/config/pipeline_status.json")

# Active statuses that should be reset on startup (container restart killed the thread)
_ACTIVE_STATUSES = {"queued", "downloading", "uploading_recording", "transcribing", "analyzing"}

# Whisper model cached in config volume so it survives container restarts
_WHISPER_CACHE_DIR = "/app/config/whisper"
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        logger.info("Loading Whisper model...")
        _whisper_model = whisper.load_model("base", download_root=_WHISPER_CACHE_DIR)
    return _whisper_model


# --- Pipeline status tracking ---

def load_pipeline_status():
    """Load pipeline status for all recordings."""
    if not os.path.exists(PIPELINE_STATUS_FILE):
        return {}
    try:
        with open(PIPELINE_STATUS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def set_pipeline_status(file_id, status, filename=""):
    """Update the pipeline status for a recording."""
    all_status = load_pipeline_status()
    entry = all_status.get(file_id, {})
    entry["status"] = status
    entry["updated"] = datetime.now(tz=timezone.utc).isoformat()
    if filename:
        entry["filename"] = filename
    all_status[file_id] = entry
    os.makedirs(os.path.dirname(PIPELINE_STATUS_FILE), exist_ok=True)
    with open(PIPELINE_STATUS_FILE, "w") as f:
        json.dump(all_status, f, indent=2)
    logger.info("Pipeline status [%s]: %s", file_id[:8], status)


def reset_stale_statuses():
    """Reset any in-progress statuses to 'error' on startup.

    If the container restarted while a recording was being processed,
    the background thread is gone but the status file still says 'transcribing' etc.
    """
    all_status = load_pipeline_status()
    changed = False
    for file_id, entry in all_status.items():
        if entry.get("status") in _ACTIVE_STATUSES:
            logger.warning(
                "Resetting stale status [%s] %s -> error (container restart)",
                file_id[:8],
                entry["status"],
            )
            entry["status"] = "error"
            entry["updated"] = datetime.now(tz=timezone.utc).isoformat()
            changed = True
    if changed:
        with open(PIPELINE_STATUS_FILE, "w") as f:
            json.dump(all_status, f, indent=2)


# --- Filename helpers ---

def _validate_filename(filename):
    """Reject filenames with path separators or special characters."""
    if not filename:
        return False
    if any(c in filename for c in "/\\:*?\"<>|"):
        return False
    if filename.startswith("."):
        return False
    return True


def _sanitize_filename(name):
    """Remove characters unsafe for filenames."""
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. ")
    return "".join(c for c in name if c in keep).strip() or "recording"


# --- Audio processing ---

def transcribe_audio(file_path):
    """Transcribe audio file using Whisper. Returns timestamped transcript text."""
    logger.info("Transcribing: %s", file_path)

    file_size = os.path.getsize(file_path)
    if file_size > MAX_AUDIO_FILE_SIZE:
        raise ValueError(
            f"File too large: {file_size} bytes (max {MAX_AUDIO_FILE_SIZE})"
        )

    model = _get_whisper_model()
    result = model.transcribe(file_path)

    # Format each segment with [MM:SS] timestamps
    lines = []
    for seg in result.get("segments", []):
        start = int(seg["start"])
        mm, ss = divmod(start, 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg['text'].strip()}")

    text = "\n".join(lines) if lines else result["text"]

    if len(text) > MAX_TRANSCRIPT_LENGTH:
        logger.warning(
            "Transcript truncated from %d to %d chars",
            len(text),
            MAX_TRANSCRIPT_LENGTH,
        )
        text = text[:MAX_TRANSCRIPT_LENGTH]

    logger.info("Transcript length: %d chars", len(text))
    return text


def create_document_via_claude(filename, transcript, timestamp=""):
    """Use Claude's tool-use API to create a structured .docx document."""
    config = load_config()
    api_key = config["anthropic_api_key"]
    output_dir = "/tmp/claudioscribe"
    claude_prompt = config["claude_prompt"]
    claude_model = config.get("claude_model", "claude-sonnet-4-6")

    if not api_key:
        raise ValueError("Anthropic API key not configured")

    client = anthropic.Anthropic(api_key=api_key)
    base_name = os.path.splitext(filename)[0]

    tools = [
        {
            "name": "create_document",
            "description": "Create a new .docx document with the given title and content",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Document title (max 200 chars)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Document content with markdown headings",
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "list_documents",
            "description": "List existing documents in the output folder",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
    ]

    def handle_tool_call(tool_name, tool_input):
        if tool_name == "create_document":
            title = tool_input["title"][:MAX_TITLE_LENGTH]
            content = tool_input["content"]
            result = doc_writer.create_document(title, content, output_dir, timestamp=timestamp)
            logger.info("Document created: %s", result["filename"])
            _upload_to_gdrive(result["path"], "documents")
            return result
        elif tool_name == "list_documents":
            return doc_writer.list_documents(output_dir)
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    messages = [
        {
            "role": "user",
            "content": (
                f"{claude_prompt}\n\n"
                f"You are running as model: {claude_model}\n\n"
                f"Audio file: {base_name}\n\n"
                f"Transcript:\n{transcript}"
            ),
        }
    ]

    logger.info("Sending transcript to Claude for document creation...")

    while True:
        response = client.messages.create(
            model=claude_model,
            max_tokens=16384,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            logger.info("Document creation complete")
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Claude calling tool: %s", block.name)
                    result = handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            raise RuntimeError(f"Claude failed: stop_reason={response.stop_reason} (no document created)")


# --- Google Drive helpers ---

def _upload_to_gdrive(local_path, folder_type):
    """Upload a file to Google Drive if connected. folder_type is 'documents' or 'recordings'.

    Skips upload if a file with the same name already exists (dedup handled in GDriveClient).
    """
    try:
        from gdrive_client import get_gdrive_client_from_config

        client = get_gdrive_client_from_config()
        if client is None:
            return

        config = load_config()
        if folder_type == "documents":
            folder_id = config.get("gdrive_documents_folder_id")
        else:
            folder_id = config.get("gdrive_recordings_folder_id")

        if not folder_id:
            logger.warning("Google Drive %s folder ID not set, skipping upload", folder_type)
            return

        client.upload_file(local_path, folder_id)
    except Exception:
        logger.exception("Failed to upload %s to Google Drive", os.path.basename(local_path))


def _already_in_gdrive(filename):
    """Check if a recording already exists in the Google Drive recordings folder."""
    try:
        from gdrive_client import get_gdrive_client_from_config

        client = get_gdrive_client_from_config()
        if client is None:
            return False

        config = load_config()
        folder_id = config.get("gdrive_recordings_folder_id")
        if not folder_id:
            return False

        return client.file_exists(filename, folder_id) is not None
    except Exception:
        logger.exception("Failed to check Google Drive for %s", filename)
        return False


def _record_history(filename, status, message=""):
    """Record processing result for the status page."""
    try:
        from settings_app import add_history_entry
        add_history_entry(filename, status, message)
    except Exception:
        logger.debug("Could not record history entry")


def archive_audio(file_path):
    """Move processed audio file to the archive directory."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    dest = os.path.join(ARCHIVE_DIR, os.path.basename(file_path))
    os.rename(file_path, dest)
    logger.info("Archived: %s", dest)


def _find_existing_transcript(base_name, transcript_dir):
    """Return the path of an existing transcript for the given base name, or None."""
    import glob
    pattern = os.path.join(transcript_dir, f"{base_name}_*.txt")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


# --- Full pipeline for a single Plaud recording ---

def process_plaud_recording(file_id, raw_filename, plaud_client):
    """Download + transcribe + Claude + upload for a single Plaud recording.

    Updates pipeline status at each stage. Called from a background thread.
    """
    # Build a safe local filename
    filename = raw_filename
    if not filename.lower().endswith((".mp3", ".ogg", ".m4a", ".wav", ".flac")):
        filename += ".mp3"
    filename = _sanitize_filename(filename)

    download_dir = "/watch/input"
    os.makedirs(download_dir, exist_ok=True)
    file_path = os.path.join(download_dir, filename)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")

    try:
        # Step 1: Download
        set_pipeline_status(file_id, "downloading", filename)
        if not plaud_client.download_recording(file_id, file_path):
            set_pipeline_status(file_id, "error", filename)
            _record_history(filename, "error", "Download failed")
            return

        # Step 2: Upload recording to GDrive (skips automatically if already there)
        set_pipeline_status(file_id, "uploading_recording", filename)
        _upload_to_gdrive(file_path, "recordings")

        # Step 3: Transcribe (skip if transcript already exists)
        base_name = os.path.splitext(filename)[0]
        transcript_dir = "/app/config/transcripts"
        os.makedirs(transcript_dir, exist_ok=True)
        existing_transcript = _find_existing_transcript(base_name, transcript_dir)

        if existing_transcript:
            logger.info("Reusing existing transcript: %s", existing_transcript)
            with open(existing_transcript) as f:
                transcript = f.read()
            transcript_path = existing_transcript
        else:
            set_pipeline_status(file_id, "transcribing", filename)
            transcript = transcribe_audio(file_path)

            transcript_name = f"{base_name}_{timestamp}.txt"
            transcript_path = os.path.join(transcript_dir, transcript_name)
            with open(transcript_path, "w") as f:
                f.write(transcript)
            logger.info("Transcript saved: %s", transcript_path)
            _upload_to_gdrive(transcript_path, "documents")

        # Step 4: Analyze with Claude
        set_pipeline_status(file_id, "analyzing", filename)
        create_document_via_claude(filename, transcript, timestamp=timestamp)

        # Step 6: Done
        archive_audio(file_path)
        set_pipeline_status(file_id, "processed", filename)
        _record_history(filename, "success")
        logger.info("Successfully processed: %s", filename)

    except Exception as exc:
        logger.exception("Failed to process: %s", filename)
        set_pipeline_status(file_id, "error", filename)
        _record_history(filename, "error", str(exc))


# --- Legacy entry point (used by file watcher) ---

def process_audio_file(file_path):
    """Full pipeline: validate, transcribe, create document, archive."""
    filename = os.path.basename(file_path)

    if not _validate_filename(filename):
        logger.error("Invalid filename rejected: %s", filename)
        return

    file_size = os.path.getsize(file_path)
    if file_size > MAX_AUDIO_FILE_SIZE:
        logger.error(
            "File too large (%d bytes, max %d): %s",
            file_size,
            MAX_AUDIO_FILE_SIZE,
            filename,
        )
        return

    try:
        # Upload original recording to Drive (skips if already there)
        _upload_to_gdrive(file_path, "recordings")
        transcript = transcribe_audio(file_path)
        create_document_via_claude(filename, transcript)
        archive_audio(file_path)
        logger.info("Successfully processed: %s", filename)
        _record_history(filename, "success")
    except Exception as exc:
        logger.exception("Failed to process: %s", filename)
        _record_history(filename, "error", str(exc))
