"""Routes for project files — browser, upload, detail, download, version, edit, delete.

URL scheme matches journal/links: `/u/{username}/{slug}/files/...`. Visibility
inherits the parent project (no per-file flag, like links). Mutation routes
are owner-only with the same `_require_owned_project` pattern.

Route ordering matters: literal-suffix routes (`/files/move`, `/files/folder/*`,
`/files/download-zip`) declared BEFORE `{file_id}`-parameterized routes,
otherwise FastAPI matches the literal as a UUID param and 422s.
"""

import mimetypes
import os
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.background import BackgroundTask

from benchlog.activity import (
    purge_file_events,
    purge_file_version_events,
    record_event,
)
from benchlog.config import settings
from benchlog.database import get_db
from benchlog.dependencies import current_user, require_user
from benchlog.files import (
    StoredBlob,
    UploadTooLarge,
    apply_file_rename_to_project_markdown,
    apply_folder_rename_to_project_markdown,
    code_language,
    copy_blob,
    delete_blob,
    delete_folder,
    get_existing_file,
    get_file_by_id,
    highlight_code,
    next_version_number,
    normalize_virtual_path,
    preview_kind,
    read_text_preview,
    regenerate_thumbnail_from_storage,
    rename_folder,
    safe_filename,
    store_upload,
)
from benchlog.models import ActivityEventType, FileVersion, Project, ProjectFile, User
from benchlog.projects import (
    get_project_by_username_and_slug,
    get_user_project_by_slug,
)
from benchlog.routes.projects import load_project_header_ctx
from benchlog.storage import get_storage
from benchlog.templating import templates

router = APIRouter()


# ---------- owner helper (same pattern as links/journal) ---------- #


async def _require_owned_project(
    db: AsyncSession, user: User, username: str, slug: str
) -> Project:
    if username.lower() != user.username.lower():
        raise HTTPException(status_code=404)
    project = await get_user_project_by_slug(db, user.id, slug)
    if project is None:
        raise HTTPException(status_code=404)
    return project


