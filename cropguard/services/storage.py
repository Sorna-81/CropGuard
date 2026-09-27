"""Private crop image storage adapter (Supabase Storage or local development)."""
import uuid
from pathlib import Path

import requests

from .. import config


class ImageStorage:
    def __init__(self, timeout=20):
        self.timeout = timeout

    @property
    def remote(self):
        return bool(config.SUPABASE_URL and config.SUPABASE_DATABASE_ENABLED)

    def save(self, content: bytes, extension: str, user_auth_id=None, crop_id=None, local_dir=None):
        if self.remote:
            if not config.SUPABASE_SECRET_KEY:
                raise RuntimeError("Supabase Storage server key is not configured")
            if not user_auth_id:
                raise RuntimeError("Verified Supabase user identity is required for private image storage")
            path = f"{user_auth_id}/{crop_id or 'unassigned'}/{uuid.uuid4().hex}{extension}"
            response = requests.post(
                f"{config.SUPABASE_URL}/storage/v1/object/{config.SUPABASE_STORAGE_BUCKET}/{path}",
                headers={"apikey": config.SUPABASE_SECRET_KEY, "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}", "Content-Type": self.mime(extension), "x-upsert": "false"},
                data=content, timeout=self.timeout,
            )
            response.raise_for_status()
            return path
        filename = f"{uuid.uuid4().hex}{extension}"
        (Path(local_dir) / filename).write_bytes(content)
        return filename

    def read(self, path: str):
        if self.remote:
            response = requests.get(
                f"{config.SUPABASE_URL}/storage/v1/object/{config.SUPABASE_STORAGE_BUCKET}/{path}",
                headers={"apikey": config.SUPABASE_SECRET_KEY, "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}"}, timeout=self.timeout,
            )
            response.raise_for_status()
            return response.content
        return (config.UPLOAD_DIR / Path(path).name).read_bytes()

    @staticmethod
    def mime(extension):
        return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(extension, "application/octet-stream")


image_storage = ImageStorage()
