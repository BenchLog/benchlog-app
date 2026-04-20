from pathlib import Path
from typing import BinaryIO

import anyio


class LocalStorage:
    """Filesystem-backed storage rooted at a single directory.

    All paths are resolved inside `root` and validated — attempts to escape
    via absolute paths, `..`, or symlinks raise ValueError.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        if not path:
            raise ValueError("storage path must not be empty")
        candidate = (self.root / path).resolve()
        # Ensure the resolved path stays within root (blocks ../ traversal).
        try:
            candidate.relative_to(self.root)
        except ValueError as e:
            raise ValueError(f"storage path {path!r} escapes root") from e
        return candidate

    async def save(self, path: str, data: BinaryIO) -> str:
        full = self._resolve(path)
        await anyio.Path(full.parent).mkdir(parents=True, exist_ok=True)
        async with await anyio.open_file(full, "wb") as f:
            while chunk := data.read(64 * 1024):
                await f.write(chunk)
        return path

    async def read(self, path: str) -> bytes:
        async with await anyio.open_file(self._resolve(path), "rb") as f:
            return await f.read()

    async def open(self, path: str) -> BinaryIO:
        """Return a blocking binary stream over the stored blob.

        Goes through `_resolve`, so path-traversal protection stays in place.
        The caller is responsible for closing the returned stream. Used by
        `copy_blob` to hand a sync-readable source to `save` without loading
        the whole blob into memory.
        """
        return open(self._resolve(path), "rb")

    async def delete(self, path: str) -> None:
        target = anyio.Path(self._resolve(path))
        if await target.exists():
            await target.unlink()

    async def exists(self, path: str) -> bool:
        return await anyio.Path(self._resolve(path)).exists()

    def get_url(self, path: str) -> str:
        return f"/files/{path}"

    def full_path(self, path: str) -> Path:
        return self._resolve(path)
