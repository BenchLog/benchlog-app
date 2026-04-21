"""Tests for the `files/…` markdown reference rewriter.

Covers the text-in / text-out helpers in `benchlog.file_references`.
Route-integration (does a rename actually patch description + journal?)
is in `tests/test_files.py`.
"""

from benchlog.file_references import (
    rewrite_file_references,
    rewrite_folder_references,
)


# ---------- per-file rewrite ---------- #


def test_rewrite_simple_link():
    result = rewrite_file_references(
        "See [the model](files/a.stl) for details.", "a.stl", "b.stl"
    )
    assert result.text == "See [the model](files/b.stl) for details."
    assert result.count == 1


def test_rewrite_image_link():
    result = rewrite_file_references(
        "![render](files/foo.png)", "foo.png", "bar.png"
    )
    assert result.text == "![render](files/bar.png)"
    assert result.count == 1


def test_rewrite_link_with_title():
    result = rewrite_file_references(
        'Download [here](files/a.stl "latest model").',
        "a.stl",
        "b.stl",
    )
    assert result.text == 'Download [here](files/b.stl "latest model").'
    assert result.count == 1


def test_rewrite_multiple_refs():
    src = (
        "[one](files/a.stl)\n"
        "[two](files/a.stl)\n"
        "Also [three](files/a.stl).\n"
    )
    result = rewrite_file_references(src, "a.stl", "b.stl")
    assert result.count == 3
    assert result.text.count("files/b.stl") == 3
    assert "files/a.stl" not in result.text


def test_rewrite_preserves_non_matching_refs():
    src = "[other](files/other.stl) and [target](files/a.stl)"
    result = rewrite_file_references(src, "a.stl", "b.stl")
    assert result.count == 1
    assert "files/other.stl" in result.text
    assert "files/b.stl" in result.text


def test_rewrite_ignores_fenced_code_block():
    src = (
        "Prose [x](files/a.stl)\n"
        "\n"
        "```\n"
        "[inside](files/a.stl)\n"
        "```\n"
    )
    result = rewrite_file_references(src, "a.stl", "b.stl")
    # Outside fence rewritten, inside untouched.
    assert "Prose [x](files/b.stl)" in result.text
    assert "[inside](files/a.stl)" in result.text
    assert result.count == 1


def test_rewrite_ignores_tilde_fence():
    src = (
        "~~~\n"
        "[inside](files/a.stl)\n"
        "~~~\n"
        "Outside [x](files/a.stl)\n"
    )
    result = rewrite_file_references(src, "a.stl", "b.stl")
    assert "[inside](files/a.stl)" in result.text
    assert "Outside [x](files/b.stl)" in result.text
    assert result.count == 1


def test_rewrite_ignores_inline_code():
    src = "Use `[x](files/a.stl)` as a template. [actual](files/a.stl) is here."
    result = rewrite_file_references(src, "a.stl", "b.stl")
    assert "`[x](files/a.stl)`" in result.text
    assert "[actual](files/b.stl)" in result.text
    assert result.count == 1


def test_rewrite_root_level_file():
    result = rewrite_file_references(
        "See [here](files/readme.md)", "readme.md", "docs/readme.md"
    )
    assert result.text == "See [here](files/docs/readme.md)"
    assert result.count == 1


def test_rewrite_same_old_and_new_is_noop():
    src = "Unchanged [x](files/a.stl)"
    result = rewrite_file_references(src, "a.stl", "a.stl")
    assert result.text == src
    assert result.count == 0


def test_rewrite_does_not_touch_exact_prefix_match():
    """`a.stl` rename must NOT rewrite `a.stl.bak` or `a.stlx`."""
    src = "[a](files/a.stl) [longer](files/a.stl.bak)"
    result = rewrite_file_references(src, "a.stl", "b.stl")
    assert "[a](files/b.stl)" in result.text
    assert "[longer](files/a.stl.bak)" in result.text
    assert result.count == 1


# ---------- folder rewrite ---------- #


def test_folder_rewrite_preserves_suffix():
    result = rewrite_folder_references(
        "[w](files/models/widget.stl)", "models", "stl"
    )
    assert result.text == "[w](files/stl/widget.stl)"
    assert result.count == 1


def test_folder_rewrite_handles_nested_file():
    result = rewrite_folder_references(
        "[deep](files/models/sub/x.stl)", "models", "stl"
    )
    assert result.text == "[deep](files/stl/sub/x.stl)"
    assert result.count == 1


