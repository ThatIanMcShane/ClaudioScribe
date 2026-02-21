"""Polls the Plaud API to maintain connection status (no auto-download)."""

import json
import logging
import os
import time
from datetime import datetime, timezone

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import load_config
from clients.plaud_client import PlaudClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATUS_FILE = os.environ.get("PLAUD_STATUS_FILE", "/app/config/plaud_status.json")


def write_status(ok: bool, message: str) -> None:
    """Write Plaud connection status to a shared file for the settings page."""
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump({
            "ok": ok,
            "message": message,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }, f)


def run() -> None:
    while True:
        config = load_config()
        token = config.get("plaud_token", "")
        poll_interval = int(config.get("plaud_poll_interval", 60))

        if not token:
            write_status(False, "No Plaud token configured")
            logger.info("No Plaud token configured — waiting for setup at http://localhost:8080")
            time.sleep(poll_interval)
            continue

        client = PlaudClient(
            token=token,
            base_url=config.get("plaud_base_url", "https://api.plaud.ai"),
        )

        status = client.test_connection()
        write_status(status["ok"], status["message"])
        if not status["ok"]:
            logger.warning("Plaud connection failed: %s", status["message"])

        time.sleep(poll_interval)


if __name__ == "__main__":
    logger.info("Plaud status poller starting")
    run()
