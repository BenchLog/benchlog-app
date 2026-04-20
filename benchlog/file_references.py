"""Rewrite `files/<path>` markdown link/image references when a file or
folder moves.

The renderer (`benchlog.markdown.rewrite_project_file_links`) falls back
to the files browser for broken `files/…` hrefs, so no rewrite leaves
readers on a 404 — but the author-written markdown still points at the
old path. When an owner renames a file or folder we patch the source
markdown so the reference itself stays truthful.

Only the author-facing `]( ... )` portion of link/image syntax is
touched. Fenced code blocks, tilde fences, HTML blocks, and inline code
spans are left alone so a `files/foo` literal inside a code sample isn't
mistaken for a live reference.

The masking strategy is a **line-scan + inline-code regex**, not a full
token walk:

* Block-level (fences, HTML blocks) are tracked by a small line-scanner
  that flips state on `^\`\`\``/`^~~~` (fenced) and paired HTML tags.
  markdown-it-py's token stream reports `map = [start_line, end_line]`
  for these too, but using line-indexed state here keeps the module a
  pure text-in/text-out helper with no dependency on the parser for
  correctness — the parser is only used to sanity-check the edge cases
  covered by tests.
* Inline code is masked per-prose-line with a balanced-backtick regex.
  This covers `` `files/foo` `` in the common case. CommonMark's
  n-backticks-open/n-backticks-close rule is nuanced (see
  https://spec.commonmark.org/0.31.2/#code-spans), but the approximation
  here matches every realistic reference in user content.

If you're extending this module for a new kind of rename, add a test in
`tests/test_file_references.py` for the edge case first — the masking
is intentionally conservative and breaking it silently would re-introduce
the problem this module was created to solve.
"""

import re
from typing import NamedTuple


class RewriteResult(NamedTuple):
    text: str
    count: int


# Matches the `]( ... )` tail of a markdown link or image. We only want
# the href (first group), not the author-facing `[...]` label, because
# the label is prose the user wrote — they might have typed the old
# filename on purpose.
#
# Link title: CommonMark allows `"…"`, `'…'`, or `(…)` — the third form
# would be ambiguous with the closing paren of the link itself, so we
# only match the two quoted variants here. The autocomplete never emits
# a title and hand-written titles are rare, but when present we preserve
# them verbatim.
_TITLE_RE = r"(\s+(?:\"[^\"]*\"|'[^']*'))?"


def _compile_file_rewrite(old_full_path: str) -> re.Pattern[str]:
    """Build the per-file link/image rewrite pattern.

    Captures the full `(!)?[text](files/<old>…)` form so we can sub the
    old filename inside the link's display text as well as the href. The
    leading `!?` matches images; `(?<!\\)` guards against a user having
    escaped the opening bracket.
    """
    return re.compile(
        r"(?<!\\)(!?)\[([^\]]*)\]\(files/"
        + re.escape(old_full_path)
        + _TITLE_RE
        + r"\)"
    )


def _compile_folder_rewrite(old_folder: str) -> re.Pattern[str]:
    """Build the folder-prefix rewrite pattern.

    Also captures the full `(!)?[text]` span so occurrences of
    ``<old_folder>/`` in the display text can be rewritten too. The
    `<rest>` capture deliberately stops at whitespace / `)` / quote —
    it's the remainder of the path after the folder, not a free-form
    URL tail, so we don't want to swallow a trailing title or the
    closing paren.
    """
    return re.compile(
        r"(?<!\\)(!?)\[([^\]]*)\]\(files/"
        + re.escape(old_folder)
        + r"/([^)\s\"']+)"
        + _TITLE_RE
        + r"\)"
    )


# Fence openers/closers. CommonMark treats a line starting with 3+
# backticks OR 3+ tildes as a fence; the closer must be the same char
# and at least as long as the opener. We track the opener length to get
# close-matching right — an inner ```` ```` run of 4 backticks doesn't
# close a 3-backtick fence.
_FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})")

