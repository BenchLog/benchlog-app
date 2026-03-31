import shutil
from pathlib import Path
from typing import BinaryIO

import anyio


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def save(self, path: str, data: BinaryIO) -> str:
        full = self.root / path
        await anyio.Path(full.parent).mkdir(parents=True, exist_ok=True)
        async with await anyio.open_file(full, "wb") as f:
            while chunk := data.read(64 * 1024):
                await f.write(chunk)
        return path

    async def read(self, path: str) -> bytes:
        async with await anyio.open_file(self.root / path, "rb") as f:
            return await f.read()

    async def delete(self, path: str) -> None:
        target = anyio.Path(self.root / path)
        if await target.exists():
            await target.unlink()

    async def exists(self, path: str) -> bool:
        return await anyio.Path(self.root / path).exists()

    def get_url(self, path: str) -> str:
        return f"/files/{path}"
