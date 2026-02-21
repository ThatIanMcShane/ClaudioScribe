import logging
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from pipeline import MAX_AUDIO_FILE_SIZE, process_audio_file

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WATCH_DIR = "/watch/input"
SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
PROCESSED = set()


def wait_for_file(filepath, timeout=120):
    """Wait until file is fully written and not locked."""
    start = time.time()
    last_size = -1
    while time.time() - start < timeout:
        try:
            if not os.path.exists(filepath):
                logger.debug("File not ready yet: %s", filepath)
                time.sleep(5)
                continue
            current_size = os.path.getsize(filepath)
            if current_size == last_size and current_size > 0:
                with open(filepath, "rb") as f:
                    f.read(1024)
                logger.info("File is fully synced: %s", filepath)
                return True
            last_size = current_size
            logger.debug("File still syncing... size=%d bytes", current_size)
        except (OSError, IOError) as e:
            logger.debug("File not ready yet: %s", e)
        time.sleep(5)
    return False


class AudioHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        filepath = event.src_path
        fname = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        if ext not in SUPPORTED_EXTENSIONS:
            return

        if fname in PROCESSED:
            logger.debug("Already processed: %s", fname)
            return

        if not os.path.exists(filepath):
            logger.debug("File no longer exists: %s", fname)
            return

        logger.info("New audio file detected: %s", filepath)
        PROCESSED.add(fname)

        if wait_for_file(filepath):
            # Check file size before processing
            file_size = os.path.getsize(filepath)
            if file_size > MAX_AUDIO_FILE_SIZE:
                logger.error(
                    "File too large (%d bytes, max %d): %s",
                    file_size,
                    MAX_AUDIO_FILE_SIZE,
                    fname,
                )
                PROCESSED.discard(fname)
                return
            process_audio_file(filepath)
        else:
            logger.warning("File never became ready: %s", filepath)
            PROCESSED.discard(fname)


if __name__ == "__main__":
    logger.info("Watching %s for audio files...", WATCH_DIR)
    os.makedirs(WATCH_DIR, exist_ok=True)
    event_handler = AudioHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
