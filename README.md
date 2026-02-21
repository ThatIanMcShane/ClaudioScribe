# ClaudioScribe

Transcribe, summarize, and organize your Plaud recordings.

ClaudioScribe connects to your Plaud account, transcribes recordings locally using OpenAI Whisper, and uses Claude to generate structured .docx documents. Documents and original recordings are uploaded to Google Drive.

## How It Works

```
Plaud API → Process Now → Whisper (local) → Claude API → .docx → Google Drive
```

1. The web UI lists your Plaud recordings via the Plaud API
2. Click "Process Now" to download and process a recording
3. Audio is transcribed locally using [OpenAI Whisper](https://openai.com/index/whisper/) (no audio data leaves your machine)
4. Claude structures the transcript into a formatted .docx document with headings, lists, tables, and hyperlinks
5. The original MP3, transcript, and .docx are uploaded to Google Drive
6. Processed audio is archived locally

Everything runs inside a single Docker container.

## Web Interface

The web UI at `http://localhost:8080` provides a single-page dashboard:

- **Recordings list** with pagination, processing status, and local download indicators
- **Processing history** sidebar showing completed/failed jobs
- **Configuration panel** (gear icon) for managing service connections and settings
- **On-demand processing** — click "Process Now" on any recording, or reprocess previously completed ones

### Configuration Panel

The settings overlay is organized by service:

- **Plaud** — API token status and connection info
- **Google Drive** — Connect/disconnect via OAuth
- **Anthropic** — API key, Claude prompt template (with reset to default), log level
- **Whisper** — Model cache status, re-download option

## Stack

- **[OpenAI Whisper](https://openai.com/index/whisper/)** — Speech-to-text, runs locally on CPU inside Docker
- **[Claude API](https://platform.claude.com/settings/keys)** — Structures transcripts into formatted documents via tool use
- **python-docx** — .docx generation with markdown-to-Word conversion (bold, italic, hyperlinks, tables, headings)
- **Google Drive API** — Automatic upload via OAuth
- **Flask** — Web UI for configuration and recording management
- **Docker** — Containerized single-service runtime

## Requirements

- Docker Desktop (macOS or Linux)
- Anthropic API key ([create one here](https://platform.claude.com/settings/keys))
- Plaud device and account ([web.plaud.ai](https://web.plaud.ai))

## Project Structure

```
settings_app.py      Web UI: dashboard, configuration, recording management
pipeline.py          Processing pipeline: download, transcribe, analyze, upload
plaud_client.py      Plaud REST API client (auth, list, download)
plaud_watcher.py     Background poller for Plaud connection status
doc_writer.py        Markdown-to-docx converter with hyperlink/table support
gdrive_client.py     Google Drive API client (OAuth, folders, upload, dedup)
config.py            JSON-based configuration with defaults
watcher.py           File system watcher for audio files (legacy)
start.sh             Container entrypoint (starts poller + web UI)
templates/
  settings.html      Single-page dashboard and configuration overlay
  privacy.html       Privacy policy (required for Google OAuth)
Dockerfile           Container build (non-root user, pinned deps)
docker-compose.yml   Service definition with config volume
requirements.txt     Pinned Python dependencies
```

## Setup

### 1. Clone

```bash
git clone https://github.com/ThatIanMcShane/ClaudioScribe.git
cd ClaudioScribe
mkdir -p config
```

### 2. Build and start

```bash
docker compose build
docker compose up -d
```

### 3. Configure via the web UI

Open http://localhost:8080 and click the gear icon to configure:

**Plaud Token** — Get this from [web.plaud.ai](https://web.plaud.ai):
1. Log in to web.plaud.ai
2. Open DevTools (F12) > Application > Local Storage
3. Copy the `tokenstr` value
4. Click "Change Token" and paste it

**Anthropic API Key** — Click "Change Key" and paste your Claude API key.

**Google Drive** — Click "Connect Google Drive", sign in, and approve access. ClaudioScribe creates the folder structure automatically (`ClaudioScribe/Documents/` and `ClaudioScribe/Recordings/`).

## Usage

1. Record audio on your Plaud device
2. Sync the recording via the Plaud mobile app
3. Open http://localhost:8080 — your recordings appear automatically
4. Click "Process Now" to download, transcribe, and generate a document
5. The original MP3, transcript, and .docx appear in Google Drive under `ClaudioScribe/`

### Reprocessing

Previously processed recordings can be reprocessed by clicking "Reprocess". If a transcript already exists locally, the transcription step is skipped and only the Claude analysis is re-run — saving time and compute.

### Managing Local Files

Each recording has a `...` menu with options to:
- **Delete downloaded recording** — removes the audio file from the container
- **Delete local transcripts & summaries** — removes cached transcripts and generated documents

## Monitoring

```bash
# Follow live logs
docker compose logs -f

# Check recent logs
docker compose logs --tail=50
```

## Persistent Data

The `config/` directory is bind-mounted into the container and stores:
- `settings.json` — all configuration (API keys, tokens, Google Drive credentials)
- `transcripts/` — cached transcripts (reused on reprocessing)
- `whisper/` — cached Whisper model (~139 MB, survives container restarts)
- `pipeline_status.json` — processing state for each recording
- `history.json` — processing history for the dashboard

## Cost

Each recording costs approximately $0.01 - $0.02 in Anthropic API usage. For 50 recordings a month, expect around $0.50 - $1.00.

Whisper runs locally — no external transcription costs.

## Privacy

ClaudioScribe runs entirely on your own infrastructure. Audio is transcribed locally using Whisper. Only the text transcript is sent to the Anthropic API for document generation. See the [privacy policy](http://localhost:8080/privacy) for details.

## Security

- All data stays within your Docker container and your Google Drive — nothing is stored on external servers. Deleting the container removes all local files (config, transcripts, cached models)
- Audio is transcribed locally; only the text transcript is sent to the Anthropic API
- Runs as non-root user inside Docker
- Pinned dependencies in requirements.txt
- Google Drive OAuth uses the narrowest scope (`drive.file` — only files the app creates)
- CSRF protection on OAuth flow (state parameter with file-based verification)
- Input validation: file size limits (500 MB), filename sanitization, transcript length caps (500K chars)
- Stale processing states are automatically recovered on container restart
- API keys and tokens stored in `config/settings.json` (gitignored)
- Structured logging (no secrets in logs)

## Acknowledgments

Originally inspired by [Plaud-Claude-Obsidian](https://github.com/holzerchristopher-tech/Plaud-Claude-Obsidian) by Christopher Holzer.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).
