from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.file import ProjectFile
from benchlog.models.update import ProjectUpdate
from benchlog.templating import templates

router = APIRouter()


def _fts_or_ilike(column, q):
    """Use FTS for longer queries, ILIKE for short ones where FTS may drop terms."""
    tsquery = func.plainto_tsquery("english", q)
    fts = func.to_tsvector("english", func.coalesce(column, "")).op("@@")(tsquery)
    ilike = func.coalesce(column, "").ilike(f"%{q}%")
    return or_(fts, ilike)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    results = {"projects": [], "files": [], "updates": []}

    if q.strip():
        # Search projects
        proj_result = await db.execute(
            select(Project)
            .options(selectinload(Project.tags))
            .where(
                or_(
                    _fts_or_ilike(Project.title, q),
                    _fts_or_ilike(Project.description, q),
                )
            )
            .order_by(Project.updated_at.desc())
            .limit(20)
        )
        results["projects"] = list(proj_result.scalars().all())

        # Search files
        file_result = await db.execute(
            select(ProjectFile)
            .join(Project)
            .where(
                or_(
                    _fts_or_ilike(ProjectFile.filename, q),
                    _fts_or_ilike(ProjectFile.description, q),
                )
            )
            .options(selectinload(ProjectFile.project))
            .order_by(ProjectFile.updated_at.desc())
            .limit(20)
        )
        results["files"] = list(file_result.scalars().all())

        # Search updates
        update_result = await db.execute(
            select(ProjectUpdate)
            .join(Project)
            .where(
                or_(
                    _fts_or_ilike(ProjectUpdate.title, q),
                    _fts_or_ilike(ProjectUpdate.content, q),
                )
            )
            .options(selectinload(ProjectUpdate.project))
            .order_by(ProjectUpdate.created_at.desc())
            .limit(20)
        )
        results["updates"] = list(update_result.scalars().all())

    total = len(results["projects"]) + len(results["files"]) + len(results["updates"])

    return templates.TemplateResponse(request, "search/results.html", {
        "q": q,
        "results": results,
        "total": total,
    })
