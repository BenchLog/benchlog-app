from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.models import SiteSettings


async def get_site_settings(db: AsyncSession) -> SiteSettings:
    """Return the site settings row, creating it with defaults if missing.

    Commits when it creates so subsequent callers see the row without needing
    to know that this service might do an INSERT.
    """
    result = await db.execute(select(SiteSettings).limit(1))
    record = result.scalar_one_or_none()
    if record is None:
        record = SiteSettings()
        db.add(record)
        await db.commit()
        await db.refresh(record)
    return record