async def _load_project_with_journal(
    db: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Reload a project with `journal_entries` eager-loaded so rename-tracking
    helpers don't trip `raise_on_sql` when they walk
    `project.journal_entries`.

    Kept local to this module — the file-rename markdown rewrite is the
    only caller that needs journal entries eager-loaded without the full
    tag/link/file bundle that `get_project_by_username_and_slug` pulls.
    """
    from sqlalchemy import select as _select

    result = await db.execute(
        _select(Project)
        .options(selectinload(Project.journal_entries))
        .where(Project.id == project_id)
    )
    return result.scalar_one_or_none()


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

    Used by mutation routes that double as progressive-enhancement
    endpoints: the client submits via fetch with `Accept: application/json`
    and gets either a 204 (edit/rename/delete/restore modals where there's
    nothing new to convey on success) or a 200 with a JSON body (the
    `/cover` and `/gallery-visibility` branches, which return the refreshed
    `{is_cover, show_in_gallery}` state so the lightbox can update in
    place). Errors are always JSON either way. The fallback HTML form path
    uses the usual redirect/re-render flow.
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
    header_ctx = await load_project_header_ctx(db, user, project)
    return templates.TemplateResponse(
        request,
        "projects/gallery.html",
        {
            "user": user,
            "project": project,
            "is_owner": is_owner,
            "visible_images": visible_images,
            "hidden_images": hidden_images,
            **header_ctx,
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
    # Total storage footprint = sum of every FileVersion blob (not just the
    # current version). Old versions still occupy disk, so the aggregate is
    # the number that answers "how big is this project on disk today".
    total_file_count = sum(1 for _ in project.files)
    total_storage_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(FileVersion.size_bytes), 0))
            .join(ProjectFile, ProjectFile.id == FileVersion.file_id)
            .where(ProjectFile.project_id == project.id)
        )
    ).scalar_one()
    header_ctx = await load_project_header_ctx(db, user, project)
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
            "total_file_count": total_file_count,
            "total_storage_bytes": int(total_storage_bytes or 0),
            "notice": request.session.pop("flash_notice", None),
            "error": request.session.pop("flash_error", None),
            **header_ctx,
        },
    )


# ---------- create (upload) ---------- #


@router.post("/u/{username}/{slug}/files")
async def upload_file(
    username: str,
    slug: str,
    request: Request,
    path: str = Form(""),
    description: str = Form(""),
    show_in_gallery: str = Form(""),
    upload: UploadFile = File(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _require_owned_project(db, user, username, slug)

    description = description.strip()
    json_mode = _wants_json(request)
    # `show_in_gallery` defaults True on the model; only opt-outs ("0") need
    # a client override. The Gallery tab passes "1" for symmetry, which is
    # equivalent to the default — either way, any "truthy" value keeps it
    # visible, and only an explicit "0" hides it on creation.
    gallery_opt_out = show_in_gallery == "0"

    def fail(msg: str, *, status: int = 400):
        # All upload flows go through fetch + drop zones now; there's no
        # HTML form to re-render. json_mode requests get HTTPException so
        # FastAPI produces the usual JSON error envelope; non-JSON posts
        # (still valid for programmatic use) get a matching JSON body.
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        return JSONResponse({"detail": msg}, status_code=status)

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
    is_new_file = existing is None
    if is_new_file:
        new_file = ProjectFile(
            project_id=project.id,
            path=normalized_path,
            filename=filename,
            description=description or None,
            show_in_gallery=not gallery_opt_out,
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
    try:
        blob: StoredBlob = await store_upload(
            storage,
            file_id=target_file.id,
            version_number=version_number,
            source=upload.file,
            declared_mime=declared_mime,
            max_bytes=settings.max_upload_size,
        )
    except UploadTooLarge:
        return fail(
            f"File is too large (max {settings.max_upload_size // (1024 * 1024)} MB).",
            status=413,
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
    if is_new_file:
        await record_event(
            db,
            actor=user,
            project=project,
            event_type=ActivityEventType.file_uploaded,
            payload={
                "file_id": str(target_file.id),
                "filename": target_file.filename,
            },
        )
    else:
        await record_event(
            db,
            actor=user,
            project=project,
            event_type=ActivityEventType.file_version_added,
            payload={
                "file_id": str(target_file.id),
                "version_number": version_number,
            },
        )
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


_ZIP_READ_CHUNK = 64 * 1024


def _build_zip_tempfile(members: list[tuple[str, str]]) -> str:
    """Stream sources into a zip on disk and return the temp path.

    `members` is (arcname, absolute_source_path). Streaming keeps memory use
    bounded regardless of project size — neither the source files nor the
    zip itself are fully resident in RAM. Caller is responsible for deleting
    the returned path.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="benchlog-zip-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, source_path in members:
                with (
                    open(source_path, "rb") as src,
                    zf.open(arcname, "w", force_zip64=True) as dst,
                ):
                    while chunk := src.read(_ZIP_READ_CHUNK):
                        dst.write(chunk)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


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
    members: list[tuple[str, str]] = []
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
        try:
            source_path = storage.full_path(f.current_version.storage_path)
        except ValueError:
            continue
        if not source_path.is_file():
            continue
        seen_names.add(arcname)
        members.append((arcname, str(source_path)))

    if not members:
        raise HTTPException(status_code=404, detail="No files to download.")

    tmp_path = await to_thread.run_sync(_build_zip_tempfile, members)
    zip_name = _zip_filename(project.slug, folder_path)

    def _cleanup() -> None:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{zip_name}"; '
                f"filename*=UTF-8''{quote(zip_name)}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(_cleanup),
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
        old_full_path = (
            f"{file.path}/{file.filename}" if file.path else file.filename
        )
        new_full_path = (
            f"{dest_path}/{file.filename}" if dest_path else file.filename
        )
        file.path = dest_path
        await db.commit()
        # DnD has no form, so there's no opt-out — moves always keep
        # markdown refs pointing at the new location.
        project_with_journal = await _load_project_with_journal(db, project.id)
        if project_with_journal is not None:
            await apply_file_rename_to_project_markdown(
                db, project_with_journal, old_full_path, new_full_path
            )
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
        project_with_journal = await _load_project_with_journal(db, project.id)
        if project_with_journal is not None:
            await apply_folder_rename_to_project_markdown(
                db, project_with_journal, src_path, new_path
            )
        return Response(status_code=204)

    raise HTTPException(status_code=400, detail="Invalid source kind.")


# ---------- folder edit (owner) ---------- #
#
# Folders are virtual — they're just the `path` column on each ProjectFile.
# "Editing" a folder means rewriting the path prefix on every descendant.
# These routes are declared BEFORE the `{file_id}` routes so FastAPI
# doesn't try to coerce "folder" to a UUID.


@router.post("/u/{username}/{slug}/files/folder/rename")
async def rename_folder_route(
    username: str,
    slug: str,
    request: Request,
    old_path: str = Form(...),
    new_path: str = Form(""),
    update_refs: str = Form(""),
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

    def fail(msg: str, *, status: int = 400):
        # Folder rename is only invoked from the inline modal (fetch
        # + Accept: json) now. Non-JSON callers still get a matching JSON
        # error envelope so the response is usable programmatically.
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        return JSONResponse({"detail": msg}, status_code=status)

    if not normalized_new:
        return fail("New folder path is required.")
    if normalized_new == normalized_old:
        if json_mode:
            return Response(status_code=204)
        return RedirectResponse(
            f"/u/{user.username}/{project.slug}/files", status_code=302
        )
    try:
        await rename_folder(db, project.id, normalized_old, normalized_new)
    except ValueError as e:
        return fail(str(e), status=409)
    await db.commit()

    ref_count = 0
    if update_refs == "1":
        project_with_journal = await _load_project_with_journal(db, project.id)
        if project_with_journal is not None:
            ref_count = await apply_folder_rename_to_project_markdown(
                db, project_with_journal, normalized_old, normalized_new
            )

    if json_mode:
        return Response(status_code=204)
    if update_refs == "1" and ref_count:
        request.session["flash_notice"] = (
            f"Folder renamed. Updated {ref_count} markdown "
            f"reference{'s' if ref_count != 1 else ''}."
        )
    else:
        request.session["flash_notice"] = "Folder renamed."
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
    return await _render_file_detail(
        request,
        user,
        project,
        file,
        is_owner,
        notice=request.session.pop("flash_notice", None),
        error=request.session.pop("flash_error", None),
        db=db,
    )


async def _render_file_detail(
    request: Request,
    user: User | None,
    project: Project,
    file: ProjectFile,
    is_owner: bool,
    *,
    error: str | None = None,
    status_code: int = 200,
    notice: str | None = None,
    db: AsyncSession | None = None,
):
    """Factored out of `file_detail` so mutation routes can re-render with a
    flash-style error on validation failure (matches form-fallback pattern
    used by upload/edit routes)."""
    kind = "none"
    text_preview: str | None = None
    text_truncated = False
    code_html: str | None = None
    language: str | None = None
    if file.current_version is not None:
        kind = preview_kind(file.current_version.mime_type, file.filename)
        if kind in {"text", "code"}:
            try:
                text_preview, text_truncated = await read_text_preview(
                    get_storage(), file.current_version.storage_path
                )
            except (FileNotFoundError, ValueError):
                text_preview = None
            if kind == "code" and text_preview is not None:
                language = code_language(file.filename)
                if language is not None:
                    code_html = highlight_code(text_preview, language)
                else:
                    # Defensive: preview_kind said "code" but the lexer
                    # lookup came back empty — treat as plain text.
                    kind = "text"

    # Shared header context (category breadcrumbs + viewer's collections +
    # membership set). Error re-renders from mutation routes pass `db=None`
    # and fall back to empty values; those responses use 4xx status codes
    # so the mis-rendered picker on a validation failure page is an
    # acceptable cost for keeping the signature light.
    header_ctx: dict = {
        "viewer_collections": [],
        "project_collection_ids": set(),
        "category_breadcrumbs": {},
        "status_chip_options": [],
        "known_tags": [],
        "known_categories": [],
    }
    if db is not None:
        header_ctx = await load_project_header_ctx(db, user, project)

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
            "code_html": code_html,
            "language": language,
            "error": error,
            "notice": notice,
            **header_ctx,
        },
        status_code=status_code,
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
            # The stored mime_type originates from the uploader's browser —
            # don't let old clients re-sniff an HTML payload into rendering.
            "X-Content-Type-Options": "nosniff",
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
        headers={"X-Content-Type-Options": "nosniff"},
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
    try:
        blob = await store_upload(
            storage,
            file_id=file.id,
            version_number=version_number,
            source=upload.file,
            declared_mime=declared_mime,
            max_bytes=settings.max_upload_size,
        )
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="File too large.")

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
    await record_event(
        db,
        actor=user,
        project=project,
        event_type=ActivityEventType.file_version_added,
        payload={
            "file_id": str(file.id),
            "version_number": version_number,
        },
    )
    await db.commit()

    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


