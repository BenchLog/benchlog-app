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

  function mountOne(mount) {
    if (mount.dataset.toastuiInitialized === "1") return;
    mount.dataset.toastuiInitialized = "1";
    const source = document.getElementById(mount.dataset.toastuiSourceId);
    if (!source || !window.toastui) return;
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
    });
    mount.__toastuiEditor = editor;

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
