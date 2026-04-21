"""Whole-project export — bundles metadata + file blobs into a single zip.

The archive is laid out for both machine consumption (`project.json` is
exhaustive and round-trippable into an import flow later) and human
skimming (`README.md` renders an overview). Visibility of individual
journal entries is honoured — a guest exporting a public project only
gets the public entries, matching what they'd see on the web.

Caller is expected to pass a `Project` with the standard eager loads
already populated: `user`, `tags`, `journal_entries`, `links`, `files`,
plus `files.current_version` and `cover_file.current_version`.
"""

import io
import json
import zipfile
from datetime import datetime, timezone

from anyio import to_thread

from benchlog.files import human_size
from benchlog.models import Project
from benchlog.storage import LocalStorage

EXPORT_VERSION = 1


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _files_meta(project: Project) -> list[dict]:
    out: list[dict] = []
    for f in sorted(project.files, key=lambda x: (x.path, x.filename.lower())):
        if f.current_version is None:
            continue
        cv = f.current_version
        out.append(
            {
                "path": f.path,
                "filename": f.filename,
                "description": f.description,
                "show_in_gallery": f.show_in_gallery,
                "size_bytes": cv.size_bytes,
                "mime_type": cv.mime_type,
                "checksum": cv.checksum,
                "version_number": cv.version_number,
                "uploaded_at": _iso(cv.uploaded_at),
                "is_image": cv.is_image,
                "width": cv.width,
                "height": cv.height,
            }
        )
    return out


def _journal_entries_meta(
    project: Project, *, include_private: bool
) -> list[dict]:
    out: list[dict] = []
    for entry in sorted(project.journal_entries, key=lambda x: x.created_at):
        if not include_private and not entry.is_public:
            continue
        out.append(
            {
                "id": str(entry.id),
                "title": entry.title,
                "slug": entry.slug,
                "content": entry.content,
                "is_public": entry.is_public,
                "is_pinned": entry.is_pinned,
                "created_at": _iso(entry.created_at),
                "updated_at": _iso(entry.updated_at),
            }
        )
    return out


def _links_meta(project: Project) -> list[dict]:
    out: list[dict] = []
    for link in sorted(project.links, key=lambda x: (x.sort_order, x.created_at)):
        out.append(
            {
                "title": link.title,
                "url": link.url,
                "link_type": link.link_type.value,
                "sort_order": link.sort_order,
                "created_at": _iso(link.created_at),
            }
        )
    return out


def build_project_json(project: Project, *, include_private_entries: bool) -> dict:
    """The whole project as a single structured document."""
    cover_path: str | None = None
    if project.cover_file is not None:
        cf = project.cover_file
        cover_path = f"{cf.path}/{cf.filename}" if cf.path else cf.filename

    return {
        "benchlog_export_version": EXPORT_VERSION,
        "exported_at": _iso(datetime.now(timezone.utc)),
        "slug": project.slug,
        "title": project.title,
        "description": project.description,
        "status": project.status.value,
        "pinned": project.pinned,
        "is_public": project.is_public,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "tags": sorted(t.slug for t in project.tags),
        "owner": {
            "username": project.user.username,
            "display_name": project.user.display_name,
        },
        "cover_file": cover_path,
        "files": _files_meta(project),
        "journal_entries": _journal_entries_meta(
            project, include_private=include_private_entries
        ),
        "links": _links_meta(project),
    }


def build_readme(project: Project, data: dict) -> str:
    """Human-readable markdown overview written to the zip root.

    Journal entries live in their own `journal.md` (since there can be
    lots) — the README just points at it. Files are annotated with
    `cover` / `gallery` tags so the reader can see which ones show up on
    the project page without opening every file.
    """
    lines: list[str] = []
    lines.append(f"# {project.title}")
    lines.append("")
    lines.append(f"*by {project.user.display_name}*")
    lines.append("")
    status_label = project.status.value.replace("_", " ")
    lines.append(f"- **Status:** {status_label}")
    if data["tags"]:
        lines.append(f"- **Tags:** {', '.join(data['tags'])}")
    if data["exported_at"]:
        lines.append(f"- **Exported:** {data['exported_at']}")
    if data["cover_file"]:
        link = f"[files/{data['cover_file']}](files/{data['cover_file']})"
        lines.append(f"- **Cover image:** {link}")
    lines.append("")

    if project.description:
        lines.append(project.description.rstrip())
        lines.append("")

    if data["journal_entries"]:
        count = len(data["journal_entries"])
        noun = "entry" if count == 1 else "entries"
        lines.append("## Journal")
        lines.append("")
        lines.append(f"See [`journal.md`](journal.md) — {count} {noun}.")
        lines.append("")

    if data["links"]:
        lines.append("## Links")
        lines.append("")
        for link in data["links"]:
            lines.append(f"- [{link['title']}]({link['url']}) — {link['link_type']}")
        lines.append("")

    if data["files"]:
        lines.extend(_render_files_section(data))

    return "\n".join(lines)


