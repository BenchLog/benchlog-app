"""Data-access helpers for ProjectFile + FileVersion.

The upload pipeline lives here: stream the incoming file to local storage,
checksum it, generate a thumbnail if it's an image, then attach a
FileVersion to a ProjectFile (creating either as needed).

Path normalization is handled here too — virtual paths are stored without
leading/trailing slashes, with no `..` segments allowed.
"""

import hashlib
import io
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import FileVersion, ProjectFile
from benchlog.storage import LocalStorage

# Thumbnails are bounded so gallery grids stay snappy. WebP is small and
# universally supported in modern browsers.
_THUMB_MAX_DIMENSION = 600
_THUMB_FORMAT = "WEBP"
_THUMB_QUALITY = 82
_CHUNK = 64 * 1024

# Cap inline text previews so we don't stream a 50MB log into the page.
_TEXT_PREVIEW_LIMIT = 256 * 1024


_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(num_bytes: int | None) -> str:
    """Format a byte count as a compact human-readable string.

    Folders sum up descendant sizes and can cross unit boundaries, so we
    scale through B/KB/MB/GB/TB and keep one decimal once we leave bytes.
    """
    if num_bytes is None:
        return ""
    size = float(num_bytes)
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def preview_kind(mime_type: str | None, filename: str) -> str:
    """Which inline preview the detail page should render for this file.

    Returns one of: "image", "video", "audio", "pdf", "text", "none".
    The detail template dispatches on this to pick the right element.
    """
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf" or filename.lower().endswith(".pdf"):
        return "pdf"
    if mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/x-yaml",
    }:
        return "text"
    # A handful of code/config extensions whose servers often return
    # application/octet-stream — trust the extension so README.md etc.
    # still previews.
    text_ext = (
        ".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".tsv",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".py", ".js", ".ts", ".html", ".css", ".scss", ".sh",
        ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt",
        ".sql", ".dockerfile",
    )
    if filename.lower().endswith(text_ext):
        return "text"
    return "none"


async def read_text_preview(
    storage, storage_path: str, *, limit: int = _TEXT_PREVIEW_LIMIT
) -> tuple[str, bool]:
    """Return (text, was_truncated) for a text-previewable file."""
    data = await storage.read(storage_path)
    truncated = len(data) > limit
    slice_ = data[:limit]
    try:
        return slice_.decode("utf-8"), truncated
    except UnicodeDecodeError:
        return slice_.decode("utf-8", errors="replace"), truncated


# ---------- path + filename normalization ---------- #

# Forbidden on Windows (and, by extension, in cross-OS-safe names). Union
# with control characters to catch null bytes etc. that could slip past
# path libraries on some platforms.
_UNSAFE_NAME_CHARS = frozenset(
    '<>:"/\\|?*' + "".join(chr(i) for i in range(32))
)

# Windows reserves these as device names regardless of extension — so
# `CON`, `con`, and `CON.txt` are all blocked.
_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "con", "prn", "aux", "nul",
        *{f"com{i}" for i in range(1, 10)},
        *{f"lpt{i}" for i in range(1, 10)},
    }
)


def _validate_os_safe_name(name: str, *, label: str = "Name") -> None:
    """Raise ValueError if `name` wouldn't round-trip through a zip download
    on Windows / macOS / Linux.

    Checks the intersection of filesystem restrictions so a single rule
    set keeps every target platform happy. The caller is expected to
    have already stripped path separators.
    """
    if not name:
        raise ValueError(f"{label} cannot be empty.")
    for ch in name:
        if ch in _UNSAFE_NAME_CHARS:
            pretty = repr(ch) if ch.isprintable() else f"'\\x{ord(ch):02x}'"
            raise ValueError(f"{label} cannot contain {pretty}.")
    if name[-1] in {" ", "."}:
        raise ValueError(f"{label} cannot end with a space or period.")
    stem = name.split(".", 1)[0].lower()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"'{name}' is a reserved name on Windows.")


def normalize_virtual_path(raw: str | None) -> str:
    """Canonicalize a user-supplied virtual folder path.

    Empty/None becomes "" (project root). Otherwise: strip leading/trailing
    slashes, collapse runs of slashes, reject `..` traversal, and validate
    each segment as an OS-safe name (no `< > : " | ? *`, no reserved
    Windows device names, no trailing space/period).
    """
    if not raw:
        return ""
    cleaned = raw.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    parts = [seg for seg in cleaned.split("/") if seg]
    for seg in parts:
        if seg in {".", ".."}:
            raise ValueError("Path may not contain '.' or '..' segments.")
        _validate_os_safe_name(seg, label=f"Folder segment '{seg}'")
    return "/".join(parts)


