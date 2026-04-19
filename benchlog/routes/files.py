"""Routes for project files — browser, upload, detail, download, version, edit, delete.

URL scheme matches updates/links: `/u/{username}/{slug}/files/...`. Visibility
inherits the parent project (no per-file flag, like links). Mutation routes
are owner-only with the same `_require_owned_project` pattern.

Route ordering matters: literal-suffix routes (`/files/new`, `/files/reorder`)
declared BEFORE `{file_id}`-parameterized routes, otherwise FastAPI matches
the literal as a UUID param and 422s.
"""

import io
import mimetypes
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.files import (
    StoredBlob,
    delete_blob,
    delete_folder,
    get_existing_file,
    get_file_by_id,
    list_files_in_folder,
    next_version_number,
    normalize_virtual_path,
    preview_kind,
    read_text_preview,
    rename_folder,
    safe_filename,
    store_upload,
)
from benchlog.models import FileVersion, Project, ProjectFile, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
)
from benchlog.storage import get_storage
from benchlog.templating import templates

router = APIRouter()


# ---------- owner helper (same pattern as links/updates) ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
) -> Project:
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


def _form_values(*, path: str = "", description: str = "") -> dict:
    return {"path": path, "description": description}


async def _load_file_with_versions(
    db: AsyncSession, project_id: uuid.UUID, file_id: uuid.UUID
) -> ProjectFile | None:
    """Load a file plus its full version history + current_version."""
    from sqlalchemy import select

    result = await db.execute(
        select(ProjectFile)
        .options(
            selectinload(ProjectFile.versions),
            selectinload(ProjectFile.current_version),
        )
        .where(
            ProjectFile.id == file_id,
            ProjectFile.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


def _content_type_from_filename(filename: str, fallback: str = "application/octet-stream") -> str:
    guess, _ = mimetypes.guess_type(filename)
    return guess or fallback


def _wants_json(request: Request) -> bool:
    """True when the client prefers a JSON response to HTML.

    Used by edit routes that double as progressive-enhancement endpoints:
    the modal submits via fetch with `Accept: application/json` and
    expects 204/JSON errors, while the fallback HTML form expects the
    usual redirect/re-render flow.
    """
    return "application/json" in request.headers.get("accept", "")


SORT_COLUMNS = ("name", "size", "version", "modified")
SORT_DIRECTIONS = ("asc", "desc")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _normalize_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    col = sort if sort in SORT_COLUMNS else "name"
    dir_ = direction if direction in SORT_DIRECTIONS else "asc"
    return col, dir_


def _file_sort_key(f: ProjectFile, column: str):
    cv = f.current_version
    if column == "size":
        return cv.size_bytes if cv else 0
    if column == "version":
        return cv.version_number if cv else 0
    if column == "modified":
        return cv.uploaded_at if cv else _EPOCH
    return f.filename.lower()


def _folder_sort_key(node: dict, column: str):
    if column == "size":
        return node["total_size_bytes"]
    if column == "version":
        # Folders have no version — collapse to 0 so they cluster together
        # when sorted by version and keep their internal name order via the
        # stable sort tiebreaker below.
        return 0
    if column == "modified":
        return node.get("max_modified") or _EPOCH
    return node["name"].lower()


def _build_file_tree(
    project: Project, sort_column: str = "name", sort_direction: str = "asc"
) -> dict:
    """Build a nested tree from the flat ProjectFile list.

    Each node carries: `name`, `path`, `total_size_bytes`, `total_file_count`
    (recursive — only files, not folders), `max_modified`, and `children` —
    a unified list of sibling folders + files already sorted by the
    requested column/direction so the template just renders in order
    (folders and files mix together, the way macOS Finder's list view does).
    Each child entry is a dict `{"kind": "folder"|"file", ...}`.
    """
    root = {
        "name": "",
        "path": "",
        "_folders": {},
        "_files": [],
        "total_size_bytes": 0,
        "total_file_count": 0,
        "max_modified": None,
    }
    for f in project.files:
        node = root
        if f.path:
            for seg in f.path.split("/"):
                child = node["_folders"].get(seg)
                if child is None:
                    full = f"{node['path']}/{seg}" if node["path"] else seg
                    child = {
                        "name": seg,
                        "path": full,
                        "_folders": {},
                        "_files": [],
                        "total_size_bytes": 0,
                        "total_file_count": 0,
                        "max_modified": None,
                    }
                    node["_folders"][seg] = child
                node = child
        node["_files"].append(f)

    reverse = sort_direction == "desc"

    def _finalize(node: dict) -> tuple[int, datetime | None, int]:
        """Recurse: roll up size, file count, max_modified. Sort this level."""
        total_size = 0
        total_files = 0
        max_modified: datetime | None = None
        for f in node["_files"]:
            total_files += 1
            if f.current_version is not None:
                total_size += f.current_version.size_bytes
                ts = f.current_version.uploaded_at
                if max_modified is None or ts > max_modified:
                    max_modified = ts
        for child in node["_folders"].values():
            child_size, child_modified, child_files = _finalize(child)
            total_size += child_size
            total_files += child_files
            if child_modified is not None:
                if max_modified is None or child_modified > max_modified:
                    max_modified = child_modified
        node["total_size_bytes"] = total_size
        node["total_file_count"] = total_files
        node["max_modified"] = max_modified

        # Merge folders + files into a single list and sort as one. Folders
        # and files mingle by whatever sort key is active — a folder named
        # "zzz" with an old newest-file drops to the bottom just like a
        # file would, instead of being pinned above files the way a
        # "folders first" grouping would force.
        combined: list[dict] = []
        for child_folder in node["_folders"].values():
            combined.append(
                {
                    "kind": "folder",
                    "folder": child_folder,
                    "_sort_key": _folder_sort_key(child_folder, sort_column),
                }
            )
        for f in node["_files"]:
            combined.append(
                {
                    "kind": "file",
                    "file": f,
                    "_sort_key": _file_sort_key(f, sort_column),
                }
            )
        combined.sort(key=lambda e: e["_sort_key"], reverse=reverse)
        node["children"] = combined
        node["child_count"] = len(combined)
        return total_size, max_modified, total_files

    _finalize(root)
    return root


# ---------- gallery tab ---------- #
#
# Images are not a separate model — they're ProjectFile rows whose current
# version's mime_type starts with "image/". The gallery is a filter view on
# top of the same data the Files tab shows.


@router.get("/u/{username}/{slug}/gallery")
async def gallery_tab(
    username: str,
    slug: str,
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)

    visible_images: list[ProjectFile] = []
    hidden_images: list[ProjectFile] = []
    for f in project.files:
        if f.current_version is None or not f.current_version.is_image:
            continue
        if f.show_in_gallery:
            visible_images.append(f)
        elif is_owner:
            hidden_images.append(f)
    return templates.TemplateResponse(
        request,
        "projects/gallery.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "visible_images": visible_images,
            "hidden_images": hidden_images,
        },
    )


# ---------- files tab (browser) ---------- #


@router.get("/u/{username}/{slug}/files")
async def files_tab(
    username: str,
    slug: str,
    request: Request,
    sort: str | None = None,
    dir: str | None = None,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    sort_column, sort_direction = _normalize_sort(sort, dir)
    tree = _build_file_tree(project, sort_column, sort_direction)
    has_folders = any(child["kind"] == "folder" for child in tree["children"])
    return templates.TemplateResponse(
        request,
        "projects/files.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "tree": tree,
            "has_folders": has_folders,
            "sort_column": sort_column,
            "sort_direction": sort_direction,
        },
    )


# ---------- create (upload) ---------- #
#
# Literal `/new` MUST come before `{file_id}` routes to avoid FastAPI
# 422-ing on UUID coercion of "new".


@router.get("/u/{username}/{slug}/files/new")
async def new_file_form(
    username: str,
    slug: str,
    request: Request,
    path: str = "",
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    return templates.TemplateResponse(
        request,
        "files/form.html",
        {
            "user": user,
            "project": project,
            "file": None,
            "form_values": _form_values(path=path),
            "max_upload_size": settings.max_upload_size,
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/files")
async def upload_file(
    username: str,
    slug: str,
    request: Request,
    path: str = Form(""),
    description: str = Form(""),
    upload: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)

    description = description.strip()
    json_mode = _wants_json(request)

    def fail(msg: str, *, values: dict | None = None, status: int = 400):
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        return templates.TemplateResponse(
            request,
            "files/form.html",
            {
                "user": user,
                "project": project,
                "file": None,
                "form_values": values or _form_values(path=path, description=description),
                "max_upload_size": settings.max_upload_size,
                "error": msg,
            },
            status_code=status,
        )

    try:
        normalized_path = normalize_virtual_path(path)
    except ValueError as e:
        return fail(str(e))

    if not upload.filename:
        return fail("Please choose a file to upload.")
    try:
        filename = safe_filename(upload.filename)
    except ValueError as e:
        return fail(str(e))

    declared_mime = (
        upload.content_type
        or _content_type_from_filename(filename)
    )

    # Reject before storing if the file is over the configured cap.
    # `upload.size` is set by Starlette from Content-Length.
    if upload.size is not None and upload.size > settings.max_upload_size:
        return fail(
            f"File is too large (max {settings.max_upload_size // (1024 * 1024)} MB)."
        )

    storage = get_storage()

    # If a file with this (path, filename) already exists, this upload becomes
    # a new version instead of a second file row. Owner-friendly default —
    # rename via the edit form to keep both copies.
    existing = await get_existing_file(
        db, project.id, normalized_path, filename
    )
    if existing is None:
        new_file = ProjectFile(
            project_id=project.id,
            path=normalized_path,
            filename=filename,
            description=description or None,
        )
        db.add(new_file)
        await db.flush()  # populate new_file.id for the storage path
        target_file = new_file
        version_number = 1
    else:
        target_file = existing
        version_number = await next_version_number(db, existing.id)
        if description:
            target_file.description = description

    upload.file.seek(0)
    blob: StoredBlob = await store_upload(
        storage,
        file_id=target_file.id,
        version_number=version_number,
        source=upload.file,
        declared_mime=declared_mime,
    )

    version = FileVersion(
        file_id=target_file.id,
        version_number=version_number,
        storage_path=blob.storage_path,
        original_name=upload.filename,
        size_bytes=blob.size_bytes,
        mime_type=blob.detected_mime or declared_mime,
        checksum=blob.checksum,
        width=blob.width,
        height=blob.height,
        thumbnail_path=blob.thumbnail_path,
    )
    db.add(version)
    await db.flush()
    target_file.current_version_id = version.id
    await db.commit()

    if json_mode:
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{target_file.id}",
        status_code=302,
    )


# ---------- zip download (any viewer) ---------- #


def _zip_filename(slug: str, folder_path: str) -> str:
    """User-friendly zip name. The `-files` infix leaves `{slug}.zip`
    reserved for the future whole-project export (which will include
    metadata too, not just the file blobs)."""
    if not folder_path:
        return f"{slug}-files.zip"
    safe = folder_path.replace("/", "-")
    return f"{slug}-files-{safe}.zip"


def _build_zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Sync zip build — run in a thread to keep the event loop free."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in members:
            zf.writestr(arcname, content)
    return buf.getvalue()


@router.get("/u/{username}/{slug}/files/download-zip")
async def download_zip(
    username: str,
    slug: str,
    path: str = "",
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream a zip of the latest version of every file under `path`
    (or the whole project when `path` is empty). Same visibility gate as
    individual file downloads — public projects serve to anyone, private
    only to the owner.

    Folder downloads flatten the folder's own segment out of the zip, so
    `models/widgets/*` opens as `widgets/...` — the downloaded zip is the
    folder's contents, not the folder path from the project root.
    """
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)

    try:
        folder_path = normalize_virtual_path(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    query = (
        select(ProjectFile)
        .options(selectinload(ProjectFile.current_version))
        .where(ProjectFile.project_id == project.id)
    )
    if folder_path:
        query = query.where(
            or_(
                ProjectFile.path == folder_path,
                ProjectFile.path.startswith(f"{folder_path}/"),
            )
        )
    result = await db.execute(query)
    files = list(result.scalars().all())
    if not files:
        raise HTTPException(status_code=404, detail="No files to download.")

    storage = get_storage()
    members: list[tuple[str, bytes]] = []
    seen_names: set[str] = set()
    for f in files:
        if f.current_version is None:
            continue
        arcname = f"{f.path}/{f.filename}" if f.path else f.filename
        # For folder downloads, strip the folder's own prefix so the zip
        # opens with that folder's contents at the top.
        if folder_path:
            prefix = f"{folder_path}/"
            if arcname.startswith(prefix):
                arcname = arcname[len(prefix):]
        # Guard against accidental duplicates from a flattening edge case.
        if arcname in seen_names:
            continue
        seen_names.add(arcname)
        try:
            content = await storage.read(f.current_version.storage_path)
        except (FileNotFoundError, ValueError):
            continue
        members.append((arcname, content))

    if not members:
        raise HTTPException(status_code=404, detail="No files to download.")

    zip_bytes = await to_thread.run_sync(_build_zip_bytes, members)
    zip_name = _zip_filename(project.slug, folder_path)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{zip_name}"; '
                f"filename*=UTF-8''{quote(zip_name)}"
            ),
        },
    )


# ---------- move (drag-and-drop, owner) ---------- #


@router.post("/u/{username}/{slug}/files/move")
async def move_item(
    username: str,
    slug: str,
    source_kind: str = Form(...),
    source_id: str = Form(""),
    source_path: str = Form(""),
    destination_path: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Drag-and-drop move. `source_kind` is "file" or "folder".

    For files, `source_id` is the file UUID and `destination_path` is the
    new containing folder (empty string = root). For folders, `source_path`
    is the folder being moved; the destination folder's `path` + the
    source's basename becomes the new location, so a folder keeps its
    name when moved. Collision and self-descendant checks keep the tree
    consistent.

    Returns 204 on success; 4xx with a terse JSON detail on validation
    errors so the client can show a toast.
    """
    project = await _require_owned_project(db, user, username, slug)
    try:
        dest_path = normalize_virtual_path(destination_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if source_kind == "file":
        try:
            fid = uuid.UUID(source_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid source id.")
        file = await get_file_by_id(db, project.id, fid)
        if file is None:
            raise HTTPException(status_code=404)
        if file.path == dest_path:
            return Response(status_code=204)
        clash = await get_existing_file(db, project.id, dest_path, file.filename)
        if clash is not None and clash.id != file.id:
            raise HTTPException(
                status_code=409,
                detail=f"'{file.filename}' already exists in that folder.",
            )
        file.path = dest_path
        await db.commit()
        return Response(status_code=204)

    if source_kind == "folder":
        try:
            src_path = normalize_virtual_path(source_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not src_path:
            raise HTTPException(status_code=400, detail="Source folder required.")
        # Can't move a folder into itself or a descendant of itself.
        if dest_path == src_path or dest_path.startswith(src_path + "/"):
            raise HTTPException(
                status_code=400,
                detail="Can't move a folder into itself.",
            )
        basename = src_path.rsplit("/", 1)[-1]
        new_path = f"{dest_path}/{basename}" if dest_path else basename
        if new_path == src_path:
            return Response(status_code=204)
        try:
            await rename_folder(db, project.id, src_path, new_path)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        await db.commit()
        return Response(status_code=204)

    raise HTTPException(status_code=400, detail="Invalid source kind.")


# ---------- folder edit (owner) ---------- #
#
# Folders are virtual — they're just the `path` column on each ProjectFile.
# "Editing" a folder means rewriting the path prefix on every descendant.
# These routes are declared BEFORE the `{file_id}` routes so FastAPI
# doesn't try to coerce "folder" to a UUID.


@router.get("/u/{username}/{slug}/files/folder/edit")
async def edit_folder_form(
    username: str,
    slug: str,
    request: Request,
    path: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    try:
        normalized = normalize_virtual_path(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not normalized:
        raise HTTPException(status_code=400, detail="Folder path is required.")
    files = await list_files_in_folder(db, project.id, normalized)
    if not files:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "files/folder_form.html",
        {
            "user": user,
            "project": project,
            "folder_path": normalized,
            "file_count": len(files),
            "form_values": {"new_path": normalized},
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/files/folder/rename")
async def rename_folder_route(
    username: str,
    slug: str,
    request: Request,
    old_path: str = Form(...),
    new_path: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    try:
        normalized_old = normalize_virtual_path(old_path)
        normalized_new = normalize_virtual_path(new_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not normalized_old:
        raise HTTPException(status_code=400, detail="Original folder path is required.")

    json_mode = _wants_json(request)

    async def fail(msg: str, *, status: int = 400):
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        files = await list_files_in_folder(db, project.id, normalized_old)
        return templates.TemplateResponse(
            request,
            "files/folder_form.html",
            {
                "user": user,
                "project": project,
                "folder_path": normalized_old,
                "file_count": len(files),
                "form_values": {"new_path": new_path},
                "error": msg,
            },
            status_code=status,
        )

    if not normalized_new:
        return await fail("New folder path is required.")
    if normalized_new == normalized_old:
        if json_mode:
            return Response(status_code=204)
        return RedirectResponse(
            f"/u/{user.username}/{project.slug}/files", status_code=302
        )
    try:
        await rename_folder(db, project.id, normalized_old, normalized_new)
    except ValueError as e:
        return await fail(str(e), status=409)
    await db.commit()
    if json_mode:
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files",
        status_code=302,
    )


@router.post("/u/{username}/{slug}/files/folder/delete")
async def delete_folder_route(
    username: str,
    slug: str,
    path: str = Form(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    try:
        normalized = normalize_virtual_path(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not normalized:
        raise HTTPException(status_code=400, detail="Folder path is required.")
    storage = get_storage()
    await delete_folder(db, storage, project.id, normalized)
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files",
        status_code=302,
    )


# ---------- detail ---------- #


@router.get("/u/{username}/{slug}/files/{file_id}")
async def file_detail(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    kind = "none"
    text_preview: str | None = None
    text_truncated = False
    if file.current_version is not None:
        kind = preview_kind(file.current_version.mime_type, file.filename)
        if kind == "text":
            try:
                text_preview, text_truncated = await read_text_preview(
                    get_storage(), file.current_version.storage_path
                )
            except (FileNotFoundError, ValueError):
                text_preview = None

    return templates.TemplateResponse(
        request,
        "files/detail.html",
        {
            "user": user,
            "project": project,
            "file": file,
            "is_owner": is_owner,
            "preview_kind": kind,
            "text_preview": text_preview,
            "text_truncated": text_truncated,
        },
    )


# ---------- download / thumbnail ---------- #


@router.get("/u/{username}/{slug}/files/{file_id}/download")
async def download_file(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    v: int | None = None,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    if v is None:
        version = file.current_version
    else:
        version = next(
            (ver for ver in file.versions if ver.version_number == v), None
        )
    if version is None:
        raise HTTPException(status_code=404)

    storage = get_storage()
    full = storage.full_path(version.storage_path)
    encoded = quote(file.filename)
    return FileResponse(
        full,
        media_type=version.mime_type or "application/octet-stream",
        headers={
            # filename* uses RFC 5987 encoding so non-ASCII names round-trip.
            "Content-Disposition": (
                f"attachment; filename=\"{file.filename}\"; filename*=UTF-8''{encoded}"
            ),
        },
    )


@router.get("/u/{username}/{slug}/files/{file_id}/thumb")
async def file_thumbnail(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    user: User | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await get_project_by_username_and_slug(db, username, slug)
    if project is None:
        raise HTTPException(status_code=404)
    is_owner = user is not None and project.user_id == user.id
    if not is_owner and not project.is_public:
        raise HTTPException(status_code=404)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None or file.current_version is None:
        raise HTTPException(status_code=404)
    if not file.current_version.thumbnail_path:
        raise HTTPException(status_code=404)
    storage = get_storage()
    return FileResponse(
        storage.full_path(file.current_version.thumbnail_path),
        media_type="image/webp",
    )


# ---------- new version (owner) ---------- #


@router.post("/u/{username}/{slug}/files/{file_id}/version")
async def upload_new_version(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    changelog: str = Form(""),
    upload: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    if not upload.filename:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    if upload.size is not None and upload.size > settings.max_upload_size:
        raise HTTPException(status_code=413, detail="File too large.")

    declared_mime = (
        upload.content_type
        or _content_type_from_filename(upload.filename)
    )

    storage = get_storage()
    version_number = await next_version_number(db, file.id)
    upload.file.seek(0)
    blob = await store_upload(
        storage,
        file_id=file.id,
        version_number=version_number,
        source=upload.file,
        declared_mime=declared_mime,
    )

    version = FileVersion(
        file_id=file.id,
        version_number=version_number,
        storage_path=blob.storage_path,
        original_name=upload.filename,
        size_bytes=blob.size_bytes,
        mime_type=blob.detected_mime or declared_mime,
        checksum=blob.checksum,
        changelog=changelog.strip() or None,
        width=blob.width,
        height=blob.height,
        thumbnail_path=blob.thumbnail_path,
    )
    db.add(version)
    await db.flush()
    file.current_version_id = version.id
    await db.commit()

    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


# ---------- edit metadata ---------- #


@router.get("/u/{username}/{slug}/files/{file_id}/edit")
async def edit_file_form(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "files/form.html",
        {
            "user": user,
            "project": project,
            "file": file,
            "form_values": {
                "path": file.path,
                "filename": file.filename,
                "description": file.description or "",
            },
            "max_upload_size": settings.max_upload_size,
            "error": None,
        },
    )


@router.post("/u/{username}/{slug}/files/{file_id}")
async def update_file_metadata(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    path: str = Form(""),
    filename: str = Form(""),
    description: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    description = description.strip()
    json_mode = _wants_json(request)

    def fail(msg: str, *, status: int = 400):
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        return templates.TemplateResponse(
            request,
            "files/form.html",
            {
                "user": user,
                "project": project,
                "file": file,
                "form_values": {
                    "path": path,
                    "filename": filename,
                    "description": description,
                },
                "max_upload_size": settings.max_upload_size,
                "error": msg,
            },
            status_code=status,
        )

    try:
        normalized_path = normalize_virtual_path(path)
    except ValueError as e:
        return fail(str(e))
    try:
        new_filename = safe_filename(filename or file.filename)
    except ValueError as e:
        return fail(str(e))

    # Block a rename onto another file in the same project.
    if (normalized_path, new_filename) != (file.path, file.filename):
        clash = await get_existing_file(db, project.id, normalized_path, new_filename)
        if clash is not None and clash.id != file.id:
            return fail(
                f"A file named '{new_filename}' already exists in that folder.",
                status=409,
            )

    file.path = normalized_path
    file.filename = new_filename
    file.description = description or None
    await db.commit()

    if json_mode:
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


# ---------- show/hide in gallery (owner) ---------- #


def _safe_local_redirect(target: str | None, fallback: str) -> str:
    """Accept a submitted redirect target only if it's a same-origin path.

    Blocks protocol-relative (`//evil.example`) and absolute URLs so the
    `next` hidden field can't be used for open-redirect. Everything else
    falls back to the default route-specific target.
    """
    if not target:
        return fallback
    if not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


@router.post("/u/{username}/{slug}/files/{file_id}/gallery-visibility")
async def toggle_gallery_visibility(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    next_path: str = Form("", alias="next"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Flip the per-file `show_in_gallery` flag.

    Owners use this to curate the Gallery tab — hide test shots or working
    photos while keeping them in the Files browser. If the hidden file was
    the project cover, clear the cover too (otherwise guests see a cover
    image that no longer appears in the gallery, which is confusing).

    The `next` form field lets callers stay on their current page instead
    of bouncing to the file detail view — the gallery grid uses this so a
    quick Hide/Show click keeps the user in the gallery.
    """
    project = await _require_owned_project(db, user, username, slug)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)
    file.show_in_gallery = not file.show_in_gallery
    if not file.show_in_gallery and project.cover_file_id == file.id:
        project.cover_file_id = None
    await db.commit()
    fallback = f"/u/{user.username}/{project.slug}/files/{file.id}"
    return RedirectResponse(
        _safe_local_redirect(next_path, fallback),
        status_code=302,
    )


# ---------- cover image (owner) ---------- #


@router.post("/u/{username}/{slug}/files/{file_id}/cover")
async def set_cover_image(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)
    # Toggle: if this file is already the cover, clear it instead.
    if project.cover_file_id == file.id:
        project.cover_file_id = None
    else:
        if file.current_version is None or not file.current_version.is_image:
            raise HTTPException(
                status_code=400, detail="Only image files can be set as cover."
            )
        project.cover_file_id = file.id
    await db.commit()
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


# ---------- delete ---------- #


@router.post("/u/{username}/{slug}/files/{file_id}/delete")
async def delete_file(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    storage = get_storage()
    versions = list(file.versions)

    # Drop the cover-image FK first if needed; the SET NULL handles the DB
    # side, but doing it explicitly avoids a deferred-FK round-trip.
    if project.cover_file_id == file.id:
        project.cover_file_id = None
    # Null out current_version_id so SQLAlchemy doesn't trip over the
    # circular FK during the cascade delete.
    file.current_version_id = None
    await db.flush()

    await db.delete(file)
    await db.commit()

    # Best-effort blob cleanup. If a delete fails (file gone, permission),
    # we don't fail the request — the DB is the source of truth.
    for v in versions:
        await delete_blob(storage, v)

    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files",
        status_code=302,
    )
