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
from pygments import highlight as _pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.file_references import (
    rewrite_file_references,
    rewrite_folder_references,
    rewrite_journal_references,
)
from benchlog.models import FileVersion, Project, ProjectFile
from benchlog.storage import LocalStorage

# Cap decoded image pixels so a small crafted PNG can't balloon into
# gigabytes of RAM when PIL decompresses it (classic "zip bomb" for
# images). 64 megapixels covers a ~9000×7000 photo with headroom.
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024

# Thumbnails are bounded so gallery grids stay snappy. WebP is small and
# universally supported in modern browsers.
_THUMB_MAX_DIMENSION = 600
_THUMB_FORMAT = "WEBP"
_THUMB_QUALITY = 82
_CHUNK = 64 * 1024


class UploadTooLarge(Exception):
    """Raised mid-stream when an upload exceeds the configured byte cap.

    The Content-Length pre-check in the route is advisory (client-supplied);
    this exception is the authoritative enforcement.
    """

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

    Returns one of: "image", "video", "audio", "pdf", "code", "text", "none".
    The detail template dispatches on this to pick the right element.

    "code" is a textual file whose extension Pygments knows a lexer for —
    route layer will render it with server-side syntax highlighting. "text"
    is the fallback for textual files without a lexer (logs, csv, etc.).
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
    is_textual = mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/x-yaml",
    }
    # A handful of code/config extensions whose servers often return
    # application/octet-stream — trust the extension so README.md etc.
    # still previews.
    text_ext = (
        ".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".tsv",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".py", ".js", ".ts", ".html", ".css", ".scss", ".sh",
        ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt",
        ".sql", ".dockerfile", ".scad",
    )
    if is_textual or filename.lower().endswith(text_ext):
        # If Pygments recognises the extension we get a nicer preview; if
        # not, keep the textual fallback.
        if code_language(filename) is not None:
            return "code"
        return "text"
    return "none"


def code_language(filename: str) -> str | None:
    """Return the Pygments lexer alias for `filename`, or None if unknown.

    Uses the file extension only — `get_lexer_for_filename` is content-agnostic
    when passed no `code` argument, which is what we want (we may not have
    the bytes loaded yet when we call this).
    """
    try:
        lexer = get_lexer_for_filename(filename)
    except ClassNotFound:
        return None
    # Plain-text-ish lexers don't add highlighting value; treat them as
    # "no lexer" so the route falls back to the simpler <pre> path.
    if isinstance(lexer, TextLexer):
        return None
    aliases = getattr(lexer, "aliases", None)
    if aliases:
        return aliases[0]
    return lexer.name.lower()


# Line numbers render as a separate <td> column so copy/paste skips them.
# `lineanchors="L"` produces `<span id="L42">` anchors for deep links.
_HTML_FORMATTER = HtmlFormatter(
    linenos="table",
    lineanchors="L",
    anchorlinenos=False,
    cssclass="highlight",
)


def highlight_code(text: str, language: str) -> str:
    """Render `text` as HTML with Pygments syntax highlighting + line numbers.

    Falls back to the plain `TextLexer` if the language name isn't recognised
    so a bad alias never raises up into the request.
    """
    try:
        lexer = get_lexer_by_name(language, stripall=False)
    except ClassNotFound:
        lexer = TextLexer(stripall=False)
    return _pygments_highlight(text, lexer, _HTML_FORMATTER)


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


# Hard cap for the editor autocomplete payload. Makers rarely cross this in
# practice; silently truncate so a pathological project doesn't balloon the
# form HTML (or the client-side filter loop).
_FILE_INDEX_MAX = 500


async def get_project_file_index(
    db: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """Serialize a project's files for the editor `files/…` autocomplete.

    Returns `[{"path": str, "filename": str, "is_image": bool}, …]` sorted
    by `(path, filename)` and capped at ``_FILE_INDEX_MAX`` entries. Files
    without a current version are skipped — they can't be downloaded, so
    linking to them from markdown would be a dead end.

    Eager-loads ``current_version`` so ``raise_on_sql`` doesn't bite when
    we read ``mime_type`` to derive ``is_image``.
    """
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.current_version))
        .where(
            ProjectFile.project_id == project_id,
            ProjectFile.current_version_id.is_not(None),
        )
        .order_by(ProjectFile.path.asc(), ProjectFile.filename.asc())
        .limit(_FILE_INDEX_MAX)
    )
    out: list[dict] = []
    for f in result.scalars().all():
        mime = (f.current_version.mime_type if f.current_version else "") or ""
        out.append(
            {
                "path": f.path or "",
                "filename": f.filename,
                "is_image": mime.startswith("image/"),
            }
        )
    return out


