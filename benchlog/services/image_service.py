import hashlib
import mimetypes
import uuid
from io import BytesIO

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.config import settings
from benchlog.models.image import Image
from benchlog.storage.local import LocalStorage

storage = LocalStorage(settings.storage_path)

THUMB_SIZE = (400, 400)


async def upload_image(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: bytes,
    original_name: str,
    project_id: uuid.UUID | None = None,
    alt_text: str = "",
) -> Image:
    mime_type = mimetypes.guess_type(original_name)[0] or "image/jpeg"
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "jpg"

    image_uuid = uuid.uuid4()
    storage_path = f"images/{image_uuid}.{ext}"
    thumb_path = f"thumbnails/{image_uuid}_thumb.{ext}"

    # Save original
    await storage.save(storage_path, BytesIO(data))

    # Get dimensions and generate thumbnail
    width, height = None, None
    try:
        pil_img = PILImage.open(BytesIO(data))
        width, height = pil_img.size

        pil_img.thumbnail(THUMB_SIZE)
        thumb_buf = BytesIO()
        fmt = "PNG" if ext == "png" else "JPEG"
        pil_img.save(thumb_buf, format=fmt)
        thumb_buf.seek(0)
        await storage.save(thumb_path, thumb_buf)
    except Exception:
        thumb_path = None

    image = Image(
        user_id=user_id,
        project_id=project_id,
        storage_path=storage_path,
        thumbnail_path=thumb_path,
        original_name=original_name,
        size_bytes=len(data),
        mime_type=mime_type,
        width=width,
        height=height,
        alt_text=alt_text or None,
    )
    db.add(image)
    await db.commit()
    return image


async def delete_image(db: AsyncSession, image_id: uuid.UUID) -> None:
    result = await db.execute(select(Image).where(Image.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        return

    await storage.delete(image.storage_path)
    if image.thumbnail_path:
        await storage.delete(image.thumbnail_path)

    await db.delete(image)
    await db.commit()


async def get_image(db: AsyncSession, image_id: uuid.UUID) -> Image | None:
    result = await db.execute(select(Image).where(Image.id == image_id))
    return result.scalar_one_or_none()
