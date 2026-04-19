from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from benchlog.markdown import render as render_markdown

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _markdown_filter(text: str | None) -> Markup:
    """Render markdown to HTML, marked safe so Jinja won't double-escape.

    markdown-it-py's "gfm-like" preset leaves `html: false`, so raw HTML
    tags in user input get escaped before we mark the result safe.
    """
    return Markup(render_markdown(text or ""))


templates.env.filters["markdown"] = _markdown_filter
