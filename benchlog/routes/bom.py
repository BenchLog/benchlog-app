import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchlog.database import get_db
from benchlog.models import Project
from benchlog.models.bom import BOMItem
from benchlog.templating import templates

router = APIRouter()


async def _get_project(slug: str, db: AsyncSession) -> Project | None:
    result = await db.execute(select(Project).where(Project.slug == slug))
    return result.scalar_one_or_none()


@router.get("/projects/{slug}/bom", response_class=HTMLResponse)
async def bom_list(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(
        select(BOMItem)
        .where(BOMItem.project_id == project.id)
        .order_by(BOMItem.sort_order, BOMItem.name)
    )
    items = result.scalars().all()

    return templates.TemplateResponse(request, "bom/list.html", {
        "project": project,
        "items": items,
    })


@router.post("/projects/{slug}/bom", response_class=HTMLResponse)
async def create_bom_item(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    form = await request.form()
    item = BOMItem(
        project_id=project.id,
        name=form.get("name", "").strip(),
        quantity=Decimal(form.get("quantity", "1") or "1"),
        unit=form.get("unit", "").strip() or None,
        category=form.get("category", "").strip() or None,
        supplier_url=form.get("supplier_url", "").strip() or None,
        price=Decimal(form["price"]) if form.get("price") else None,
        notes=form.get("notes", "").strip() or None,
    )
    db.add(item)
    await db.commit()

    result = await db.execute(
        select(BOMItem)
        .where(BOMItem.project_id == project.id)
        .order_by(BOMItem.sort_order, BOMItem.name)
    )
    items = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "bom/_table.html", {
            "project": project,
            "items": items,
        })
    return RedirectResponse(f"/projects/{slug}/bom", status_code=302)


@router.post("/projects/{slug}/bom/{item_id}/edit", response_class=HTMLResponse)
async def edit_bom_item(request: Request, slug: str, item_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(select(BOMItem).where(BOMItem.id == uuid.UUID(item_id)))
    item = result.scalar_one_or_none()
    if not item:
        return HTMLResponse("Item not found", status_code=404)

    form = await request.form()
    item.name = form.get("name", "").strip()
    item.quantity = Decimal(form.get("quantity", "1") or "1")
    item.unit = form.get("unit", "").strip() or None
    item.category = form.get("category", "").strip() or None
    item.supplier_url = form.get("supplier_url", "").strip() or None
    item.price = Decimal(form["price"]) if form.get("price") else None
    item.notes = form.get("notes", "").strip() or None
    await db.commit()

    result = await db.execute(
        select(BOMItem)
        .where(BOMItem.project_id == project.id)
        .order_by(BOMItem.sort_order, BOMItem.name)
    )
    items = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "bom/_table.html", {
            "project": project,
            "items": items,
        })
    return RedirectResponse(f"/projects/{slug}/bom", status_code=302)


@router.post("/projects/{slug}/bom/{item_id}/delete", response_class=HTMLResponse)
async def delete_bom_item(request: Request, slug: str, item_id: str, db: AsyncSession = Depends(get_db)):
    project = await _get_project(slug, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)

    result = await db.execute(select(BOMItem).where(BOMItem.id == uuid.UUID(item_id)))
    item = result.scalar_one_or_none()
    if item:
        await db.delete(item)
        await db.commit()

    result = await db.execute(
        select(BOMItem)
        .where(BOMItem.project_id == project.id)
        .order_by(BOMItem.sort_order, BOMItem.name)
    )
    items = result.scalars().all()

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "bom/_table.html", {
            "project": project,
            "items": items,
        })
    return RedirectResponse(f"/projects/{slug}/bom", status_code=302)