# ---------- delete / restore individual version (owner) ---------- #
#
# These are nested under `{file_id}/version/{version_number}/...`. `version`
# is a literal segment here, so it never collides with `{file_id}` — but
# it still has to live BEFORE the catch-all edit POST (`{file_id}`) since
# FastAPI's router doesn't reorder.


@router.post("/u/{username}/{slug}/files/{file_id}/version/{version_number}/delete")
async def delete_file_version(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    version_number: int,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a single FileVersion row + its blob + thumbnail.

    Cannot delete the current version while other versions exist — the
    owner has to restore another version first (which promotes it to
    current) before the previously-current one can go. Cannot delete the
    only remaining version either — that's what "Delete file" is for.
    """
    await _require_owned_project(db, user, username, slug)
    # Re-load through the public lookup so the detail-page re-render path
    # gets every eager-load it needs (tags, journal, files, etc.) without
    # a second owner check.
    project = await get_project_by_username_and_slug(db, username, slug)
    assert project is not None  # _require_owned_project guarantees existence
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    target = next(
        (v for v in file.versions if v.version_number == version_number), None
    )
    if target is None:
        raise HTTPException(status_code=404)

    json_mode = _wants_json(request)

    async def fail(msg: str, *, status: int = 400):
        if json_mode:
            return JSONResponse({"detail": msg}, status_code=status)
        return await _render_file_detail(
            request, user, project, file, True, error=msg, status_code=status
        )

    if len(file.versions) <= 1:
        return await fail(
            "This is the only version. Use Delete File to remove the entire file."
        )
    if file.current_version_id == target.id:
        return await fail(
            "Can't delete the current version while other versions exist. "
            "Restore another version first to make it current, then delete this one."
        )

    storage = get_storage()
    target_version_number = target.version_number
    await db.delete(target)
    await db.flush()
    await purge_file_version_events(db, file_id, target_version_number)
    await db.commit()

    # Best-effort blob cleanup. Matches the pattern in delete_file — the DB
    # is the source of truth; a missing blob on disk never fails the request.
    await delete_blob(storage, target)

    if json_mode:
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


@router.post("/u/{username}/{slug}/files/{file_id}/version/{version_number}/edit")
async def edit_file_version_changelog(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    version_number: int,
    request: Request,
    changelog: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Rewrite the "what changed" note on any existing version.

    Owner-only. Empty string clears the note. Drag-drop uploads bypass
    the changelog form entirely, so this is the post-hoc way to fill in
    context on a version that was added without one.
    """
    project = await _require_owned_project(db, user, username, slug)
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)
    target = next(
        (v for v in file.versions if v.version_number == version_number), None
    )
    if target is None:
        raise HTTPException(status_code=404)

    target.changelog = changelog.strip() or None
    await db.commit()

    if _wants_json(request):
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


