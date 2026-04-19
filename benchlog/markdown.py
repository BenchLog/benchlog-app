"""Markdown rendering for project descriptions and updates.

GFM-like: tables, strikethrough, autolinks, task lists, footnotes.

The file-link rewriter (`rewrite_project_file_links`) resolves links of the
form `files/path/to/name.ext` inside rendered HTML to project file URLs, so
update authors can reference files by their virtual path without knowing
their database IDs.
"""

import re
from typing import Callable

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

_md = (
    MarkdownIt("gfm-like", {"linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)
tasklists_plugin(_md)
footnote_plugin(_md)


FileLookup = Callable[[str, str], str | None]
"""(path, filename) -> file_id (stringified UUID) or None."""


def render(text: str) -> str:
    return _md.render(text or "")


_FILES_LINK_RE = re.compile(
    r'(<a\s[^>]*?)href="files/([^"]*)"',
    re.IGNORECASE,
)


def rewrite_project_file_links(
    html: str,
    slug: str,
    file_lookup: FileLookup | None = None,
) -> str:
    """Rewrite `href="files/..."` anchors to project file routes.

    If a `file_lookup` is provided and matches, the link points at the
    file's download URL. Otherwise it falls back to the file browser at
    the referenced path.
    """

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        rel = match.group(2)
        if "/" in rel:
            path, filename = rel.rsplit("/", 1)
        else:
            path, filename = "", rel

        file_id = file_lookup(path, filename) if file_lookup else None
        if file_id:
            href = f"/projects/{slug}/files/{file_id}/download"
        else:
            href = f"/projects/{slug}/files?path={path}"
        return f'{prefix}href="{href}"'

    return _FILES_LINK_RE.sub(_replace, html)


def render_for_project(
    text: str,
    slug: str,
    file_lookup: FileLookup | None = None,
) -> str:
    return rewrite_project_file_links(render(text), slug, file_lookup)
