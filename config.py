import json
import logging
import os

logger = logging.getLogger(__name__)

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "anthropic_api_key": "",
    "output_dir": "/tmp/claudioscribe",
    "log_level": "INFO",
    "claude_prompt": (
        "You are a document assistant. Create a well-structured document "
        "from this audio transcript.\n\n"
        "Instructions:\n"
        "1. First list existing documents to understand context\n"
        "2. Create a new document with a clear title based on the content\n"
        "3. The document should include:\n"
        "   - A summary section at the top\n"
        "   - Key points or action items\n"
        "   - The full transcript at the bottom under a Transcript heading\n"
        "4. Use clear headings and organize the content logically"
    ),
    "claude_model": "claude-sonnet-4-6",
    "plaud_token": "",
    "plaud_base_url": "https://api-euc1.plaud.ai",
    "plaud_poll_interval": 60,
    "gdrive_enabled": False,
    "gdrive_access_token": "",
    "gdrive_refresh_token": "",
    "gdrive_token_expiry": "",
    "gdrive_folder_id": "",
    "gdrive_documents_folder_id": "",
    "gdrive_recordings_folder_id": "",
}


def load_config():
    """Load config from settings.json, filling in defaults for missing keys."""
    config = dict(DEFAULTS)

    # Override with env var if set (takes precedence over file)
    env_key = os.environ.get("ANTHROPIC_API_KEY")

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            config.update(saved)
            logger.info("Loaded config from %s", CONFIG_FILE)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read config file, using defaults: %s", e)
    else:
        logger.info("No config file found, using defaults")

    # Env var overrides file setting
    if env_key:
        config["anthropic_api_key"] = env_key

    return config


def save_config(data):
    """Validate and save config to settings.json."""
    config = dict(DEFAULTS)

    # Only save recognized keys
    for key in DEFAULTS:
        if key in data:
            config[key] = data[key]

    # Validate log_level
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
    if config["log_level"] not in valid_levels:
        config["log_level"] = "INFO"

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    logger.info("Config saved to %s", CONFIG_FILE)
    return config
