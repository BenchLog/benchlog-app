"""Write and read helpers for ActivityEvent.

`record_event` is a thin wrapper used by the call sites that emit events
(create_project, fork_project, create_entry, file upload). It flushes so
the caller can inspect the new row but leaves the commit to the caller,
matching the convention in `add_relation`, `fork_project`, etc.

The three list helpers (`list_project_activity`, `list_user_activity`,
`list_global_activity`) back the per-project tab, the profile section,
and the explore firehose respectively. Visibility is applied at the
query level for the user + global feeds; the project feed skips it
because the surrounding route already gates on project visibility.

Visibility reflects the project's CURRENT `is_public` flag. If a project
goes private, its events disappear from the profile/global feeds; if it
later goes public again they reappear. No historical snapshotting —
matches every other visibility surface in the app.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from benchlog.models import (
    ActivityEvent,
    ActivityEventType,
    JournalEntry,
    Project,
    User,
)


async def purge_entry_events(db: AsyncSession, entry_id: uuid.UUID) -> None:
    """Drop `journal_entry_posted` events for a deleted entry.

    Activity is a what's-new feed, not a history log — once the entry is
    gone there's nothing meaningful to link to. Caller owns the commit.
    """
    await db.execute(
        delete(ActivityEvent).where(
            ActivityEvent.event_type == ActivityEventType.journal_entry_posted,
            ActivityEvent.payload["entry_id"].astext == str(entry_id),
        )
    )


async def purge_file_events(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Drop all `file_uploaded` / `file_version_added` events for a
    deleted file (both event types share the same `file_id` payload key).
    Caller owns the commit.
    """
    await db.execute(
        delete(ActivityEvent).where(
            ActivityEvent.event_type.in_(
                (
                    ActivityEventType.file_uploaded,
                    ActivityEventType.file_version_added,
                )
            ),
            ActivityEvent.payload["file_id"].astext == str(file_id),
        )
    )


async def purge_file_version_events(
    db: AsyncSession, file_id: uuid.UUID, version_number: int
) -> None:
    """Drop a single `file_version_added` event. Used when one version of
    an otherwise-surviving file is deleted. Caller owns the commit.
    """
    await db.execute(
        delete(ActivityEvent).where(
            ActivityEvent.event_type == ActivityEventType.file_version_added,
            ActivityEvent.payload["file_id"].astext == str(file_id),
            ActivityEvent.payload["version_number"].astext == str(version_number),
        )
    )


async def record_event(
    db: AsyncSession,
    *,
    actor: User,
    project: Project,
    event_type: ActivityEventType,
    payload: dict | None = None,
) -> ActivityEvent:
    """Insert an ActivityEvent row. Caller owns the commit."""
    event = ActivityEvent(
        actor_id=actor.id,
        project_id=project.id,
        event_type=event_type,
        payload=payload or {},
    )
    db.add(event)
    await db.flush()
    return event


def _with_rendering_loads(query):
    """Apply the eager-loads every feed template needs.

    Loads actor, project, and project.user in one go so the
    `_activity_line.html` partial can render deep-links without tripping
    `raise_on_sql`.
    """
    return query.options(
        selectinload(ActivityEvent.actor),
        selectinload(ActivityEvent.project).selectinload(Project.user),
    )


async def _filter_private_journal_events(
    db: AsyncSession,
    events: list[ActivityEvent],
    viewer_id: uuid.UUID | None,
) -> list[ActivityEvent]:
    """Drop `journal_entry_posted` events whose referenced entry is private
    and the viewer isn't its actor.

    Project visibility is enforced at the SQL level by the three list
    helpers, but journal entries have their own independent `is_public`
    flag — a private entry on a public project shouldn't leak its
    existence via the activity feed. Entries that no longer exist keep
    their event (visibility can't be verified after delete and "posted
    something" leaks nothing the project page doesn't already).
    """
    needs_check: dict[str, list[ActivityEvent]] = {}
    for event in events:
        if event.event_type != ActivityEventType.journal_entry_posted:
            continue
        if viewer_id is not None and event.actor_id == viewer_id:
            continue
        entry_id = event.payload.get("entry_id") if event.payload else None
        if not entry_id:
            continue
        needs_check.setdefault(entry_id, []).append(event)

    if not needs_check:
        return events

    try:
        entry_uuids = [uuid.UUID(eid) for eid in needs_check.keys()]
    except (ValueError, TypeError):
        return events

    result = await db.execute(
        select(JournalEntry.id, JournalEntry.is_public).where(
            JournalEntry.id.in_(entry_uuids)
        )
    )
    visibility = {str(eid): is_pub for eid, is_pub in result.all()}

    return [
        e for e in events
        if not (
            e.event_type == ActivityEventType.journal_entry_posted
            and (viewer_id is None or e.actor_id != viewer_id)
            and visibility.get(
                (e.payload or {}).get("entry_id", "")
            ) is False
        )
    ]


async def list_project_activity(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID | None,
    limit: int = 50,
) -> list[ActivityEvent]:
    """Events for a single project, newest first. The surrounding route
    gates project-level visibility; this helper filters private journal
    events for non-actor viewers.
    """
    result = await db.execute(
        _with_rendering_loads(
            select(ActivityEvent)
            .where(ActivityEvent.project_id == project_id)
            .order_by(ActivityEvent.created_at.desc())
            .limit(limit)
        )
    )
    events = list(result.scalars().all())
    return await _filter_private_journal_events(db, events, viewer_id)


async def list_user_activity(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID | None,
    limit: int = 50,
) -> list[ActivityEvent]:
    """Events by `user_id` on projects visible to `viewer_id`.

    Guests (`viewer_id is None`) see events on the user's public projects
    only. Logged-in viewers see public projects plus any private projects
    they own themselves (which, given `actor_id == user_id`, only matters
    when the viewer IS the profile owner — they see all their own).
    """
    query = (
        select(ActivityEvent)
        .join(Project, Project.id == ActivityEvent.project_id)
        .where(ActivityEvent.actor_id == user_id)
    )
    if viewer_id is None:
        query = query.where(Project.is_public.is_(True))
    else:
        query = query.where(
            (Project.is_public.is_(True)) | (Project.user_id == viewer_id)
        )
    query = query.order_by(ActivityEvent.created_at.desc()).limit(limit)
    result = await db.execute(_with_rendering_loads(query))
    events = list(result.scalars().all())
    return await _filter_private_journal_events(db, events, viewer_id)


async def list_global_activity(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    limit: int = 50,
    offset: int = 0,
) -> list[ActivityEvent]:
    """Firehose feed — public projects only, newest first, paginated by offset."""
    query = (
        select(ActivityEvent)
        .join(Project, Project.id == ActivityEvent.project_id)
        .where(Project.is_public.is_(True))
        .order_by(ActivityEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(_with_rendering_loads(query))
    events = list(result.scalars().all())
    return await _filter_private_journal_events(db, events, viewer_id)