def safe_filename(raw: str) -> str:
    """Strip path separators from a user-supplied filename, trim length,
    and enforce OS-safe-name rules (see `_validate_os_safe_name`).
    """
    name = (raw or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        raise ValueError("Filename is required.")
    # Reject names that are only dots (., .., ...) — they'd either be
    # traversal or disappear on Windows.
    if set(name) == {"."}:
        raise ValueError("Filename cannot be only dots.")
    name = name[:256]
    _validate_os_safe_name(name, label="Filename")
    return name


# ---------- lookup ---------- #


async def get_file_by_id(
    db: AsyncSession, project_id: uuid.UUID, file_id: uuid.UUID
) -> ProjectFile | None:
    """Scoped lookup — returns None for files in other projects."""
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.current_version))
        .where(
            ProjectFile.id == file_id,
            ProjectFile.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def get_existing_file(
    db: AsyncSession, project_id: uuid.UUID, path: str, filename: str
) -> ProjectFile | None:
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.current_version))
        .where(
            ProjectFile.project_id == project_id,
            ProjectFile.path == path,
            ProjectFile.filename == filename,
        )
    )
    return result.scalar_one_or_none()


async def next_version_number(db: AsyncSession, file_id: uuid.UUID) -> int:
    """1-based, monotonically increasing per file."""
    result = await db.execute(
        select(func.max(FileVersion.version_number)).where(
            FileVersion.file_id == file_id
        )
    )
    current = result.scalar_one_or_none()
    return 1 if current is None else current + 1


# ---------- upload ---------- #


@dataclass
class StoredBlob:
    storage_path: str
    size_bytes: int
    checksum: str
    width: int | None
    height: int | None
    thumbnail_path: str | None
    detected_mime: str | None


async def store_upload(
    storage: LocalStorage,
    *,
    file_id: uuid.UUID,
    version_number: int,
    source: BinaryIO,
    declared_mime: str,
) -> StoredBlob:
    """Write the upload to storage, checksum it, generate thumbnail if image.

    Returns the metadata needed to construct the FileVersion row. The caller
    persists that row in its own transaction.
    """
    storage_path = f"files/{file_id}/{version_number}"
    size_bytes, checksum = await _save_with_checksum(storage, storage_path, source)

    width: int | None = None
    height: int | None = None
    thumbnail_path: str | None = None
    detected_mime: str | None = None
    if (declared_mime or "").startswith("image/"):
        try:
            data = await storage.read(storage_path)
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                # WebP output drops EXIF, so orientation metadata would be
                # lost and the thumbnail would render sideways. Bake the
                # rotation into pixel data first.
                oriented = ImageOps.exif_transpose(img)
                width, height = oriented.size
                detected_mime = Image.MIME.get(img.format)
                thumbnail_path = await _save_thumbnail(
                    storage, file_id, version_number, oriented
                )
        except (UnidentifiedImageError, OSError):
            # Fall through with image fields left null — declared_mime was
            # wrong or the bytes are corrupt. The file still uploads OK; it
            # just won't appear in the gallery.
            width = height = None
            thumbnail_path = None
            detected_mime = None

    return StoredBlob(
        storage_path=storage_path,
        size_bytes=size_bytes,
        checksum=checksum,
        width=width,
        height=height,
        thumbnail_path=thumbnail_path,
        detected_mime=detected_mime,
    )


async def _save_with_checksum(
    storage: LocalStorage, storage_path: str, source: BinaryIO
) -> tuple[int, str]:
    """Save while computing sha256 and the byte count in one pass."""
    sha = hashlib.sha256()
    size = 0

    class _CountingHasher:
        def read(self, n: int = -1) -> bytes:
            chunk = source.read(n if n > 0 else _CHUNK)
            if chunk:
                sha.update(chunk)
            nonlocal size
            size += len(chunk)
            return chunk

    await storage.save(storage_path, _CountingHasher())
    return size, sha.hexdigest()


