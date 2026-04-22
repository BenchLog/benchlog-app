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