# Block-level HTML: a line starting with `<tag` (or `</tag`) at column 0-3
# opens an HTML block that runs until a blank line or a matching close,
# depending on the subtype. For masking purposes we only need to know
# "this line is inside an HTML block" — the exact CommonMark subtype 6/7
# heuristics would be overkill. We approximate: once we see a line that
# starts with `<` and the previous line is blank (or start-of-doc), we're
# in an HTML block until the next blank line. This covers the realistic
# cases — a `<div>…</div>` with inline markdown disabled — and errs on
# the side of NOT rewriting inside HTML blocks, which is the safe bias.
_HTML_BLOCK_OPEN_RE = re.compile(r"^\s{0,3}<[a-zA-Z/!?]")


# Inline code: backtick-delimited run on a single line. We use the
# minimal-match form so adjacent spans don't merge. CommonMark's
# n-backticks-open / n-backticks-close rule isn't fully honoured here
# (a span opened with ``` `` ``` should only close on ``` `` ```, not a
# single `), but for `files/…` references this is adequate: a single-
# backtick span is what autocomplete users type.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _mask_inline_code(line: str) -> str:
    """Return `line` with inline-code runs replaced by `\x00`-padding.

    The padding keeps character offsets aligned so subsequent regex
    operations on the masked line still correspond to the original
    positions — callers that need the original line reassemble by
    overlaying the masked slice back.
    """
    return _INLINE_CODE_RE.sub(lambda m: "\x00" * len(m.group(0)), line)


def _iter_prose_lines(md_text: str):
    """Yield `(line_index, raw_line, is_prose)` for each line in md_text.

    `is_prose` is False inside fenced code blocks, tilde fences, and
    HTML blocks. Callers apply rewrites only to prose lines. Inline
    code is *not* handled here — do that per-line with `_mask_inline_code`
    because inline code can sit next to rewritable prose on the same line.
    """
    lines = md_text.split("\n")
    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    in_html_block = False
    prev_blank = True
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_blank = not stripped

        if in_fence:
            # Inside a fence — check for a close that matches the opener.
            m = _FENCE_RE.match(line)
            if m and m.group(2)[0] == fence_char and len(m.group(2)) >= fence_len:
                in_fence = False
                fence_char = None
                fence_len = 0
            yield i, line, False
            prev_blank = is_blank
            continue

        if in_html_block:
            if is_blank:
                in_html_block = False
                yield i, line, True  # blank line outside HTML block again
                prev_blank = True
                continue
            yield i, line, False
            prev_blank = is_blank
            continue

        # Not inside anything — check if this line OPENS a fence or HTML block.
        m = _FENCE_RE.match(line)
        if m:
            in_fence = True
            fence_char = m.group(2)[0]
            fence_len = len(m.group(2))
            yield i, line, False
            prev_blank = is_blank
            continue

        if prev_blank and _HTML_BLOCK_OPEN_RE.match(line):
            in_html_block = True
            yield i, line, False
            prev_blank = is_blank
            continue

        yield i, line, True
        prev_blank = is_blank


def _rewrite_prose(md_text: str, pattern: re.Pattern[str], replacement) -> tuple[str, int]:
    """Run `pattern.sub(replacement, …)` on prose lines only.

    `replacement` is a callable `(match) -> str` so folder rewrites can
    reconstruct the `<rest>` suffix. Inline code is masked out before
    matching so a ref inside `` `files/foo` `` stays untouched, then the
    original characters are re-assembled for output.
    """
    total = 0
    out_lines: list[str] = []
    for _i, line, is_prose in _iter_prose_lines(md_text):
        if not is_prose:
            out_lines.append(line)
            continue
        # Mask inline code spans with NUL bytes that preserve char offsets.
        # Then run the substitution on the ORIGINAL line, but skip matches
        # whose span sits inside a masked region — that keeps a
        # `files/foo.stl` sitting in a backtick span from being rewritten.
        masked = _mask_inline_code(line)
        line_count = 0

        def _guarded(m: re.Match[str]) -> str:
            if "\x00" in masked[m.start():m.end()]:
                return m.group(0)
            nonlocal line_count
            line_count += 1
            return replacement(m)

        new_line = pattern.sub(_guarded, line)
        out_lines.append(new_line)
        total += line_count
    return "\n".join(out_lines), total


