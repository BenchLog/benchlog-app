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


def render_markdown_for_project(text: str, slug: str, file_lookup=None) -> str:
    """Render markdown with project file link resolution."""
    from benchlog.markdown_links import rewrite_file_links
    html = md.render(text)
    return rewrite_file_links(html, slug, file_lookup)