def _render_files_section(data: dict) -> list[str]:
    """Files section — grouped by virtual folder, with clickable relative
    markdown links so a reader opening the zip in a viewer can jump to
    each file. Root-level files render first (no heading), nested folders
    follow as `###` subsections."""
    cover = data["cover_file"]
    gallery_entries = [
        f for f in data["files"] if f["is_image"] and f["show_in_gallery"]
    ]

    lines: list[str] = ["## Files", ""]
    total = len(data["files"])
    noun = "file" if total == 1 else "files"
    extras: list[str] = []
    if gallery_entries:
        extras.append(f"{len(gallery_entries)} in gallery")
    if cover:
        extras.append("1 cover image")
    summary = f"{total} {noun} total"
    if extras:
        summary += f" — {', '.join(extras)}"
    lines.append(summary + ".")
    lines.append("")

    # Group entries by their `path` (folder). Root ("") first, then other
    # folders alphabetically. Within each folder, files go alphabetical.
    groups: dict[str, list[dict]] = {}
    for f in data["files"]:
        groups.setdefault(f["path"], []).append(f)
    for bucket in groups.values():
        bucket.sort(key=lambda e: e["filename"].lower())
    sorted_groups = sorted(groups.items(), key=lambda kv: (kv[0] != "", kv[0].lower()))

    for folder, entries in sorted_groups:
        if folder:
            lines.append(f"### {folder}/")
            lines.append("")
        for f in entries:
            full = f"{f['path']}/{f['filename']}" if f["path"] else f["filename"]
            link = f"[{f['filename']}](files/{full})"
            pieces = [link, human_size(f["size_bytes"])]
            if cover and full == cover:
                pieces.append("cover")
            if f["is_image"] and f["show_in_gallery"]:
                pieces.append("gallery")
            lines.append(f"- {' · '.join(pieces)}")
        lines.append("")

    return lines


def build_journal_md(project: Project, data: dict) -> str:
    """Dedicated journal log. Same rendering as the old inline README
    section, but its own file so long-running projects don't dwarf the
    README with years of entries."""
    lines: list[str] = []
    lines.append(f"# Journal — {project.title}")
    lines.append("")
    count = len(data["journal_entries"])
    noun = "entry" if count == 1 else "entries"
    lines.append(f"{count} {noun}.")
    lines.append("")
    for entry in data["journal_entries"]:
        title = entry["title"] or "Untitled entry"
        date = (entry["created_at"] or "")[:10]
        visibility = "" if entry["is_public"] else " _(private)_"
        lines.append(f"## {title} — {date}{visibility}")
        lines.append("")
        lines.append(entry["content"].rstrip())
        lines.append("")
    return "\n".join(lines)


async def build_project_export(
    project: Project,
    storage: LocalStorage,
    *,
    include_private_entries: bool,
) -> bytes:
    """Package the project into a `{slug}.zip`-shaped bytes blob.

    Contents:
      project.json   — exhaustive structured metadata (journal entries,
                       links, files list, tags, cover pointer, owner)
      README.md      — human overview: description + journal + links
      journal.md     — dedicated journal log (omitted when there are none)
      files/…        — every current file version, preserving virtual paths

    Compression runs in a thread so the event loop stays free. For
    projects with lots of bytes this still loads the whole zip into
    memory — fine for self-hosted instances, revisit with streaming
    if anyone hits the limit.
    """
    data = build_project_json(
        project, include_private_entries=include_private_entries
    )
    readme = build_readme(project, data)
    journal_md = (
        build_journal_md(project, data) if data["journal_entries"] else None
    )

    file_members: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for f in project.files:
        if f.current_version is None:
            continue
        arcname = f"files/{f.path}/{f.filename}" if f.path else f"files/{f.filename}"
        if arcname in seen:
            continue
        seen.add(arcname)
        try:
            content = await storage.read(f.current_version.storage_path)
        except (FileNotFoundError, ValueError):
            continue
        file_members.append((arcname, content))

    def _zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "project.json", json.dumps(data, indent=2, ensure_ascii=False)
            )
            zf.writestr("README.md", readme)
            if journal_md is not None:
                zf.writestr("journal.md", journal_md)
            for arcname, content in file_members:
                zf.writestr(arcname, content)
        return buf.getvalue()

    return await to_thread.run_sync(_zip)
