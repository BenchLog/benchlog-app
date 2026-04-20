/*
 * Inline description edit flow for project detail pages.
 *
 * Click "Edit" → the editor expands in place with toastui. Save → AJAX
 * POST to the description endpoint; on success the server returns rendered
 * HTML which we swap into the read view without a full reload. Cancel
 * restores the original rendered view.
 *
 * The editor itself is mounted lazily (on first open) so toastui doesn't
 * boot on page load for owners who never click Edit — keeps first paint
 * cheap when you're just reading. `window.initToastuiEditors` (from
 * toastui-init.js) handles the mount once we inject the source textarea
 * and mount div into the slot.
 */
(() => {
  "use strict";

  const section = document.querySelector("[data-description-section]");
  if (!section) return;

  const editBtn = section.querySelector("[data-description-edit]");
  const renderedEl = section.querySelector("[data-description-rendered]");
  const form = section.querySelector("[data-description-edit-form]");
  if (!editBtn || !form || !renderedEl) return;

  const cancelBtn = form.querySelector("[data-description-cancel]");
  const saveBtn = form.querySelector("[data-description-save]");
  const errorEl = form.querySelector("[data-description-error]");
  const slot = form.querySelector("[data-description-editor-slot]");
  const csrfInput = form.querySelector('input[name="_csrf"]');
  let editor = null;

  function showError(msg) {
    if (!errorEl) return;
    errorEl.textContent = msg || "";
    errorEl.classList.toggle("hidden", !msg);
  }

  function openEditor() {
    showError("");
    renderedEl.classList.add("hidden");
    form.classList.remove("hidden");
    // Hide the Edit button while in edit mode — clicking it during a
    // session would be a no-op (editor already visible) and the clutter
    // competes with the Save/Cancel affordances.
    editBtn.classList.add("hidden");
    if (!editor && window.toastui) {
      // Build the hidden source textarea + mount div inside the slot. We
      // do this at click time (rather than at render time) so the editor
      // only pays its initialization cost when someone actually edits.
      const raw = form.dataset.rawDescription || "";
      slot.innerHTML = "";
      const sourceId = "toastui-source-description-inline";
      const mountId = "toastui-mount-description-inline";
      const source = document.createElement("textarea");
      source.id = sourceId;
      source.className = "hidden";
      source.value = raw;
      slot.appendChild(source);
      // Same wrap+resizer shape as the shared partial so resize behaves
      // the same way (editor fills the resizer; the resizer floors and
      // tracks the drag, enclosing card grows in flow).
      const resizer = document.createElement("div");
      resizer.className = "toastui-editor-resizer";
      resizer.style.height = "420px";
      resizer.style.minHeight = "420px";
      slot.appendChild(resizer);
      const mount = document.createElement("div");
      mount.id = mountId;
      mount.dataset.toastuiMount = "";
      mount.dataset.toastuiSourceId = sourceId;
      mount.className = "h-full";
      resizer.appendChild(mount);
      window.initToastuiEditors?.(slot);
      editor = mount.__toastuiEditor || null;
    }
  }

  function closeEditor() {
    form.classList.add("hidden");
    renderedEl.classList.remove("hidden");
    editBtn.classList.remove("hidden");
    showError("");
  }

  editBtn.addEventListener("click", openEditor);
  cancelBtn?.addEventListener("click", closeEditor);

  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    // Fall back to the existing markdown from the form if the editor
    // failed to boot (toastui script missing, etc.) — we still let the
    // user save whatever they had typed.
    const markdown = editor ? editor.getMarkdown() : (form.dataset.rawDescription || "");
    saveBtn.disabled = true;
    showError("");
    try {
      const resp = await fetch(form.action, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-CSRF-Token": csrfInput?.value || "",
        },
        body: JSON.stringify({ description: markdown }),
      });
      if (resp.ok) {
        const data = await resp.json().catch(() => ({}));
        renderedEl.innerHTML = data.html || "";
        form.dataset.rawDescription = markdown;
        // Any Lucide icons inside newly-rendered content need re-init.
        // Rendered markdown shouldn't ship `<i data-lucide>` today, but the
        // hook is cheap and matches base.html's pattern.
        if (window.lucide && typeof window.lucide.createIcons === "function") {
          window.lucide.createIcons();
        }
        closeEditor();
      } else {
        let detail = "Couldn't save description.";
        try {
          const data = await resp.json();
          if (data && typeof data.detail === "string") detail = data.detail;
        } catch (_) {
          /* keep default */
        }
        showError(detail);
      }
    } catch (_) {
      showError("Network error — please try again.");
    } finally {
      saveBtn.disabled = false;
    }
  });
})();