# Hard cap for the editor autocomplete's journal-entry payload. Same
# bounding logic as files — untitled entries are excluded (they have no
# slug to link to), so the cap only covers the linkable subset.
_ENTRY_INDEX_MAX = 500


async def get_project_entry_index(
    db: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """Serialize a project's titled journal entries for the `journal/…`
    autocomplete.

    Returns `[{"slug": str, "title": str}, …]` sorted by `(title)` and
    capped at ``_ENTRY_INDEX_MAX`` entries. Untitled entries are skipped
    — they have no slug and therefore no deep-link target.

    NOTE: does NOT filter by `is_public`. Only call from owner-gated
    surfaces (new/edit forms, owner's description editor). Rendering this
    on a non-owner page would leak private entry slugs + titles.
    """
    # Local import to sidestep the import cycle (models imports Base,
    # files imports models; journal_entry lands here at runtime without
    # a top-level circular).
    from benchlog.models import JournalEntry

    result = await db.execute(
        select(JournalEntry)
        .where(
            JournalEntry.project_id == project_id,
            JournalEntry.slug.is_not(None),
        )
        .order_by(JournalEntry.title.asc())
        .limit(_ENTRY_INDEX_MAX)
    )
    return [
        {"slug": e.slug, "title": e.title or ""}
        for e in result.scalars().all()
    ]


async def get_project_file_lookup(db: AsyncSession, project_id: uuid.UUID):
    """Return a `(path, filename) -> file_id_str` callable for markdown
    `files/…` link rewriting. Use from routes that don't eager-load files
    (e.g. the description AJAX endpoint). Routes that already load
    ``project.files`` should use ``benchlog.markdown.build_file_lookup_from_files``
    instead to avoid an extra DB round-trip.
    """
    result = await db.execute(
        select(ProjectFile.id, ProjectFile.path, ProjectFile.filename).where(
            ProjectFile.project_id == project_id,
            ProjectFile.current_version_id.is_not(None),
        )
    )
    index: dict[tuple[str, str], str] = {}
    for row in result.all():
        index[(row.path or "", row.filename)] = str(row.id)
    return lambda path, filename: index.get((path, filename))


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
    max_bytes: int,
) -> StoredBlob:
    """Write the upload to storage, checksum it, generate thumbnail if image.

    Returns the metadata needed to construct the FileVersion row. The caller
    persists that row in its own transaction. Raises UploadTooLarge if the
    streamed body exceeds `max_bytes`.
    """
    storage_path = f"files/{file_id}/{version_number}"
    try:
        size_bytes, checksum = await _save_with_checksum(
            storage, storage_path, source, max_bytes=max_bytes
        )
    except UploadTooLarge:
        # Clean up the partial blob so we don't leak disk on a rejected upload.
        try:
            await storage.delete(storage_path)
        except (FileNotFoundError, ValueError):
            pass
        raise

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
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            # Fall through with image fields left null — declared_mime was
            # wrong, the bytes are corrupt, or the decoded pixel count
            # exceeded MAX_IMAGE_PIXELS. The file still uploads OK; it just
            # won't appear in the gallery.
            width = height = None
            thumbnail_path = None
            detected_mime = None
    # Note: `regenerate_thumbnail_from_storage` below reuses this same
    # decode+thumbnail path for restored versions. Keep them in sync.

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
    storage: LocalStorage,
    storage_path: str,
    source: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[int, str]:
    """Save while computing sha256 and the byte count in one pass.

    Enforces `max_bytes` on the actual streamed bytes — Content-Length is
    client-supplied and can lie, so the only trustworthy cap is counted
    during the copy.
    """
    sha = hashlib.sha256()
    size = 0

    class _CountingHasher:
        def read(self, n: int = -1) -> bytes:
            chunk = source.read(n if n > 0 else _CHUNK)
            if chunk:
                sha.update(chunk)
            nonlocal size
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLarge()
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


async def regenerate_thumbnail_from_storage(
    storage: LocalStorage,
    *,
    file_id: uuid.UUID,
    version_number: int,
    storage_path: str,
) -> tuple[int | None, int | None, str | None]:
    """Read the blob at `storage_path`, decode as image, write a thumbnail.

    Returns (width, height, thumbnail_path) on success, or (None, None, None)
    if the blob isn't a valid image. Mirrors the Pillow path in `store_upload`
    so restored-version thumbnails match the initial-upload pipeline exactly.
    """
    try:
        data = await storage.read(storage_path)
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            oriented = ImageOps.exif_transpose(img)
            width, height = oriented.size
            thumbnail_path = await _save_thumbnail(
                storage, file_id, version_number, oriented
            )
            return width, height, thumbnail_path
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None, None, None


