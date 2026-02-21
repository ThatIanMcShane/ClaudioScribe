import logging
import os
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

ROOT_FOLDER_NAME = "ClaudioScribe"
DOCUMENTS_FOLDER_NAME = "Documents"
RECORDINGS_FOLDER_NAME = "Recordings"


class GDriveClient:
    """Google Drive API wrapper for uploading files."""

    def __init__(self, access_token, refresh_token, token_expiry=None):
        self._creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            scopes=SCOPES,
        )
        if token_expiry:
            try:
                self._creds.expiry = datetime.fromisoformat(token_expiry).replace(
                    tzinfo=None
                )
            except (ValueError, TypeError):
                pass

    def _get_service(self):
        """Build Drive v3 service, auto-refreshing token if expired."""
        if self._creds.expired and self._creds.refresh_token:
            logger.info("Refreshing expired Google access token")
            self._creds.refresh(Request())
            self._save_refreshed_token()
        return build("drive", "v3", credentials=self._creds)

    def _save_refreshed_token(self):
        """Persist refreshed token back to settings.json."""
        try:
            from config import load_config, save_config

            config = load_config()
            config["gdrive_access_token"] = self._creds.token
            if self._creds.expiry:
                config["gdrive_token_expiry"] = self._creds.expiry.replace(
                    tzinfo=timezone.utc
                ).isoformat()
            save_config(config)
            logger.info("Saved refreshed Google token to config")
        except Exception:
            logger.exception("Failed to save refreshed Google token")

    def get_or_create_folder(self, name, parent_id="root"):
        """Find or create a folder by name under the given parent."""
        service = self._get_service()

        query = (
            f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        results = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        files = results.get("files", [])
        if files:
            logger.info("Found existing folder: %s (%s)", name, files[0]["id"])
            return files[0]["id"]

        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=metadata, fields="id").execute()
        logger.info("Created folder: %s (%s)", name, folder["id"])
        return folder["id"]

    def ensure_folder_structure(self):
        """Create ClaudioScribe/Documents/ and ClaudioScribe/Recordings/ if needed.

        Returns dict with folder IDs.
        """
        root_id = self.get_or_create_folder(ROOT_FOLDER_NAME)
        docs_id = self.get_or_create_folder(DOCUMENTS_FOLDER_NAME, root_id)
        recs_id = self.get_or_create_folder(RECORDINGS_FOLDER_NAME, root_id)

        return {
            "gdrive_folder_id": root_id,
            "gdrive_documents_folder_id": docs_id,
            "gdrive_recordings_folder_id": recs_id,
        }

    def file_exists(self, filename, folder_id):
        """Check if a file with the given name already exists in the folder.

        Returns the file ID if found, None otherwise.
        """
        service = self._get_service()
        # Escape single quotes in filename for the query
        safe_name = filename.replace("'", "\\'")
        query = (
            f"name = '{safe_name}' "
            f"and '{folder_id}' in parents and trashed = false"
        )
        results = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
            .execute()
        )
        files = results.get("files", [])
        if files:
            logger.info("File already exists in Drive: %s (%s)", filename, files[0]["id"])
            return files[0]["id"]
        return None

    def upload_file(self, local_path, folder_id, filename=None):
        """Upload a file to the given folder. Skips if already present. Returns the file ID."""
        service = self._get_service()
        if filename is None:
            filename = os.path.basename(local_path)

        existing_id = self.file_exists(filename, folder_id)
        if existing_id:
            logger.info("Skipping upload, already in Drive: %s", filename)
            return existing_id

        metadata = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(local_path, resumable=True)
        uploaded = (
            service.files()
            .create(body=metadata, media_body=media, fields="id")
            .execute()
        )
        logger.info("Uploaded %s to Google Drive (%s)", filename, uploaded["id"])
        return uploaded["id"]

    def test_connection(self):
        """Test that credentials work by listing files."""
        try:
            service = self._get_service()
            service.files().list(pageSize=1, fields="files(id)").execute()
            return {"ok": True, "message": "Connected to Google Drive"}
        except Exception as e:
            logger.exception("Google Drive connection test failed")
            return {"ok": False, "message": f"Connection failed: {e}"}


def get_gdrive_client_from_config():
    """Build a GDriveClient from the current settings, or None if not configured."""
    from config import load_config

    config = load_config()
    if not config.get("gdrive_enabled") or not config.get("gdrive_refresh_token"):
        return None

    return GDriveClient(
        access_token=config.get("gdrive_access_token", ""),
        refresh_token=config["gdrive_refresh_token"],
        token_expiry=config.get("gdrive_token_expiry", ""),
    )