@router.post("/u/{username}/{slug}/files/{file_id}/version/{version_number}/restore")
async def restore_file_version(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    version_number: int,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote an older version to current by creating a new version whose
    blob is a fresh copy of the source. The original row stays intact —
    only `current_version_id` moves. Version numbers never renumber, so a
    gap left by a prior delete is preserved.
    """
    await _require_owned_project(db, user, username, slug)
    project = await get_project_by_username_and_slug(db, username, slug)
    assert project is not None
    file = await _load_file_with_versions(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    source = next(
        (v for v in file.versions if v.version_number == version_number), None
    )
    if source is None:
        raise HTTPException(status_code=404)

    json_mode = _wants_json(request)

    async def fail(msg: str, *, status: int = 400):
        if json_mode:
            return JSONResponse({"detail": msg}, status_code=status)
        return await _render_file_detail(
            request, user, project, file, True, error=msg, status_code=status
        )

    if file.current_version_id == source.id:
        return await fail("This version is already the latest.")

    storage = get_storage()
    new_version_number = await next_version_number(db, file.id)
    new_storage_path = f"files/{file.id}/{new_version_number}"

    await copy_blob(storage, source.storage_path, new_storage_path)

    new_width: int | None = None
    new_height: int | None = None
    new_thumbnail_path: str | None = None
    # Only regenerate thumbnails for versions that actually had one —
    # otherwise a non-image source would decode-fail and cost us an IO
    # round trip on every restore.
    if source.thumbnail_path or source.is_image:
        new_width, new_height, new_thumbnail_path = (
            await regenerate_thumbnail_from_storage(
                storage,
                file_id=file.id,
                version_number=new_version_number,
                storage_path=new_storage_path,
            )
        )

    new_version = FileVersion(
        file_id=file.id,
        version_number=new_version_number,
        storage_path=new_storage_path,
        original_name=source.original_name,
        size_bytes=source.size_bytes,
        mime_type=source.mime_type,
        checksum=source.checksum,
        changelog=f"Restored from v{source.version_number}",
        width=new_width,
        height=new_height,
        thumbnail_path=new_thumbnail_path,
    )
    db.add(new_version)
    await db.flush()
    file.current_version_id = new_version.id
    await db.commit()

    if json_mode:
        return Response(status_code=204)
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


# ---------- edit metadata ---------- #


@router.post("/u/{username}/{slug}/files/{file_id}")
async def update_file_metadata(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    path: str = Form(""),
    filename: str = Form(""),
    description: str = Form(""),
    update_refs: str = Form(""),
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
        # The file-edit modal is the only UI caller now; non-JSON posts
        # get a matching JSON error envelope for programmatic use.
        if json_mode:
            raise HTTPException(status_code=status, detail=msg)
        return JSONResponse({"detail": msg}, status_code=status)

    try:
        normalized_path = normalize_virtual_path(path)
    except ValueError as e:
        return fail(str(e))
    try:
        new_filename = safe_filename(filename or file.filename)
    except ValueError as e:
        return fail(str(e))

    # Snapshot the pre-rename virtual path before mutating so we can
    # rewrite `files/<old>` references in the project markdown after the
    # rename commits.
    old_full_path = f"{file.path}/{file.filename}" if file.path else file.filename
    new_full_path = (
        f"{normalized_path}/{new_filename}" if normalized_path else new_filename
    )

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

    # Rewrite markdown refs after the rename commits — default on, opt-out
    # via the form checkbox. Only runs when the virtual path actually
    # changed (pure description edits leave markdown alone).
    ref_count = 0
    path_changed = old_full_path != new_full_path
    if path_changed and update_refs == "1":
        project_with_journal = await _load_project_with_journal(db, project.id)
        if project_with_journal is not None:
            ref_count = await apply_file_rename_to_project_markdown(
                db, project_with_journal, old_full_path, new_full_path
            )

    if json_mode:
        return Response(status_code=204)
    if path_changed:
        if update_refs == "1" and ref_count:
            request.session["flash_notice"] = (
                f"File renamed. Updated {ref_count} markdown "
                f"reference{'s' if ref_count != 1 else ''}."
            )
        else:
            request.session["flash_notice"] = "File renamed."
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
    request: Request,
    next_path: str = Form("", alias="next"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Flip the per-file `show_in_gallery` flag.

    Owners use this to curate the Gallery tab — hide test shots or working
    photos while keeping them in the Files browser. If the hidden file was
    the project cover, clear the cover too (otherwise guests see a cover
    image that no longer appears in the gallery, which is confusing).

    Two response modes:
    - HTML form post (default): redirects to `next` if local-safe, else to
      the file detail page. Used by the gallery grid's plain-form button.
    - JSON (Accept: application/json): returns `{is_cover, show_in_gallery}`
      without redirecting. Used by the lightbox so the action stays in-place.
    """
    project = await _require_owned_project(db, user, username, slug)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)
    file.show_in_gallery = not file.show_in_gallery
    if not file.show_in_gallery and project.cover_file_id == file.id:
        project.cover_file_id = None
        _apply_crop(project, None)
    await db.commit()

    if _wants_json(request):
        return {
            "is_cover": project.cover_file_id == file.id,
            "show_in_gallery": file.show_in_gallery,
        }

    fallback = f"/u/{user.username}/{project.slug}/files/{file.id}"
    return RedirectResponse(
        _safe_local_redirect(next_path, fallback),
        status_code=302,
    )


