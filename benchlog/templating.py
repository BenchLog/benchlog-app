from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from benchlog.config import settings
from benchlog.files import human_size
from benchlog.markdown import plain_excerpt
from benchlog.markdown import render as render_markdown

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _markdown_filter(text: str | None) -> Markup:
    """Render markdown to HTML, marked safe so Jinja won't double-escape.

    markdown-it-py's "gfm-like" preset leaves `html: false`, so raw HTML
    tags in user input get escaped before we mark the result safe.
    """
    return Markup(render_markdown(text or ""))


def _absolute_url(path: str) -> str:
    """Join an app-relative path onto settings.base_url. Used for canonical
    URLs and og:image where social scrapers need an absolute."""
    if path.startswith(("http://", "https://")):
        return path
    return settings.base_url.rstrip("/") + "/" + path.lstrip("/")


templates.env.filters["markdown"] = _markdown_filter
templates.env.filters["human_size"] = human_size
templates.env.filters["excerpt"] = plain_excerpt
templates.env.filters["absolute_url"] = _absolute_url
templates.env.globals["site_name"] = "BenchLog"
templates.env.globals["site_base_url"] = settings.base_url.rstrip("/")
