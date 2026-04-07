import io
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.config import settings
from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.file import ProjectFile
from benchlog.models.update import ProjectUpdate
from benchlog.models.bom import BOMItem
from benchlog.models.link import ProjectLink
from benchlog.storage.local import LocalStorage

router = APIRouter()
storage = LocalStorage(settings.storage_path)


@router.get("/projects/{slug}/export")
async def export_project_zip(slug: str, db: AsyncSession = Depends(get_db)):
    """Download the entire project as an organized ZIP archive."""
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.tags))
        .where(Project.slug == slug)
    )
    project = result.scalar_one_or_none()
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    # Fetch all files with current versions
    files_result = await db.execute(
        select(ProjectFile)
        .options(selectinload(ProjectFile.versions))
        .where(ProjectFile.project_id == project.id)
        .order_by(ProjectFile.path, ProjectFile.filename)
    )
    files = files_result.scalars().all()

    # Fetch updates
    updates_result = await db.execute(
        select(ProjectUpdate)
        .where(ProjectUpdate.project_id == project.id)
        .order_by(ProjectUpdate.created_at.desc())
    )
    updates = updates_result.scalars().all()

    # Fetch BOM items
    bom_result = await db.execute(
        select(BOMItem)
        .where(BOMItem.project_id == project.id)
        .order_by(BOMItem.sort_order, BOMItem.name)
    )
    bom_items = bom_result.scalars().all()

    # Fetch links
    links_result = await db.execute(
        select(ProjectLink)
        .where(ProjectLink.project_id == project.id)
        .order_by(ProjectLink.sort_order)
    )
    links = links_result.scalars().all()

    # Build the ZIP in memory
    buf = io.BytesIO()
    prefix = project.slug

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add README.md
        readme = _build_readme(project, updates, bom_items, links)
        zf.writestr(f"{prefix}/README.md", readme)

        # Add project files (current versions only)
        for f in files:
            current_v = next((v for v in f.versions if v.is_current), None)
            if not current_v:
                continue

            if f.path:
                zip_path = f"{prefix}/files/{f.path}/{f.filename}"
            else:
                zip_path = f"{prefix}/files/{f.filename}"

            try:
                data = await storage.read(current_v.storage_path)
                zf.writestr(zip_path, data)
            except FileNotFoundError:
                continue

    buf.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"{project.slug}-{timestamp}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_readme(project, updates, bom_items, links) -> str:
    """Generate a README.md for the exported project."""
    lines = [f"# {project.title}\n"]

    status = project.status.value.replace("_", " ").title()
    lines.append(f"**Status:** {status}\n")

    if project.tags:
        tag_names = ", ".join(t.name for t in project.tags)
        lines.append(f"**Tags:** {tag_names}\n")

    if project.description:
        lines.append("")
        lines.append(project.description)
        lines.append("")

    if updates:
        lines.append("\n---\n")
        lines.append("## Updates\n")
        for u in updates:
            date_str = u.created_at.strftime("%Y-%m-%d %H:%M")
            if u.title:
                lines.append(f"### {u.title}")
                lines.append(f"*{date_str}*\n")
            else:
                lines.append(f"### {date_str}\n")
            lines.append(u.content)
            lines.append("")

    if bom_items:
        lines.append("\n---\n")
        lines.append("## Bill of Materials\n")
        lines.append("| Name | Qty | Unit | Category | Price | Notes |")
        lines.append("|------|-----|------|----------|-------|-------|")
        for item in bom_items:
            qty = str(item.quantity) if item.quantity else ""
            unit = item.unit or ""
            cat = item.category or ""
            price = f"${item.price:.2f}" if item.price else ""
            notes = item.notes or ""
            lines.append(f"| {item.name} | {qty} | {unit} | {cat} | {price} | {notes} |")
        lines.append("")

    if links:
        lines.append("\n---\n")
        lines.append("## Links\n")
        for link in links:
            lines.append(f"- [{link.title}]({link.url})")
        lines.append("")

    lines.append("\n---\n")
    lines.append(f"*Exported from BenchLog on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*\n")

    return "\n".join(lines)
