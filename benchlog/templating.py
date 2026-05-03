from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from benchlog.config import settings
from benchlog.files import file_icon, human_size
from benchlog.markdown import build_file_lookup_from_files, plain_excerpt
from benchlog.markdown import render as render_markdown
from benchlog.markdown import render_for_project

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _markdown_filter(text: str | None) -> Markup:
    """Render markdown to HTML, marked safe so Jinja won't double-escape.

    markdown-it-py's "gfm-like" preset leaves `html: false`, so raw HTML
    tags in user input get escaped before we mark the result safe.
    """
    return Markup(render_markdown(text or ""))


def _project_markdown_filter(text: str | None, project, is_owner: bool = False) -> Markup:
    """Render markdown for content tied to a project. Rewrites bare
    `files/<path>/<name>` links to canonical `/u/{user}/{slug}/files/{id}`
    detail-page URLs, rewrites bare `journal/<entry_slug>` links to
    `/u/{user}/{slug}/journal/{entry_slug}`, and turns
    `![[name.excalidraw]]` into editable embed placeholders.

    `is_owner` controls whether Excalidraw embeds get the editable
    affordance. Pass it from the calling template's `is_owner` local.
    Defaults False so pages that don't pass it stay safe (read-only).

    Requires ``project.files`` to be eager-loaded — the shared
    ``get_project_by_username_and_slug`` helper does this. Falls back to
    plain markdown rendering if the files relationship isn't accessible,
    so pages that forget to eager-load still render content rather than
    raise ``raise_on_sql``.
    """
    if not text:
        return Markup("")
    try:
        files = list(project.files)
    except Exception:
        return Markup(render_markdown(text))
    lookup = build_file_lookup_from_files(files)
    return Markup(
        render_for_project(
            text, project.user.username, project.slug, lookup, is_owner=is_owner
        )
    )


def _absolute_url(path: str) -> str:
    """Join an app-relative path onto settings.base_url. Used for canonical
    URLs and og:image where social scrapers need an absolute."""
    if path.startswith(("http://", "https://")):
        return path
    return settings.base_url.rstrip("/") + "/" + path.lstrip("/")


templates.env.filters["markdown"] = _markdown_filter
templates.env.filters["project_markdown"] = _project_markdown_filter
templates.env.filters["human_size"] = human_size
templates.env.filters["file_icon"] = file_icon
templates.env.filters["excerpt"] = plain_excerpt
templates.env.filters["absolute_url"] = _absolute_url
templates.env.globals["site_name"] = "BenchLog"
templates.env.globals["site_base_url"] = settings.base_url.rstrip("/")
