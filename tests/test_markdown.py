"""Tests for the shared markdown renderer.

Covers the Pygments fence-highlighting wired up via the `highlight` option:
  - Known language fences emit `<pre class="highlight language-X">` with token
    spans so `.highlight .*` CSS rules apply.
  - Fences with no language fall back to the default `<pre><code>` shape.
  - Fences with an unknown language fall back too — we don't want a
    TextLexer wrapper in prose.
"""

from benchlog.markdown import render


def test_fenced_block_with_known_language_is_highlighted():
    html = render("```python\ndef hi():\n    return 1\n```")
    # Outer wrapper starts with <pre class="highlight ..."> so markdown-it
    # skips its default <pre><code> wrapper.
    assert '<pre class="highlight language-python">' in html
    # Pygments tokens land as <span class="k"> etc., picking up the shared
    # `.highlight .k` token styles.
    assert 'class="k"' in html  # keyword (def, return)
    assert 'class="nf"' in html  # function name
    # Inline code is wrapped in <code> for semantics + copy/paste friendliness.
    assert "<code>" in html


def test_fenced_block_without_language_uses_default_wrapper():
    html = render("```\nplain code\n```")
    # No highlight class, no language-* class — just the default wrapper.
    assert "<pre><code>" in html
    assert "highlight" not in html
    assert "language-" not in html


def test_fenced_block_with_unknown_language_falls_back():
    html = render("```zorblax\nstuff\n```")
    # markdown-it's default keeps the raw info-string as language-zorblax,
    # but the token wrapper `<pre class="highlight">` should not appear.
    assert '<pre class="highlight' not in html
    assert "<pre><code" in html


def test_alias_resolves_to_canonical_language_name():
    # `py` is a Pygments alias for the Python lexer. The output class should
    # use the canonical alias, not the raw user string — that's what the
    # CSS and future language-* hooks can rely on.
    html = render("```py\nx = 1\n```")
    assert '<pre class="highlight language-python">' in html


def test_inline_code_is_not_highlighted():
    # Inline code (single backticks) is never fence-highlighted — it stays a
    # bare `<code>` element, handled by the `:not(pre) > code` style rule.
    html = render("Use `foo` for that.")
    assert "<code>foo</code>" in html
    assert "highlight" not in html


# ---------- excalidraw embed ---------- #


def test_excalidraw_embed_renders_placeholder():
    from benchlog.markdown import render_for_project

    lookup = lambda path, filename: (
        "abc-123" if (path, filename) == ("", "diagram.excalidraw") else None
    )
    html = render_for_project("![[diagram.excalidraw]]", "alice", "p", lookup)
    assert "data-excalidraw-embed" in html
    assert 'data-file-id="abc-123"' in html
    assert "/u/alice/p/files/abc-123/raw" in html
    assert 'data-filename="diagram.excalidraw"' in html


def test_excalidraw_embed_default_is_not_editable():
    from benchlog.markdown import render_for_project

    lookup = lambda p, f: "x" if f == "d.excalidraw" else None
    html = render_for_project("![[d.excalidraw]]", "alice", "p", lookup)
    assert 'data-is-owner="0"' in html


def test_excalidraw_embed_editable_for_owner():
    from benchlog.markdown import render_for_project

    lookup = lambda p, f: "x" if f == "d.excalidraw" else None
    html = render_for_project(
        "![[d.excalidraw]]", "alice", "p", lookup, is_owner=True
    )
    assert 'data-is-owner="1"' in html


def test_excalidraw_embed_inline_with_text():
    from benchlog.markdown import render_for_project

    lookup = lambda p, f: "abc" if f == "d.excalidraw" else None
    html = render_for_project(
        "Here it is: ![[d.excalidraw]] (cool)", "alice", "p", lookup
    )
    assert "data-excalidraw-embed" in html


def test_excalidraw_embed_unknown_file_preserves_literal():
    from benchlog.markdown import render_for_project

    html = render_for_project(
        "![[ghost.excalidraw]]", "alice", "p", lambda p, f: None
    )
    # No silent disappearance — author should see the unresolved ref.
    assert "ghost.excalidraw" in html
    assert "data-excalidraw-embed" not in html


def test_excalidraw_embed_subdirectory():
    from benchlog.markdown import render_for_project

    lookup = lambda path, filename: (
        "x" if (path, filename) == ("designs", "v2.excalidraw") else None
    )
    html = render_for_project("![[designs/v2.excalidraw]]", "alice", "p", lookup)
    assert "data-excalidraw-embed" in html
    assert 'data-file-id="x"' in html


def test_excalidraw_embed_only_matches_excalidraw_extension():
    from benchlog.markdown import render_for_project

    # `![[foo.png]]` is NOT an Excalidraw embed — leave it alone. We
    # don't want every Obsidian-style wikilink to compete with the
    # existing `![alt](files/...)` image syntax.
    html = render_for_project(
        "![[photo.png]]", "alice", "p", lambda p, f: "x"
    )
    assert "data-excalidraw-embed" not in html
