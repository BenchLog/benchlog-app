import uuid

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.file import FileVersion, ProjectFile
from benchlog.services import file_service
from benchlog.templating import templates

router = APIRouter()


async def _get_project(slug: str, db: AsyncSession) -> Project | None:
    result = await db.execute(
        select(Project).options(selectinload(Project.tags)).where(Project.slug == slug)
    )
    return result.scalar_one_or_none()


@router.get("/projects/{slug}/files", response_class=HTMLResponse)
async def file_browser(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    path = request.query_params.get("path", "")

    files = await file_service.get_files_at_path(db, project.id, path)
    subfolders = await file_service.get_subfolders(db, project.id, path)
    folder_tree = await file_service.get_folder_tree(db, project.id)

    # Build breadcrumbs
    breadcrumbs = []
    if path:
        parts = path.split("/")
        for i, part in enumerate(parts):
            breadcrumbs.append({
                "name": part,
                "path": "/".join(parts[: i + 1]),
            })

    return templates.TemplateResponse(request, "files/browser.html", {
        "project": project,
        "files": files,
        "subfolders": subfolders,
        "folder_tree": folder_tree,
        "current_path": path,
        "breadcrumbs": breadcrumbs,
        "format_size": file_service.format_size,
        "active_tab": "files",
    })


@router.post("/projects/{slug}/files/upload")
async def upload_files(
    request: Request,
    slug: str,
    files: list[UploadFile] = File(...),
    path: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    for upload in files:
        data = await upload.read()
        if data:
            await file_service.upload_file(db, project.id, path, upload.filename, data)

    redirect_url = f"/projects/{slug}/files"
    if path:
        redirect_url += f"?path={path}"

    if request.headers.get("hx-request"):
        return HTMLResponse(
            "",
            status_code=200,
            headers={"HX-Redirect": redirect_url},
        )
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/projects/{slug}/files/mkdir")
async def create_folder(
    request: Request,
    slug: str,
    folder_name: str = Form(...),
    path: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Create a folder by adding a placeholder — folders are virtual paths."""
    # Folders are implicit from file paths, but we track them by ensuring
    # the path exists. We redirect back to show the new folder in subfolders.
    # Since folders are virtual, we just redirect to the new path.
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    new_path = f"{path}/{folder_name}" if path else folder_name

    redirect_url = f"/projects/{slug}/files?path={new_path}"
    if request.headers.get("hx-request"):
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=302)


@router.get("/projects/{slug}/files/{file_id}/detail", response_class=HTMLResponse)
async def file_detail(request: Request, slug: str, file_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    pf = await file_service.get_file_by_id(db, uuid.UUID(file_id))
    if not pf:
        return HTMLResponse("File not found", status_code=404)

    current_version = next((v for v in pf.versions if v.is_current), pf.versions[0] if pf.versions else None)

    return templates.TemplateResponse(request, "files/detail.html", {
        "project": project,
        "file": pf,
        "current_version": current_version,
        "format_size": file_service.format_size,
        "active_tab": "files",
    })


@router.post("/projects/{slug}/files/{file_id}/edit")
async def edit_file_metadata(
    request: Request,
    slug: str,
    file_id: str,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    pf = await file_service.get_file_by_id(db, uuid.UUID(file_id))
    if not pf:
        return HTMLResponse("File not found", status_code=404)

    form = await request.form()
    pf.description = form.get("description", "").strip() or None
    pf.filename = form.get("filename", pf.filename).strip()
    await db.commit()

    return RedirectResponse(f"/projects/{slug}/files/{file_id}/detail", status_code=302)


@router.post("/projects/{slug}/files/{file_id}/version")
async def upload_new_version(
    request: Request,
    slug: str,
    file_id: str,
    file: UploadFile = File(...),
    changelog: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    data = await file.read()
    await file_service.upload_new_version(db, uuid.UUID(file_id), file.filename, data, changelog)

    return RedirectResponse(f"/projects/{slug}/files/{file_id}/detail", status_code=302)


@router.get("/projects/{slug}/files/{file_id}/download")
async def download_file(slug: str, file_id: str, db: AsyncSession = Depends(get_db)):
    pf = await file_service.get_file_by_id(db, uuid.UUID(file_id))
    if not pf:
        return HTMLResponse("File not found", status_code=404)

    current_version = next((v for v in pf.versions if v.is_current), None)
    if not current_version:
        return HTMLResponse("No version found", status_code=404)

    data = await file_service.download_version(current_version)
    return Response(
        content=data,
        media_type=current_version.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{pf.filename}"'},
    )


@router.get("/projects/{slug}/files/{file_id}/versions/{version_id}/download")
async def download_specific_version(slug: str, file_id: str, version_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FileVersion).where(FileVersion.id == uuid.UUID(version_id))
    )
    version = result.scalar_one_or_none()
    if not version:
        return HTMLResponse("Version not found", status_code=404)

    data = await file_service.download_version(version)
    return Response(
        content=data,
        media_type=version.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{version.original_name}"'},
    )


@router.post("/projects/{slug}/files/{file_id}/delete")
async def delete_file(request: Request, slug: str, file_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    pf = await file_service.get_file_by_id(db, uuid.UUID(file_id))
    if not pf:
        return HTMLResponse("File not found", status_code=404)

    path = pf.path
    await file_service.delete_file(db, uuid.UUID(file_id))

    redirect_url = f"/projects/{slug}/files"
    if path:
        redirect_url += f"?path={path}"

    if request.headers.get("hx-request"):
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/projects/{slug}/files/{file_id}/move")
async def move_file(
    request: Request,
    slug: str,
    file_id: str,
    new_path: str = Form(""),
    new_filename: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    await file_service.move_file(
        db, uuid.UUID(file_id), new_path, new_filename or None
    )

    return RedirectResponse(f"/projects/{slug}/files/{file_id}/detail", status_code=302)


@router.post("/projects/{slug}/files/batch-delete")
async def batch_delete(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    file_ids = form.getlist("file_ids")
    path = form.get("path", "")

    for fid in file_ids:
        await file_service.delete_file(db, uuid.UUID(fid))

    redirect_url = f"/projects/{slug}/files"
    if path:
        redirect_url += f"?path={path}"

    if request.headers.get("hx-request"):
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/projects/{slug}/files/batch-move")
async def batch_move(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    file_ids = form.getlist("file_ids")
    new_path = form.get("new_path", "")

    for fid in file_ids:
        await file_service.move_file(db, uuid.UUID(fid), new_path)

    redirect_url = f"/projects/{slug}/files"
    if new_path:
        redirect_url += f"?path={new_path}"

    if request.headers.get("hx-request"):
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=302)
