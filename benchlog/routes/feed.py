from datetime import timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Depends
from fastapi.responses import Response, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.database import get_db
from benchlog.markdown import render_markdown
from benchlog.models import Project
from benchlog.models.update import ProjectUpdate

router = APIRouter()

ATOM_NS = "http://www.w3.org/2005/Atom"


def _rfc3339(dt) -> str:
    """Format a datetime as RFC 3339 (Atom feed format)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _build_atom_feed(
    title: str,
    feed_url: str,
    site_url: str,
    updates: list,
) -> bytes:
    """Build an Atom XML feed from a list of (update, project_slug) tuples."""
    feed = Element("feed", xmlns=ATOM_NS)

    SubElement(feed, "title").text = title
    SubElement(feed, "id").text = feed_url
    SubElement(feed, "link", href=feed_url, rel="self")
    SubElement(feed, "link", href=site_url, rel="alternate")
    SubElement(feed, "generator").text = "BenchLog"

    if updates:
        SubElement(feed, "updated").text = _rfc3339(updates[0][0].created_at)

    for update, project_slug in updates:
        entry = SubElement(feed, "entry")

        entry_title = update.title or f"Update — {update.created_at.strftime('%b %d, %Y')}"
        SubElement(entry, "title").text = entry_title

        entry_url = f"{settings.base_url}/projects/{project_slug}/updates/{update.id}"
        SubElement(entry, "id").text = entry_url
        SubElement(entry, "link", href=entry_url, rel="alternate")

        SubElement(entry, "published").text = _rfc3339(update.created_at)
        SubElement(entry, "updated").text = _rfc3339(update.updated_at or update.created_at)

        content_html = render_markdown(update.content)
        content_el = SubElement(entry, "content", type="html")
        content_el.text = content_html

    return b'<?xml version="1.0" encoding="utf-8"?>\n' + tostring(feed, encoding="unicode").encode("utf-8")


@router.get("/projects/{slug}/feed.atom")
async def project_feed(slug: str, db: AsyncSession = Depends(get_db)):
    """Atom feed for a single project's updates."""
    result = await db.execute(
        select(Project).where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    updates_result = await db.execute(
        select(ProjectUpdate)
        .where(ProjectUpdate.project_id == project.id)
        .order_by(ProjectUpdate.created_at.desc())
        .limit(50)
    )
    updates = [(u, slug) for u in updates_result.scalars().all()]

    xml = _build_atom_feed(
        title=f"{project.title} — BenchLog",
        feed_url=f"{settings.base_url}/projects/{slug}/feed.atom",
        site_url=f"{settings.base_url}/projects/{slug}",
        updates=updates,
    )

    return Response(content=xml, media_type="application/atom+xml; charset=utf-8")


@router.get("/feed.atom")
async def global_feed(db: AsyncSession = Depends(get_db)):
    """Atom feed for recent updates across all projects."""
    updates_result = await db.execute(
        select(ProjectUpdate)
        .options(selectinload(ProjectUpdate.project))
        .order_by(ProjectUpdate.created_at.desc())
        .limit(50)
    )
    updates = [(u, u.project.slug) for u in updates_result.scalars().all()]

    xml = _build_atom_feed(
        title="BenchLog — Recent Updates",
        feed_url=f"{settings.base_url}/feed.atom",
        site_url=settings.base_url,
        updates=updates,
    )

    return Response(content=xml, media_type="application/atom+xml; charset=utf-8")
