/*
 * Toast UI editor initializer.
 *
 * Finds every `[data-toastui-mount]` in the DOM (or inside a given root) and
 * boots a toastui.Editor against the sibling hidden textarea referenced by
 * `data-toastui-source-id`. The textarea stays in the DOM so form submits
 * naturally carry the markdown — we just keep its value synced on every
 * change and on the enclosing form's submit event so a fast submit-after-
 * typing doesn't drop the last keystroke.
 *
 * Exposes `window.initToastuiEditors(root)` so callers that reveal new
 * mounts dynamically (see description-edit.js) can reinitialize them.
 */
(() => {
  "use strict";

  // Default toolbar layout — mirrors toast-ui's out-of-the-box grouping so
  // the editor keeps every built-in action. We copy it explicitly because
  // any override of `toolbarItems` replaces the default wholesale.
  const DEFAULT_TOOLBAR = [
    ["heading", "bold", "italic", "strike"],
    ["hr", "quote"],
    ["ul", "ol", "task", "indent", "outdent"],
    ["table", "image", "link"],
    ["code", "codeblock"],
    ["scrollSync"],
  ];

  function buildToolbar(hasFileIndex, hasEntryIndex) {
    const extras = [];
    if (hasFileIndex) {
      // Using `className` + `command` (rather than a custom `el`) keeps
      // toast-ui in charge of the button element: same wrapper, same
      // dimensions, same hover/focus behaviour as every built-in item.
      // Icon glyph is delivered via `mask-image` on the className — see
      // input.css. The command is registered on the editor below.
      extras.push({
        name: "fileLink",
        tooltip: "Insert file link",
        className: "benchlog-toolbar-file-link",
        command: "benchlogInsertFileLink",
      });
    }
    if (hasEntryIndex) {
      extras.push({
        name: "journalLink",
        tooltip: "Insert journal entry link",
        className: "benchlog-toolbar-journal-link",
        command: "benchlogInsertJournalLink",
      });
    }
    if (!extras.length) return DEFAULT_TOOLBAR;
    return [...DEFAULT_TOOLBAR, extras];
  }

  function mountOne(mount) {
    if (mount.dataset.toastuiInitialized === "1") return;
    mount.dataset.toastuiInitialized = "1";
    const source = document.getElementById(mount.dataset.toastuiSourceId);
    if (!source || !window.toastui) return;

    const hasFileIndex = Boolean(mount.dataset.toastuiFileIndex);
    const hasEntryIndex = Boolean(mount.dataset.toastuiEntryIndex);

    const editor = new window.toastui.Editor({
      el: mount,
      initialValue: source.value,
      initialEditType: "markdown",
      // "tab" preview keeps the editor compact: one pane at a time (write /
      // preview) rather than a split-screen that eats horizontal space. Feels
      // closer in weight to the form inputs around it.
      previewStyle: "tab",
      usageStatistics: false,
      // The resizer wrapper (`.toastui-editor-resizer`) sets the visible
      // height and owns the drag handle; the editor fills it so a user
      // drag expands the whole thing in natural document flow and the
      // enclosing card grows with it. minHeight here floors toastui's
      // internal writer so it doesn't collapse below a usable size.
      height: "100%",
      minHeight: "120px",
      placeholder: mount.dataset.toastuiPlaceholder || "",
      // Keep the markdown / WYSIWYG tabs visible at the bottom-left so
      // users who prefer WYSIWYG can still flip — default is markdown.
      hideModeSwitch: false,
      toolbarItems: buildToolbar(hasFileIndex, hasEntryIndex),
    });
    mount.__toastuiEditor = editor;

    // Register the commands the custom toolbar buttons dispatch. Each
    // inserts its trigger at the caret, which the file-link autocomplete
    // module picks up on the next `change` event and surfaces its panel.
    if (hasFileIndex) {
      editor.addCommand("markdown", "benchlogInsertFileLink", () => {
        editor.insertText("files/");
        editor.focus();
        return true;
      });
      editor.addCommand("wysiwyg", "benchlogInsertFileLink", () => {
        editor.insertText("files/");
        editor.focus();
        return true;
      });
    }
    if (hasEntryIndex) {
      editor.addCommand("markdown", "benchlogInsertJournalLink", () => {
        editor.insertText("journal/");
        editor.focus();
        return true;
      });
      editor.addCommand("wysiwyg", "benchlogInsertJournalLink", () => {
        editor.insertText("journal/");
        editor.focus();
        return true;
      });
    }

    // Project-scoped editors carry a JSON file index so the `files/…`
    // typeahead module can offer completions. Parse once and cache on the
    // mount; absent attribute → typeahead stays disabled for this editor
    // (bio editor, anything outside a project context). The sibling
    // `journal/…` index is parsed alongside — same gate, empty list
    // allowed, so a project with no titled entries just shows files.
    if (mount.dataset.toastuiFileIndex) {
      try {
        mount.__fileLinkIndex = JSON.parse(mount.dataset.toastuiFileIndex);
      } catch (_) {
        mount.__fileLinkIndex = [];
      }
      if (mount.dataset.toastuiEntryIndex) {
        try {
          mount.__journalEntryIndex = JSON.parse(
            mount.dataset.toastuiEntryIndex,
          );
        } catch (_) {
          mount.__journalEntryIndex = [];
        }
      } else {
        mount.__journalEntryIndex = [];
      }
      // Loose coupling: the autocomplete module registers itself on window
      // after this file loads (both tags use `defer`, so load order is
      // preserved). If the module isn't present for any reason, the editor
      // still works — the typeahead just doesn't attach.
      if (typeof window.initFileLinkAutocomplete === "function") {
        window.initFileLinkAutocomplete(mount);
      }
    }

    // Sync on every change — cheap, and avoids races with fast submits.
    editor.on("change", () => {
      source.value = editor.getMarkdown();
    });

    // Belt-and-braces: also sync on form submit so programmatic submits
    // don't race the change event.
    const form = source.closest("form");
    if (form) {
      form.addEventListener("submit", () => {
        source.value = editor.getMarkdown();
      });
    }
  }

  function mountAll(root) {
    (root || document)
      .querySelectorAll("[data-toastui-mount]")
      .forEach(mountOne);
  }

  window.initToastuiEditors = mountAll;
  document.addEventListener("DOMContentLoaded", () => mountAll());
})();
