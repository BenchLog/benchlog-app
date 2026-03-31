import hashlib
import mimetypes
import uuid

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.models.file import FileVersion, ProjectFile
from benchlog.storage.local import LocalStorage

storage = LocalStorage(settings.storage_path)


async def get_files_at_path(db: AsyncSession, project_id: uuid.UUID, path: str) -> list[ProjectFile]:
    """Get all files at a given virtual path within a project."""
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.versions))
        .where(ProjectFile.project_id == project_id, ProjectFile.path == path)
        .order_by(ProjectFile.sort_order, ProjectFile.filename)
    )
    return list(result.scalars().all())


async def get_subfolders(db: AsyncSession, project_id: uuid.UUID, path: str) -> list[str]:
    """Get distinct immediate subfolders at a given path."""
    prefix = f"{path}/" if path else ""
    result = await db.execute(
        select(ProjectFile.path)
        .where(
            ProjectFile.project_id == project_id,
            ProjectFile.path.like(f"{prefix}%") if prefix else ProjectFile.path != "",
        )
        .distinct()
    )
    paths = result.scalars().all()

    # Extract immediate children only
    folders = set()
    for p in paths:
        if prefix:
            remainder = p[len(prefix):]
        else:
            remainder = p
        if "/" in remainder:
            folders.add(remainder.split("/")[0])
        elif remainder:
            folders.add(remainder)
    return sorted(folders)


async def get_folder_tree(db: AsyncSession, project_id: uuid.UUID) -> list[str]:
    """Get all distinct folder paths in a project."""
    result = await db.execute(
        select(ProjectFile.path)
        .where(ProjectFile.project_id == project_id)
        .distinct()
    )
    paths = set()
    for p in result.scalars().all():
        if p:
            parts = p.split("/")
            for i in range(len(parts)):
                paths.add("/".join(parts[: i + 1]))
    return sorted(paths)


async def get_file_by_id(db: AsyncSession, file_id: uuid.UUID) -> ProjectFile | None:
    result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.versions))
        .where(ProjectFile.id == file_id)
    )
    return result.scalar_one_or_none()


async def upload_file(
    db: AsyncSession,
    project_id: uuid.UUID,
    path: str,
    filename: str,
    data: bytes,
    description: str = "",
) -> ProjectFile:
    """Upload a new file, creating the ProjectFile and first FileVersion."""
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    checksum = hashlib.sha256(data).hexdigest()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

    version_uuid = uuid.uuid4()
    storage_filename = f"files/{version_uuid}.{ext}" if ext else f"files/{version_uuid}"

    import io
    await storage.save(storage_filename, io.BytesIO(data))

    project_file = ProjectFile(
        project_id=project_id,
        path=path,
        filename=filename,
        description=description or None,
    )
    db.add(project_file)
    await db.flush()

    version = FileVersion(
        file_id=project_file.id,
        version_number=1,
        storage_path=storage_filename,
        original_name=filename,
        size_bytes=len(data),
        mime_type=mime_type,
        checksum=checksum,
        is_current=True,
    )
    db.add(version)
    await db.commit()
    return project_file


async def upload_new_version(
    db: AsyncSession,
    file_id: uuid.UUID,
    filename: str,
    data: bytes,
    changelog: str = "",
) -> FileVersion:
    """Upload a new version of an existing file."""
    project_file = await get_file_by_id(db, file_id)
    if not project_file:
        raise ValueError("File not found")

    # Mark old versions as not current
    for v in project_file.versions:
        v.is_current = False

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    checksum = hashlib.sha256(data).hexdigest()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

    max_version = max((v.version_number for v in project_file.versions), default=0)

    version_uuid = uuid.uuid4()
    storage_filename = f"files/{version_uuid}.{ext}" if ext else f"files/{version_uuid}"

    import io
    await storage.save(storage_filename, io.BytesIO(data))

    version = FileVersion(
        file_id=file_id,
        version_number=max_version + 1,
        storage_path=storage_filename,
        original_name=filename,
        size_bytes=len(data),
        mime_type=mime_type,
        checksum=checksum,
        changelog=changelog or None,
        is_current=True,
    )
    db.add(version)
    await db.commit()
    return version


async def download_version(version: FileVersion) -> bytes:
    """Download a specific file version from storage."""
    return await storage.read(version.storage_path)


async def delete_file(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Delete a file and all its versions from storage and DB."""
    project_file = await get_file_by_id(db, file_id)
    if not project_file:
        return

    for version in project_file.versions:
        await storage.delete(version.storage_path)

    await db.delete(project_file)
    await db.commit()


async def move_file(
    db: AsyncSession, file_id: uuid.UUID, new_path: str, new_filename: str | None = None
) -> ProjectFile | None:
    """Move/rename a file."""
    project_file = await get_file_by_id(db, file_id)
    if not project_file:
        return None

    project_file.path = new_path
    if new_filename:
        project_file.filename = new_filename

    await db.commit()
    return project_file


def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
