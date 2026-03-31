from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

md = (
    MarkdownIt("gfm-like", {"linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)

tasklists_plugin(md)
footnote_plugin(md)


def render_markdown(text: str) -> str:
    """Render markdown text to HTML using GFM-like rules."""
    return md.render(text)