async def _save_thumbnail(
    storage: LocalStorage,
    file_id: uuid.UUID,
    version_number: int,
    img: Image.Image,
) -> str:
    """Bound the longer side to _THUMB_MAX_DIMENSION; encode as WebP."""
    thumb = img.copy()
    thumb.thumbnail((_THUMB_MAX_DIMENSION, _THUMB_MAX_DIMENSION))
    if thumb.mode not in {"RGB", "RGBA"}:
        thumb = thumb.convert("RGBA" if "A" in thumb.mode else "RGB")
    buf = io.BytesIO()
    thumb.save(buf, format=_THUMB_FORMAT, quality=_THUMB_QUALITY, method=4)
    buf.seek(0)
    thumbnail_path = f"thumbnails/{file_id}/{version_number}.webp"
    await storage.save(thumbnail_path, buf)
    return thumbnail_path


# ---------- delete-side cleanup ---------- #


async def list_files_in_folder(
    db: AsyncSession, project_id: uuid.UUID, folder_path: str
) -> list[ProjectFile]:
    """Every file whose virtual path is exactly `folder_path` or nested
    below it. `folder_path` must already be normalized."""
    from sqlalchemy import or_

    prefix = f"{folder_path}/"
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.versions))
        .where(
            ProjectFile.project_id == project_id,
            or_(
                ProjectFile.path == folder_path,
                ProjectFile.path.startswith(prefix),
            ),
        )
    )
    return list(result.scalars().unique().all())


async def rename_folder(
    db: AsyncSession,
    project_id: uuid.UUID,
    old_path: str,
    new_path: str,
) -> int:
    """Rename (or move) a virtual folder.

    Folders aren't their own entity — they're just the `path` column on
    each ProjectFile row. Renaming rewrites every descendant's path to
    use the new prefix. `new_path` can have a different parent, so this
    doubles as a move: `models/widgets` → `archive/widgets-old`.

    Raises ValueError if the rename would collide with a file already
    sitting at the destination.
    """
    if old_path == new_path:
        return 0
    files = await list_files_in_folder(db, project_id, old_path)
    if not files:
        raise ValueError(f"Folder '{old_path}' has no files in it.")

    # Build the destination paths first so we can collision-check before
    # mutating anything.
    moves: list[tuple[ProjectFile, str]] = []
    moving_ids = {f.id for f in files}
    for f in files:
        remainder = f.path[len(old_path):]  # "" or "/sub/..."
        moves.append((f, new_path + remainder))

    # Collision check — does a file OUTSIDE the moving set already live at
    # any of the destination (path, filename) slots?
    for f, dest_path in moves:
        clash = await db.execute(
            select(ProjectFile.id).where(
                ProjectFile.project_id == project_id,
                ProjectFile.path == dest_path,
                ProjectFile.filename == f.filename,
                ProjectFile.id.notin_(moving_ids),
            )
        )
        if clash.scalar_one_or_none() is not None:
            raise ValueError(
                f"Can't rename — '{dest_path}/{f.filename}' already exists."
            )

    for f, dest_path in moves:
        f.path = dest_path
    return len(moves)


async def delete_folder(
    db: AsyncSession,
    storage: LocalStorage,
    project_id: uuid.UUID,
    folder_path: str,
) -> int:
    """Delete every file under `folder_path` (inclusive) + their blobs.

    Returns the number of ProjectFile rows deleted. Blob cleanup is best-
    effort; a missing blob on disk never blocks the DB delete.
    """
    files = await list_files_in_folder(db, project_id, folder_path)
    versions = [v for f in files for v in f.versions]

    # Break the ProjectFile -> FileVersion circular FK before deleting so
    # SQLAlchemy doesn't trip over it during cascade.
    for f in files:
        f.current_version_id = None
    await db.flush()
    for f in files:
        await db.delete(f)

    for v in versions:
        await delete_blob(storage, v)
    return len(files)


async def delete_blob(storage: LocalStorage, version: FileVersion) -> None:
    """Remove a version's stored blob (and thumbnail if any). DB row is
    handled separately by the cascade or the route's own delete()."""
    try:
        await storage.delete(version.storage_path)
    except (FileNotFoundError, ValueError):
        pass
    if version.thumbnail_path:
        try:
            await storage.delete(version.thumbnail_path)
        except (FileNotFoundError, ValueError):
            pass
