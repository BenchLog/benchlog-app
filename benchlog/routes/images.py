import uuid

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.models.user import User
from benchlog.services import image_service
from benchlog.storage.local import LocalStorage
from benchlog.config import settings

router = APIRouter()
storage = LocalStorage(settings.storage_path)


async def _get_user_id(db: AsyncSession) -> uuid.UUID:
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    return user.id if user else uuid.uuid4()


@router.post("/images/upload")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    project_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Upload an image. Returns JSON with the image URL for markdown editors."""
    data = await file.read()
    if not data:
        return JSONResponse({"error": "Empty file"}, status_code=400)

    user_id = await _get_user_id(db)
    pid = uuid.UUID(project_id) if project_id else None

    image = await image_service.upload_image(
        db, user_id, data, file.filename, project_id=pid
    )

    return JSONResponse({
        "id": str(image.id),
        "url": f"/images/{image.id}/view",
        "thumbnail_url": f"/images/{image.id}/thumb" if image.thumbnail_path else None,
        "markdown": f"![{image.original_name}](/images/{image.id}/view)",
    })


@router.get("/images/{image_id}/view")
async def view_image(image_id: str, db: AsyncSession = Depends(get_db)):
    image = await image_service.get_image(db, uuid.UUID(image_id))
    if not image:
        return HTMLResponse("Image not found", status_code=404)

    data = await storage.read(image.storage_path)
    return Response(content=data, media_type=image.mime_type)


@router.get("/images/{image_id}/thumb")
async def view_thumbnail(image_id: str, db: AsyncSession = Depends(get_db)):
    image = await image_service.get_image(db, uuid.UUID(image_id))
    if not image or not image.thumbnail_path:
        return HTMLResponse("Thumbnail not found", status_code=404)

    data = await storage.read(image.thumbnail_path)
    return Response(content=data, media_type=image.mime_type)


@router.post("/images/{image_id}/delete")
async def delete_image(request: Request, image_id: str, db: AsyncSession = Depends(get_db)):
    await image_service.delete_image(db, uuid.UUID(image_id))
    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return JSONResponse({"ok": True})