async def copy_blob(
    storage: LocalStorage, src_storage_path: str, dst_storage_path: str
) -> None:
    """Copy a stored blob from one path to another via the storage backend.

    Both paths are storage-relative (not filesystem absolute). Streams through
    the same `storage.save` path new uploads use, so path-traversal protection
    stays intact on both ends.
    """
    stream = await storage.open(src_storage_path)
    try:
        await storage.save(dst_storage_path, stream)
    finally:
        stream.close()


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


# ---------- rename-tracking for markdown refs ---------- #
#
# When a file or folder moves, or a journal entry's slug changes, the link
# text in the project's description and sibling journal entries still
# points at the old target. The renderer falls back to the files browser
# (or 404 for journal) so the link doesn't silently resolve elsewhere, but
# the author's prose still lies about where the target lives. These
# helpers patch the source markdown so the reference itself stays
# truthful.
#
# Every helper assumes `project.journal_entries` is already eager-loaded —
# the relationship is `raise_on_sql`. Callers who don't already have it
# loaded must `selectinload(Project.journal_entries)` first.


async def apply_file_rename_to_project_markdown(
    db: AsyncSession,
    project: Project,
    old_full_path: str,
    new_full_path: str,
) -> int:
    """Rewrite `files/<old_full_path>` refs in description + journal entries.

    Returns the number of refs rewritten across everything. Commits. No-op
    (returns 0) when old == new.
    """
    if old_full_path == new_full_path:
        return 0

    total = 0
    if project.description:
        result = rewrite_file_references(
            project.description, old_full_path, new_full_path
        )
        if result.count:
            project.description = result.text
            total += result.count

    for entry in project.journal_entries:
        if not entry.content:
            continue
        result = rewrite_file_references(
            entry.content, old_full_path, new_full_path
        )
        if result.count:
            entry.content = result.text
            total += result.count

    if total:
        await db.commit()
    return total


async def apply_folder_rename_to_project_markdown(
    db: AsyncSession,
    project: Project,
    old_folder: str,
    new_folder: str,
) -> int:
    """Rewrite `files/<old_folder>/…` refs in description + journal entries.

    Returns the total count. Commits. No-op when old == new.
    """
    if old_folder == new_folder:
        return 0

    total = 0
    if project.description:
        result = rewrite_folder_references(
            project.description, old_folder, new_folder
        )
        if result.count:
            project.description = result.text
            total += result.count

    for entry in project.journal_entries:
        if not entry.content:
            continue
        result = rewrite_folder_references(
            entry.content, old_folder, new_folder
        )
        if result.count:
            entry.content = result.text
            total += result.count

    if total:
        await db.commit()
    return total


async def apply_journal_rename_to_project_markdown(
    db: AsyncSession,
    project: Project,
    username: str,
    old_entry_slug: str,
    new_entry_slug: str,
    *,
    old_title: str | None = None,
    new_title: str | None = None,
    skip_entry_id=None,
) -> int:
    """Rewrite journal refs in description + sibling entries when an entry
    is renamed (slug and/or title).

    Scoped to this project only — journal slugs are per-project unique, so
    rewriting across projects would touch unrelated links. Skips
    `skip_entry_id` when provided so the entry whose slug just changed
    doesn't self-rewrite its own body (the author may have embedded the
    new slug/title in prose and we don't want to double-substitute).

    Returns the total count. Commits. No-op when neither slug nor title
    changed.
    """
    if old_entry_slug == new_entry_slug and (
        old_title is None or new_title is None or old_title == new_title
    ):
        return 0

    total = 0
    if project.description:
        result = rewrite_journal_references(
            project.description,
            username,
            project.slug,
            old_entry_slug,
            new_entry_slug,
            old_title=old_title,
            new_title=new_title,
        )
        if result.count:
            project.description = result.text
            total += result.count

    for entry in project.journal_entries:
        if skip_entry_id is not None and entry.id == skip_entry_id:
            continue
        if not entry.content:
            continue
        result = rewrite_journal_references(
            entry.content,
            username,
            project.slug,
            old_entry_slug,
            new_entry_slug,
            old_title=old_title,
            new_title=new_title,
        )
        if result.count:
            entry.content = result.text
            total += result.count

    if total:
        await db.commit()
    return total