# ---------- cover image (owner) ---------- #

# Accept ratios within 1% of 16:9 — JS math introduces rounding, and we'd
# rather be forgiving about a 0.56 vs 0.5625 mismatch than surface a false
# "wrong aspect" 400 to the user. Clients that care can clamp tighter.
_COVER_ASPECT = 16.0 / 9.0
_COVER_ASPECT_TOLERANCE = 0.01


def _parse_crop_field(raw: str | None) -> float | None:
    """Parse a single crop-coord form value. Returns None for empty/missing
    so the four-together rule below can distinguish "not submitted" from
    "submitted as 0"."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid crop coordinate.")


def _validate_crop(
    x: float | None,
    y: float | None,
    w: float | None,
    h: float | None,
    image_width: int | None = None,
    image_height: int | None = None,
) -> tuple[float, float, float, float] | None:
    """Enforce the crop contract. Returns the four floats when all four are
    present and valid; None when all four are absent (no crop submitted);
    raises 400 on partial or out-of-bounds input.

    `w` and `h` are normalized to image dimensions, so the *image-pixel*
    aspect of the crop is `(w * W) / (h * H)`, not `w / h`. Aspect is only
    enforced when image dims are known (otherwise we trust the cropper JS).
    """
    provided = [v is not None for v in (x, y, w, h)]
    if not any(provided):
        return None
    if not all(provided):
        raise HTTPException(
            status_code=400,
            detail="Cover crop requires all four of x, y, width, height.",
        )
    # All four present by now — narrow for the type checker.
    assert x is not None and y is not None and w is not None and h is not None
    # Each coord in [0, 1]; width + height strictly positive.
    for val in (x, y, w, h):
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400, detail="Crop coordinates must be in [0, 1]."
            )
    if w <= 0.0 or h <= 0.0:
        raise HTTPException(
            status_code=400, detail="Crop width and height must be positive."
        )
    if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
        raise HTTPException(
            status_code=400, detail="Crop rectangle extends past the image edge."
        )
    if image_width and image_height:
        image_aspect = (w * image_width) / (h * image_height)
        if abs(image_aspect - _COVER_ASPECT) > _COVER_ASPECT_TOLERANCE:
            raise HTTPException(
                status_code=400,
                detail="Cover crop must be 16:9.",
            )
    return x, y, w, h


def _apply_crop(project: Project, crop: tuple[float, float, float, float] | None) -> None:
    if crop is None:
        project.cover_crop_x = None
        project.cover_crop_y = None
        project.cover_crop_width = None
        project.cover_crop_height = None
    else:
        x, y, w, h = crop
        project.cover_crop_x = x
        project.cover_crop_y = y
        project.cover_crop_width = w
        project.cover_crop_height = h


async def _read_crop_from_request(
    request: Request,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Pull crop coords from either a form body or a JSON body.

    The /cover and /cover-crop routes both accept either form or JSON —
    fetch-from-JS sends `application/json`, the plain HTML form path sends
    `application/x-www-form-urlencoded`. Missing fields stay None.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON body.")

        def _coerce(v) -> float | None:
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid crop coordinate.")

        return (
            _coerce(payload.get("crop_x")),
            _coerce(payload.get("crop_y")),
            _coerce(payload.get("crop_width")),
            _coerce(payload.get("crop_height")),
        )
    form = await request.form()
    return (
        _parse_crop_field(form.get("crop_x")),
        _parse_crop_field(form.get("crop_y")),
        _parse_crop_field(form.get("crop_width")),
        _parse_crop_field(form.get("crop_height")),
    )


@router.post("/u/{username}/{slug}/files/{file_id}/cover")
async def set_cover_image(
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

    # Always attempt to parse the body so stale forms can't skip validation.
    crop_raw = await _read_crop_from_request(request)
    cv = file.current_version
    crop = _validate_crop(
        *crop_raw,
        image_width=cv.width if cv else None,
        image_height=cv.height if cv else None,
    )
    json_mode = _wants_json(request)

    # Toggle: if this file is already the cover and no crop was supplied,
    # clear it (and any stored crop). A crop in the request means the user
    # is re-cropping, not toggling — preserve the cover and update the crop.
    if project.cover_file_id == file.id and crop is None:
        project.cover_file_id = None
        _apply_crop(project, None)
    else:
        if file.current_version is None or not file.current_version.is_image:
            raise HTTPException(
                status_code=400, detail="Only image files can be set as cover."
            )
        # Setting or changing cover writes the submitted crop (None for the
        # bare toggle path, the validated tuple for the modal path). Either
        # way the previous crop is discarded cleanly.
        project.cover_file_id = file.id
        _apply_crop(project, crop)
    await db.commit()

    if json_mode:
        return {
            "is_cover": project.cover_file_id == file.id,
            "show_in_gallery": file.show_in_gallery,
        }
    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files/{file.id}",
        status_code=302,
    )


@router.post("/u/{username}/{slug}/files/{file_id}/cover-crop")
async def set_cover_crop(
    username: str,
    slug: str,
    file_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Adjust the crop on the already-selected cover image.

    404s if `file_id` isn't the current cover — this route is crop-only,
    not a way to sneak past the cover-image check in /cover.
    """
    project = await _require_owned_project(db, user, username, slug)
    if project.cover_file_id != file_id:
        raise HTTPException(status_code=404)
    file = await get_file_by_id(db, project.id, file_id)
    if file is None:
        raise HTTPException(status_code=404)

    crop_raw = await _read_crop_from_request(request)
    cv = file.current_version
    crop = _validate_crop(
        *crop_raw,
        image_width=cv.width if cv else None,
        image_height=cv.height if cv else None,
    )
    _apply_crop(project, crop)
    await db.commit()

    if _wants_json(request):
        return Response(status_code=204)
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
    # side, but doing it explicitly avoids a deferred-FK round-trip. Crop
    # coordinates are meaningless without the cover so clear them too.
    if project.cover_file_id == file.id:
        project.cover_file_id = None
        _apply_crop(project, None)
    # Null out current_version_id so SQLAlchemy doesn't trip over the
    # circular FK during the cascade delete.
    file.current_version_id = None
    await db.flush()

    await db.delete(file)
    await db.flush()
    await purge_file_events(db, file_id)
    await db.commit()

    # Best-effort blob cleanup. If a delete fails (file gone, permission),
    # we don't fail the request — the DB is the source of truth.
    for v in versions:
        await delete_blob(storage, v)

    return RedirectResponse(
        f"/u/{user.username}/{project.slug}/files",
        status_code=302,
    )
