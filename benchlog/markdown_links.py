"""Resolve project-internal file links in rendered markdown.

Supports links like:
    [my model](files/models/widget.stl)
    [schematic](files/electronics/board.pdf)

These get rewritten to:
    /projects/{slug}/files/{file_id}/download  (if file found)
    /projects/{slug}/files?path=...  (fallback to browser at that path)
"""

import re

_FILES_LINK_RE = re.compile(
    r'(<a\s[^>]*?)href="files/([^"]*)"',
    re.IGNORECASE,
)


def rewrite_file_links(html: str, slug: str, file_lookup=None) -> str:
    """Rewrite files/... links to project file URLs.

    Args:
        html: Rendered HTML string.
        slug: Project slug for URL generation.
        file_lookup: Optional callable(path, filename) -> file_id or None.
    """
    def _replace(match: re.Match) -> str:
        prefix = match.group(1)
        file_path = match.group(2)

        if "/" in file_path:
            parts = file_path.rsplit("/", 1)
            path, filename = parts[0], parts[1]
        else:
            path, filename = "", file_path

        if file_lookup and filename:
            file_id = file_lookup(path, filename)
            if file_id:
                return f'{prefix}href="/projects/{slug}/files/{file_id}/download"'

        if filename:
            browser_path = path if path else ""
            return f'{prefix}href="/projects/{slug}/files?path={browser_path}"'
        else:
            return f'{prefix}href="/projects/{slug}/files?path={file_path.rstrip("/")}"'

    return _FILES_LINK_RE.sub(_replace, html)