def rewrite_file_references(
    markdown_text: str,
    old_full_path: str,
    new_full_path: str,
) -> RewriteResult:
    """Rewrite `](files/<old_full_path>)` references to the new path.

    `old_full_path` / `new_full_path` are the full virtual path a user
    would type — `"models/widget.stl"` for a file inside a folder, or
    just `"widget.stl"` for a root-level file. Matches exact-path only;
    a file named `"widget.stl"` won't match a reference to
    `"models/widget.stl"`.

    Leaves code blocks, inline code, and HTML blocks untouched. Returns
    the new text and how many refs were rewritten. If old == new, the
    text is returned unchanged with count 0.
    """
    if not markdown_text or old_full_path == new_full_path:
        return RewriteResult(markdown_text or "", 0)

    pattern = _compile_file_rewrite(old_full_path)
    old_basename = old_full_path.rsplit("/", 1)[-1]
    new_basename = new_full_path.rsplit("/", 1)[-1]

    def _sub(m: re.Match[str]) -> str:
        bang = m.group(1)
        text = m.group(2)
        title = m.group(3) or ""
        # Rewrite the author-facing label too. Full path first (more
        # specific) so `[models/widget.stl]` becomes `[stl/widget.stl]`
        # cleanly when the file both moved and got renamed; then the
        # basename to catch standalone mentions like `[widget.stl]`.
        # `str.replace` is all-occurrences — matches the "even if it's
        # part of a longer text" intent.
        new_text = text.replace(old_full_path, new_full_path)
        if old_basename != new_basename:
            new_text = new_text.replace(old_basename, new_basename)
        return f"{bang}[{new_text}](files/{new_full_path}{title})"

    new_text, count = _rewrite_prose(markdown_text, pattern, _sub)
    return RewriteResult(new_text, count)


def rewrite_folder_references(
    markdown_text: str,
    old_folder: str,
    new_folder: str,
) -> RewriteResult:
    """Rewrite `](files/<old_folder>/<rest>)` references to use the new folder.

    The `<rest>` portion (everything after the folder's own path
    segment, up to whitespace / `)` / quote) is preserved verbatim, so
    `files/models/sub/x.stl` becomes `files/<new_folder>/sub/x.stl`.

    Boundary-checked: a folder rename `model → animal` does NOT rewrite
    `files/models/…` — the trailing `/` in the pattern enforces that
    the old folder is a complete path segment.
    """
    if not markdown_text or old_folder == new_folder:
        return RewriteResult(markdown_text or "", 0)

    pattern = _compile_folder_rewrite(old_folder)
    old_prefix = f"{old_folder}/"
    new_prefix = f"{new_folder}/"

    def _sub(m: re.Match[str]) -> str:
        bang = m.group(1)
        text = m.group(2)
        rest = m.group(3)
        title = m.group(4) or ""
        # Rewrite occurrences of `<old_folder>/` inside the link's
        # display text too — same "even if it's part of a longer text"
        # intent as the per-file version. The trailing `/` keeps a bare
        # mention of the folder's name (without slash) alone, which
        # avoids mangling unrelated prose that happens to share the name.
        new_text = text.replace(old_prefix, new_prefix)
        return f"{bang}[{new_text}](files/{new_folder}/{rest}{title})"

    new_text, count = _rewrite_prose(markdown_text, pattern, _sub)
    return RewriteResult(new_text, count)
