"""Markdown rendering for project descriptions and journal entries.

GFM-like: tables, strikethrough, autolinks, task lists, footnotes.

The file-link rewriter (`rewrite_project_file_links`) resolves links of the
form `files/path/to/name.ext` inside rendered HTML to project file URLs, so
entry authors can reference files by their virtual path without knowing
their database IDs.
"""

import re
from typing import Callable
from urllib.parse import quote

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight as _pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound


# Separate from the line-numbered formatter in `benchlog/files.py` — inline
# fenced blocks are usually short snippets, so the table-style line column
# reads as clutter. `nowrap=True` emits just the token spans so we can wrap
# them in our own `<pre>` and dodge markdown-it's default `<pre><code>`
# wrapper (it skips wrapping only if the returned HTML starts with `<pre`).
# Token colors come from the shared `.highlight .*` rules already in the
# stylesheet.
_HIGHLIGHT_FORMATTER = HtmlFormatter(nowrap=True)


def _highlight_fence(code: str, lang: str, _attrs: str) -> str:
    """markdown-it highlight callback — Pygments-render known languages only.

    Returning "" means "let markdown-it do the default `<pre><code>`", which
    is what we want for fences with no language or an unknown one. No lexer
    fallback to TextLexer here (unlike file previews): an unstyled fence is
    the expected shape for prose.
    """
    if not lang:
        return ""
    try:
        lexer = get_lexer_by_name(lang, stripall=False)
    except ClassNotFound:
        return ""
    tokens = _pygments_highlight(code, lexer, _HIGHLIGHT_FORMATTER)
    # Use the lexer's canonical alias (not the raw user string) for the
    # language-* class, so untrusted input can't break out of the attribute.
    aliases = getattr(lexer, "aliases", None) or [lexer.name.lower()]
    canonical = aliases[0]
    return f'<pre class="highlight language-{canonical}"><code>{tokens}</code></pre>'


_md = (
    # html: false forces escaping of raw HTML tags in the source — otherwise
    # the gfm-like preset would let `<script>` etc. through unchanged, since
    # we mark the rendered output safe for Jinja.
    MarkdownIt(
        "gfm-like",
        {
            "linkify": True,
            "typographer": True,
            "html": False,
            "highlight": _highlight_fence,
        },
    )
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

_JOURNAL_LINK_RE = re.compile(
    r'(<a\s[^>]*?)href="journal/([^"]*)"',
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
    file's detail page (`/u/{username}/{slug}/files/{id}`) — more useful
    than a direct download since it shows description, versions, and a
    preview. Otherwise it falls back to the file browser at the
    referenced path so a renamed/missing file still lands somewhere useful.
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
            href = f"{base}/{file_id}"
        else:
            href = f"{base}?path={quote(path)}" if path else base
        return f'{prefix}href="{href}"'

    return _FILES_LINK_RE.sub(_replace, html)


def rewrite_project_journal_links(
    html: str,
    username: str,
    slug: str,
) -> str:
    """Rewrite `href="journal/<entry_slug>"` anchors to canonical URLs.

    The autocomplete inserts relative `journal/<slug>` hrefs; this turns
    them into `/u/{username}/{slug}/journal/<entry_slug>` so they resolve
    the same from any rendering context (description, sibling entries,
    AJAX swaps). A dangling slug (entry since deleted or renamed) still
    routes to a 404 rather than a mystery location.
    """
    base = f"/u/{username}/{slug}/journal"

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        rel = match.group(2)
        # Strip any stray leading slash; otherwise pass the slug through
        # as the authoritative last segment.
        rel = rel.lstrip("/")
        return f'{prefix}href="{base}/{rel}"'

    return _JOURNAL_LINK_RE.sub(_replace, html)


def render_for_project(
    text: str,
    username: str,
    slug: str,
    file_lookup: FileLookup | None = None,
) -> str:
    html = render(text)
    html = rewrite_project_file_links(html, username, slug, file_lookup)
    html = rewrite_project_journal_links(html, username, slug)
    return html


def build_file_lookup_from_files(files) -> FileLookup:
    """Build a FileLookup callable from an eager-loaded ProjectFile list.

    Used by routes that already have `project.files` loaded (the detail
    page, journal tab, etc. via `get_project_by_username_and_slug`). For
    routes that don't eager-load, see `benchlog.files.get_project_file_lookup`.
    """
    index: dict[tuple[str, str], str] = {}
    for f in files:
        index[(f.path or "", f.filename)] = str(f.id)
    return lambda path, filename: index.get((path, filename))
