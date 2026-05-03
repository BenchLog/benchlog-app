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

  // Default toolbar layout — mirrors toast-ui's out-of-the-box grouping
  // minus `scrollSync`. Sync is a toggle for split-pane scroll mirroring;
  // mobile only renders one pane so the switch does nothing there, and
  // dropping it gives the overflow `…` button less to swallow on phones.
  // Any override of `toolbarItems` replaces the default wholesale, so we
  // copy the full list explicitly.
  const DEFAULT_TOOLBAR = [
    ["heading", "bold", "italic", "strike"],
    ["hr", "quote"],
    ["ul", "ol", "task", "indent", "outdent"],
    ["table", "image", "link"],
    ["code", "codeblock"],
  ];

  function buildToolbar(hasFileIndex, hasEntryIndex, hasExcalidraw) {
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
    if (hasExcalidraw) {
      extras.push({
        name: "excalidraw",
        // Kept short — Toast UI's overflow toolbar strip clips long
        // tooltips when the editor is hosted inside a modal whose CSS
        // applies overflow: hidden.
        tooltip: "Insert drawing",
        className: "benchlog-toolbar-excalidraw",
        command: "benchlogInsertExcalidraw",
      });
    }
    if (!extras.length) return DEFAULT_TOOLBAR;
    return [...DEFAULT_TOOLBAR, extras];
  }

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  // Toast-ui's default image handler base64-encodes the blob into the
  // markdown source. That breaks two things at scale: (1) Postgres rejects
  // the resulting tsvector when the description grows past ~1 MB, and
  // (2) every render serializes a multi-MB string into the DOM. The hook
  // we install instead uploads the blob through the project files
  // endpoint and inserts a `files/<filename>` reference, which the
  // server-side markdown rewriter resolves to a canonical `/raw` URL.
  // That keeps embeds resilient to filename normalization and means the
  // image lives in the file tree where it can be moved or replaced.
  function makeImageUploadHook(uploadUrl) {
    return async (blob, callback) => {
      const file = blob instanceof File
        ? blob
        : new File([blob], blob.name || "pasted-image", { type: blob.type });
      const fd = new FormData();
      fd.append("_csrf", csrfToken());
      fd.append("path", "");
      fd.append("description", "");
      fd.append("upload", file, file.name);
      try {
        const resp = await fetch(uploadUrl, {
          method: "POST",
          body: fd,
          headers: { Accept: "application/json" },
        });
        if (!resp.ok) {
          let msg = `Image upload failed (${resp.status}).`;
          try {
            const data = await resp.json();
            if (data && typeof data.detail === "string") msg = data.detail;
          } catch (_) { /* keep default */ }
          window.alert(msg);
          return;
        }
        const data = await resp.json();
        // Use the server-canonical filename — `safe_filename` strips
        // unsafe characters and the upload route may rewrite HEIC to
        // JPEG, so the original blob name can drift from what's stored.
        // Always inserted at the project root since `path` was empty;
        // the user can move it via the Files tab afterward and the
        // markdown reference will follow if they update the link.
        const filename = data.filename || file.name;
        const insert = () => callback(`files/${filename}`, file.name);

        if (data.is_quarantined && window.benchlogGpsReview) {
          // GPS detected — defer the editor insert until the user picks
          // Strip / Keep / Discard. Crucially we do NOT reload (would
          // wipe unsaved description content); on Discard we just don't
          // insert anything and the file is gone. The page that hosts
          // the editor must include `_gps_review_modal.html` and load
          // gps-review.js.
          window.benchlogGpsReview.show([data], {
            batchUrlBase: uploadUrl,
            csrfToken: csrfToken(),
            onDone: ({ action }) => {
              if (action === "discard") return;
              insert();
            },
          });
          return;
        }

        // Quarantined fallback when the modal isn't loaded on this page:
        // alert the user so they're not staring at a broken image with
        // no idea what happened.
        if (data.is_quarantined) {
          window.alert(
            "Image was uploaded but contains GPS data and is awaiting " +
              "review. Visit the project's Files tab to strip or keep it.",
          );
          return;
        }
        insert();
      } catch (_) {
        window.alert("Image upload failed — network error.");
      }
    };
  }

  // Stand-in for editors with no project context (bio, collection forms).
  // Keeps the user from silently base64-embedding multi-MB images that
  // the server-side description size cap would later reject.
  const NO_UPLOAD_HOOK = (_blob, _callback) => {
    window.alert(
      "Image embedding isn't supported in this editor. Add a link to an " +
        "image hosted elsewhere instead.",
    );
  };

  function mountOne(mount) {
    if (mount.dataset.toastuiInitialized === "1") return;
    mount.dataset.toastuiInitialized = "1";
    const source = document.getElementById(mount.dataset.toastuiSourceId);
    if (!source || !window.toastui) return;

    const hasFileIndex = Boolean(mount.dataset.toastuiFileIndex);
    const hasEntryIndex = Boolean(mount.dataset.toastuiEntryIndex);
    const uploadUrl = mount.dataset.toastuiUploadUrl || "";
    // Project URL = upload URL minus the trailing `/files`. The
    // Excalidraw "Insert drawing" toolbar command POSTs to
    // `${projectUrl}/excalidraw/new`; mounts without an upload URL
    // (bio editor, etc.) get no Excalidraw button.
    const projectUrl = uploadUrl.endsWith("/files")
      ? uploadUrl.slice(0, -"/files".length)
      : "";
    const hasExcalidraw = Boolean(projectUrl);

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
      toolbarItems: buildToolbar(hasFileIndex, hasEntryIndex, hasExcalidraw),
      hooks: {
        addImageBlobHook: uploadUrl
          ? makeImageUploadHook(uploadUrl)
          : NO_UPLOAD_HOOK,
      },
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
    if (hasExcalidraw) {
      // Defer to BenchlogExcalidrawPicker (loaded by the same template).
      // The picker reads the per-mount __fileLinkIndex (parsed below) to
      // populate the existing-drawings list, and uses projectUrl to POST
      // to `/excalidraw/new` for the "Create new" branch.
      const openPicker = () => {
        if (!window.BenchlogExcalidrawPicker) {
          window.alert("Drawing picker failed to load.");
          return false;
        }
        window.BenchlogExcalidrawPicker.open({ editor, mount, projectUrl });
        return true;
      };
      editor.addCommand("markdown", "benchlogInsertExcalidraw", openPicker);
      editor.addCommand("wysiwyg", "benchlogInsertExcalidraw", openPicker);
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