def test_folder_rewrite_does_not_match_prefix_subset():
    """Folder `model` rewrite must NOT match `files/models/…`."""
    src = "[m](files/models/x.stl) [plain](files/model)"
    result = rewrite_folder_references(src, "model", "animal")
    # `models/x.stl` shouldn't be touched — `model/` is the boundary.
    assert "[m](files/models/x.stl)" in result.text
    # `files/model` alone (no trailing slash) shouldn't match either —
    # it's not `files/model/<rest>`.
    assert "[plain](files/model)" in result.text
    assert result.count == 0


def test_folder_rewrite_ignores_fenced_block():
    src = (
        "```\n"
        "[keep](files/models/x.stl)\n"
        "```\n"
        "[move](files/models/y.stl)\n"
    )
    result = rewrite_folder_references(src, "models", "stl")
    assert "[keep](files/models/x.stl)" in result.text
    assert "[move](files/stl/y.stl)" in result.text
    assert result.count == 1


def test_folder_rewrite_preserves_title():
    result = rewrite_folder_references(
        '[w](files/models/widget.stl "source model")',
        "models",
        "stl",
    )
    assert result.text == '[w](files/stl/widget.stl "source model")'
    assert result.count == 1


def test_folder_rewrite_same_old_and_new_is_noop():
    src = "[x](files/models/a.stl)"
    result = rewrite_folder_references(src, "models", "models")
    assert result.text == src
    assert result.count == 0


# ---- link-text rewriting ---- #
# When the reference URL is rewritten, occurrences of the old name inside
# the link's display text should follow along so the rendered text stays
# truthful. Only the link whose URL we're rewriting is touched — arbitrary
# prose that happens to mention the old name elsewhere is left alone.


def test_rewrite_replaces_old_name_in_link_text():
    result = rewrite_file_references(
        "Grab [widget.stl](files/widget.stl) now.", "widget.stl", "gadget.stl"
    )
    assert result.text == "Grab [gadget.stl](files/gadget.stl) now."
    assert result.count == 1


def test_rewrite_replaces_old_name_embedded_in_longer_text():
    result = rewrite_file_references(
        "Download [my widget.stl here](files/widget.stl).",
        "widget.stl",
        "gadget.stl",
    )
    assert (
        result.text
        == "Download [my gadget.stl here](files/gadget.stl)."
    )


def test_rewrite_replaces_old_name_multiple_times_in_same_text():
    result = rewrite_file_references(
        "[widget.stl vs widget.stl](files/widget.stl)",
        "widget.stl",
        "gadget.stl",
    )
    assert result.text == "[gadget.stl vs gadget.stl](files/gadget.stl)"


def test_rewrite_leaves_text_alone_when_old_name_absent():
    # Author labelled the link with prose unrelated to the filename — the
    # text must stay as-is; only the URL updates.
    result = rewrite_file_references(
        "[latest model](files/widget.stl)", "widget.stl", "gadget.stl"
    )
    assert result.text == "[latest model](files/gadget.stl)"


def test_rewrite_prefers_full_path_over_basename_in_text():
    # File moved from `models/` to `stl/`, also renamed. Text mentioning
    # the full old path gets the full new path; any standalone basename
    # mentions update to the new basename.
    result = rewrite_file_references(
        "See [models/widget.stl and also widget.stl](files/models/widget.stl).",
        "models/widget.stl",
        "stl/gadget.stl",
    )
    assert (
        result.text
        == "See [stl/gadget.stl and also gadget.stl](files/stl/gadget.stl)."
    )


def test_rewrite_doesnt_touch_other_links_text():
    # Only the link whose URL is being rewritten has its text adjusted.
    # A sibling link mentioning `widget.stl` but pointing elsewhere stays
    # untouched.
    src = (
        "[widget.stl](files/widget.stl) "
        "[backup](files/other.stl \"widget.stl reference\")"
    )
    result = rewrite_file_references(src, "widget.stl", "gadget.stl")
    assert (
        result.text
        == "[gadget.stl](files/gadget.stl) "
        "[backup](files/other.stl \"widget.stl reference\")"
    )


def test_folder_rewrite_replaces_old_folder_prefix_in_text():
    result = rewrite_folder_references(
        "Pick [models/a.stl](files/models/a.stl).", "models", "stl"
    )
    assert result.text == "Pick [stl/a.stl](files/stl/a.stl)."


def test_folder_rewrite_leaves_bare_folder_mention_in_text_alone():
    # Rewrite only triggers on `<old_folder>/` (with the slash) so a bare
    # mention of the folder name without slash stays put — avoids mangling
    # unrelated prose.
    result = rewrite_folder_references(
        "[The models folder: models/a.stl](files/models/a.stl)",
        "models",
        "stl",
    )
    assert (
        result.text
        == "[The models folder: stl/a.stl](files/stl/a.stl)"
    )
