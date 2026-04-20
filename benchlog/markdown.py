"""Markdown rendering for project descriptions and updates.

GFM-like: tables, strikethrough, autolinks, task lists, footnotes.

The file-link rewriter (`rewrite_project_file_links`) resolves links of the
form `files/path/to/name.ext` inside rendered HTML to project file URLs, so
update authors can reference files by their virtual path without knowing
their database IDs.
"""

import re
from typing import Callable
from urllib.parse import quote

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

_md = (
    # html: false forces escaping of raw HTML tags in the source — otherwise
    # the gfm-like preset would let `<script>` etc. through unchanged, since
    # we mark the rendered output safe for Jinja.
    MarkdownIt("gfm-like", {"linkify": True, "typographer": True, "html": False})
    .enable("table")
    .enable("strikethrough")
)
tasklists_plugin(_md)
footnote_plugin(_md)


FileLookup = Callable[[str, str], str | None]
"""(path, filename) -> file_id (stringified UUID) or None."""


def render(text: str) -> str:
    return _md.render(text or "")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def plain_excerpt(text: str | None, max_len: int = 200) -> str:
    """Markdown → plain-text single-line excerpt, capped at `max_len`.

    Used for meta description / og:description where a paragraph of
    pretty-printed prose would be noise. Renders markdown so things like
    `**bold**` become `bold` (not literal asterisks), strips tags, folds
    whitespace, truncates on a word boundary with an ellipsis.
    """
    if not text:
        return ""
    html = _md.render(text)
    stripped = _TAG_RE.sub("", html)
    flat = _WS_RE.sub(" ", stripped).strip()
    if len(flat) <= max_len:
        return flat
    cut = flat[: max_len + 1].rsplit(" ", 1)[0].rstrip(",.;:—-")
    return f"{cut}…"


_FILES_LINK_RE = re.compile(
    r'(<a\s[^>]*?)href="files/([^"]*)"',
    re.IGNORECASE,
)


def rewrite_project_file_links(
    html: str,
    username: str,
    slug: str,
    file_lookup: FileLookup | None = None,
) -> str:
    """Rewrite `href="files/..."` anchors to canonical project file URLs.

    If a `file_lookup` is provided and matches, the link points at the
    file's download URL (`/u/{username}/{slug}/files/{id}/download`).
    Otherwise it falls back to the file browser at the referenced path.
    """

    base = f"/u/{username}/{slug}/files"

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        rel = match.group(2)
        if "/" in rel:
            path, filename = rel.rsplit("/", 1)
        else:
            path, filename = "", rel

        file_id = file_lookup(path, filename) if file_lookup else None
        if file_id:
            href = f"{base}/{file_id}/download"
        else:
            href = f"{base}?path={quote(path)}" if path else base
        return f'{prefix}href="{href}"'

    return _FILES_LINK_RE.sub(_replace, html)


def render_for_project(
    text: str,
    username: str,
    slug: str,
    file_lookup: FileLookup | None = None,
) -> str:
    return rewrite_project_file_links(render(text), username, slug, file_lookup)
