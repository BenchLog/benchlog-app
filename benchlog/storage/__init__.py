from functools import lru_cache

from benchlog.config import settings
from benchlog.storage.base import StorageBackend
from benchlog.storage.local import LocalStorage


@lru_cache(maxsize=1)
def get_storage() -> LocalStorage:
    """Return the configured storage backend (singleton).

    Only `local` is wired up; other backends (S3) would dispatch on
    `settings.storage_backend` here.
    """
    return LocalStorage(settings.storage_path)


__all__ = ["StorageBackend", "LocalStorage", "get_storage"]
