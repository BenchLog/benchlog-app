// Picker dialog for the Toast UI "Insert drawing" toolbar button.
//
// Exposes window.BenchlogExcalidrawPicker.open({editor, mount}) — used by
// the toolbar command in toastui-init.js. Reads the per-mount file index
// (`mount.__fileLinkIndex`, populated from `data-toastui-file-index` on
// the editor's mount node) to list existing `.excalidraw` files. Offers
// a "Create new" CTA at the top.
//
// On pick:
//   - existing → insert `![[path/filename]]` at the editor's caret
//   - create new → POST to ${projectUrl}/excalidraw/new with a
//     timestamped name; insert `![[filename]]`; open the modal so the
//     user can draw immediately.

(function () {
  "use strict";

  let dialog = null;
  let listEl = null;
  let createBtn = null;
  let closeBtn = null;
  let emptyEl = null;
  let activeContext = null;

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  function timestampedName() {
    return `drawing-${new Date()
      .toISOString()
      .replace(/[-:T.]/g, "")
      .slice(0, 14)}.excalidraw`;
  }

  function buildDialog() {
    if (dialog) return;
    dialog = document.createElement("dialog");
    dialog.className = "modal excalidraw-picker";
    dialog.setAttribute("aria-label", "Insert drawing");
    dialog.innerHTML = `
      <div class="excalidraw-picker-frame">
        <div class="flex items-baseline justify-between gap-3">
          <h2 class="text-lg font-display m-0">Insert drawing</h2>
          <button type="button" class="btn-ghost text-xs"
                  data-excalidraw-picker-close aria-label="Close">
            <i data-lucide="x" class="w-4 h-4"></i>
          </button>
        </div>
        <button type="button" class="btn-primary text-sm w-full justify-center"
                data-excalidraw-picker-create>
          <i data-lucide="pen-tool" class="inline w-4 h-4 mr-1"></i>
          Create new drawing
        </button>
        <div class="excalidraw-picker-divider">
          <span>or pick an existing one</span>
        </div>
        <div class="excalidraw-picker-list" data-excalidraw-picker-list></div>
        <p class="meta excalidraw-picker-empty" data-excalidraw-picker-empty hidden>
          No drawings in this project yet — use "Create new" above.
        </p>
      </div>
    `;
    document.body.appendChild(dialog);
    listEl = dialog.querySelector("[data-excalidraw-picker-list]");
    emptyEl = dialog.querySelector("[data-excalidraw-picker-empty]");
    createBtn = dialog.querySelector("[data-excalidraw-picker-create]");
    closeBtn = dialog.querySelector("[data-excalidraw-picker-close]");
    closeBtn.addEventListener("click", () => dialog.close());
    createBtn.addEventListener("click", () => createNew());
  }

  function reference(path, filename) {
    return path ? `${path}/${filename}` : filename;
  }

  function insertAtCursor(text) {
    const editor = activeContext?.editor;
    if (!editor) return;
    editor.insertText(text);
    editor.focus();
  }

  function pickExisting(path, filename) {
    insertAtCursor(`![[${reference(path, filename)}]]`);
    dialog.close();
  }

  async function createNew() {
    const projectUrl = activeContext?.projectUrl;
    if (!projectUrl) return;
    createBtn.disabled = true;
    const fd = new FormData();
    fd.append("_csrf", csrfToken());
    fd.append("name", timestampedName());
    try {
      const resp = await fetch(`${projectUrl}/excalidraw/new`, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) {
        let detail = `Could not create drawing (${resp.status}).`;
        try {
          const data = await resp.json();
          if (data?.detail) detail = data.detail;
        } catch (_) {}
        window.alert(detail);
        return;
      }
      const payload = await resp.json();
      insertAtCursor(`![[${reference(payload.path || "", payload.filename)}]]`);
      // Close the picker first.
      dialog.close();

      // If the picker was opened from inside another <dialog> (the
      // new-entry / description-edit modal), that dialog is still in
      // the browser's top layer — anything fixed-positioned (like our
      // Excalidraw modal) renders BELOW it. Temporarily close those
      // parent dialogs so the Excalidraw modal is visible, and reopen
      // them when the Excalidraw modal closes so the user lands back
      // in their drafting context.
      const stashedDialogs = Array.from(
        document.querySelectorAll("dialog[open]"),
      ).filter((d) => d !== dialog);
      stashedDialogs.forEach((d) => d.close());

      if (window.BenchlogExcalidrawModal) {
        window.BenchlogExcalidrawModal.open({
          projectUrl,
          fileId: payload.id,
          filename: payload.filename,
          filePath: payload.path || "",
          fileDescription: "",
          sceneUrl: `${projectUrl}/files/${payload.id}/raw`,
          isOwner: true,
          onClose: () => {
            // Reopen any parent dialogs we closed. `showModal()` puts
            // the dialog back in the top layer; the user resumes
            // editing the post they were drafting.
            for (const d of stashedDialogs) {
              try {
                d.showModal();
              } catch (_) { /* dialog may already be removed from DOM */ }
            }
          },
        });
      }
    } catch (_) {
      window.alert("Could not create drawing — network error.");
    } finally {
      createBtn.disabled = false;
    }
  }

  function renderList(fileIndex) {
    listEl.innerHTML = "";
    const drawings = (fileIndex || [])
      .filter((f) => /\.excalidraw$/i.test(f.filename))
      .sort((a, b) => {
        const an = reference(a.path || "", a.filename);
        const bn = reference(b.path || "", b.filename);
        return an.localeCompare(bn);
      });
    if (!drawings.length) {
      emptyEl.hidden = false;
      return;
    }
    emptyEl.hidden = true;
    for (const f of drawings) {
      const ref = reference(f.path || "", f.filename);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "excalidraw-picker-item";
      btn.innerHTML = `
        <i data-lucide="pen-tool" class="w-3.5 h-3.5"></i>
        <span class="excalidraw-picker-item-name">${ref.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</span>
      `;
      btn.addEventListener("click", () => pickExisting(f.path || "", f.filename));
      listEl.appendChild(btn);
    }
  }

  function open(opts) {
    const { editor, mount, projectUrl } = opts;
    if (!editor || !projectUrl) return;
    buildDialog();
    activeContext = { editor, projectUrl };
    const fileIndex = mount?.__fileLinkIndex || [];
    renderList(fileIndex);
    dialog.showModal();
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function close() {
    if (dialog?.open) dialog.close();
  }

  window.BenchlogExcalidrawPicker = { open, close };
})();
