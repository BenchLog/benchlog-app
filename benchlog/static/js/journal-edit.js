/*
 * Inline journal UX — keeps the user on the list/detail view for every
 * owner action (create, edit, pin, visibility, delete). Mirrors the
 * pattern used by description-edit.js and project-inline-edit.js.
 *
 * Surfaces:
 *   - `[data-journal-section]` is present on both the journal tab and
 *     the entry detail page. Typeahead indexes (files + journal entries)
 *     ride along as JSON on that element for reuse in every editor the
 *     page spawns.
 *   - Each entry is a `[data-journal-entry]` article. The feed tab has
 *     many; the detail page has one. Wiring is idempotent and delegates
 *     off the article's data attributes, so swapping the article in
 *     after an edit just re-wires the new one.
 *   - The "New entry" modal on the feed tab is a native <dialog>; lazy
 *     toastui mount on first open, same as description-edit.
 *
 * All mutations hit the existing journal routes with Accept: JSON +
 * Content-Type: JSON. CSRF comes from the meta tag via X-CSRF-Token.
 */
(() => {
  "use strict";

  const section = document.querySelector("[data-journal-section]");
  if (!section) return;

  const csrf =
    document.querySelector('meta[name="csrf-token"]')?.content || "";
  const fileIndex = section.dataset.fileIndex || "";
  const entryIndex = section.dataset.entryIndex || "";
  const uploadUrl = section.dataset.uploadUrl || "";

  function refreshIcons(root) {
    if (!window.lucide || typeof window.lucide.createIcons !== "function") {
      return;
    }
    // Lucide v0.x-style API: createIcons() with no args reprocesses the
    // whole document; some builds accept {attrs, root} — we pass none to
    // stay compatible and re-run globally. Cheap enough for these actions.
    window.lucide.createIcons();
  }

  async function postJSON(url, body) {
    const init = {
      method: "POST",
      headers: {
        Accept: "application/json",
        "X-CSRF-Token": csrf,
      },
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(url, init);
  }

  async function readError(resp, fallback) {
    try {
      const data = await resp.json();
      if (data && typeof data.detail === "string") return data.detail;
    } catch (_) {
      /* fall through */
    }
    return fallback;
  }

  // ---- toastui mount helper (shared between inline edit + new modal) ----
  let editorCounter = 0;
  function mountEditor(slot, rawMarkdown) {
    if (!slot || !window.toastui) return null;
    slot.innerHTML = "";
    const uid = ++editorCounter;
    const sourceId = `toastui-source-journal-${uid}`;
    const mountId = `toastui-mount-journal-${uid}`;
    const source = document.createElement("textarea");
    source.id = sourceId;
    source.className = "hidden";
    source.value = rawMarkdown || "";
    slot.appendChild(source);
    // Same resizer wrap shape the shared partial uses — keeps the drag
    // behaviour identical across every place we mount an editor.
    const resizer = document.createElement("div");
    resizer.className = "toastui-editor-resizer";
    resizer.style.height = "320px";
    resizer.style.minHeight = "320px";
    slot.appendChild(resizer);
    const mount = document.createElement("div");
    mount.id = mountId;
    mount.dataset.toastuiMount = "";
    mount.dataset.toastuiSourceId = sourceId;
    if (fileIndex) mount.dataset.toastuiFileIndex = fileIndex;
    if (entryIndex) mount.dataset.toastuiEntryIndex = entryIndex;
    if (uploadUrl) mount.dataset.toastuiUploadUrl = uploadUrl;
    mount.className = "h-full";
    resizer.appendChild(mount);
    window.initToastuiEditors?.(slot);
    return mount.__toastuiEditor || null;
  }

  // ---- per-entry wiring (inline edit, chips, pin, delete) ----
  function wireEntry(article) {
    if (!article || article.dataset.journalWired === "1") return;
    article.dataset.journalWired = "1";
    wireInlineEdit(article);
    wireVisibilityChip(article);
    wirePinToggle(article);
    wireDelete(article);
  }

  function wireInlineEdit(article) {
    const editBtn = article.querySelector("[data-entry-edit]");
    const form = article.querySelector("[data-entry-edit-form]");
    const renderedEl = article.querySelector("[data-entry-rendered]");
    const renderedHeader = article.querySelector("[data-entry-rendered-header]");
    if (!editBtn || !form || !renderedEl) return; // non-owner article

    const titleInput = form.querySelector("[data-entry-title]");
    const slugInput = form.querySelector("[data-entry-slug]");
    const slugWrap = form.querySelector("[data-entry-slug-wrap]");
    const saveBtn = form.querySelector("[data-entry-save]");
    const cancelBtn = form.querySelector("[data-entry-cancel]");
    const errorEl = form.querySelector("[data-entry-error]");
    const slot = form.querySelector("[data-entry-editor-slot]");
    let editor = null;

    function showError(msg) {
      if (!errorEl) return;
      errorEl.textContent = msg || "";
      errorEl.classList.toggle("hidden", !msg);
    }

    function open() {
      showError("");
      renderedEl.classList.add("hidden");
      form.classList.remove("hidden");
      // Hide the whole rendered header (includes chips + action buttons)
      // while in edit mode — those controls don't apply mid-edit, and
      // leaving them visible is visually noisy.
      if (renderedHeader) renderedHeader.classList.add("hidden");
      if (!editor) {
        editor = mountEditor(slot, form.dataset.rawContent || "");
      }
      // Slug field should only appear once the title is non-empty — mirror
      // the server-side form behaviour where the slug is sticky on titled
      // entries and irrelevant on untitled ones.
      syncSlugVisibility();
      // Focus the title field on first open so keyboard users can type
      // right away; moving to the editor is one Tab away.
      titleInput?.focus();
    }

    function close() {
      form.classList.add("hidden");
      renderedEl.classList.remove("hidden");
      if (renderedHeader) renderedHeader.classList.remove("hidden");
      showError("");
    }

    function syncSlugVisibility() {
      if (!slugWrap) return;
      const hasTitle = !!titleInput?.value.trim();
      const hadTitle = article.dataset.entryHasTitle === "1";
      // Show the slug field + label + warning only when editing a
      // currently-titled entry AND the user is keeping the title. Going
      // from titled → untitled wipes the slug server-side so the field
      // shouldn't suggest it'll be kept; going from untitled → titled
      // lets the server auto-slug.
      slugWrap.hidden = !(hasTitle && hadTitle);
    }

    editBtn.addEventListener("click", open);
    cancelBtn?.addEventListener("click", close);
    titleInput?.addEventListener("input", syncSlugVisibility);

    saveBtn?.addEventListener("click", async () => {
      const content = editor
        ? editor.getMarkdown()
        : form.dataset.rawContent || "";
      const body = {
        title: titleInput?.value || "",
        slug: slugInput?.value || "",
        content,
      };
      saveBtn.disabled = true;
      showError("");
      try {
        const resp = await postJSON(form.dataset.actionUrl, body);
        if (resp.ok) {
          const data = await resp.json().catch(() => ({}));
          if (data.html) {
            // Server returned the full re-rendered <article> — swap the
            // whole element so all data-* stay in lockstep with the new
            // state (slug changes move the base URL, edited timestamps
            // appear, etc.).
            const wrapper = document.createElement("div");
            wrapper.innerHTML = data.html.trim();
            const fresh = wrapper.firstElementChild;
            if (fresh) {
              article.replaceWith(fresh);
              wireEntry(fresh);
              refreshIcons(fresh);
              return;
            }
          }
          close();
        } else {
          showError(await readError(resp, "Couldn't save the entry."));
        }
      } catch (_) {
        showError("Network error — please try again.");
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  function wireVisibilityChip(article) {
    const menu = article.querySelector("[data-entry-visibility-menu]");
    if (!menu) return;
    const summary = menu.querySelector("summary");
    const label = menu.querySelector("[data-entry-visibility-label]");
    // `-slot` is a stable wrapper that lucide's createIcons doesn't touch;
    // we rebuild the inner <i> on toggle. Earlier versions captured the
    // <i> directly but that ref goes stale once lucide swaps it for <svg>
    // at DOMContentLoaded, so the icon would never visually update.
    const iconSlot = menu.querySelector("[data-entry-visibility-icon-slot]");
    const makePublicBtn = menu.querySelector('[data-entry-visibility-option="1"]');
    const makePrivateBtn = menu.querySelector('[data-entry-visibility-option="0"]');

    menu.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-entry-visibility-option]");
      if (!btn) return;
      const makePublic = btn.dataset.entryVisibilityOption === "1";
      const base = article.dataset.entryBaseUrl;
      const resp = await postJSON(`${base}/visibility`, {
        is_public: makePublic,
      });
      if (!resp.ok) return;
      if (label) label.textContent = makePublic ? "Public" : "Private";
      if (summary) {
        summary.classList.toggle("text-rust-deep", makePublic);
        summary.classList.toggle("hover:text-rust", !makePublic);
        summary.title = makePublic
          ? "Public — visible to anyone who can see this project"
          : "Private — only you can see this entry";
      }
      if (iconSlot) {
        iconSlot.innerHTML = `<i data-lucide="${makePublic ? "globe" : "lock"}" class="w-3.5 h-3.5"></i>`;
        refreshIcons();
      }
      // Show only the "other" option so the user never picks a no-op.
      if (makePublicBtn) makePublicBtn.classList.toggle("hidden", makePublic);
      if (makePrivateBtn) makePrivateBtn.classList.toggle("hidden", !makePublic);
      // Close the <details> wrapper.
      menu.open = false;
    });
  }

  function wirePinToggle(article) {
    const btn = article.querySelector("[data-entry-pin-toggle]");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      // Infer current state from the lucide icon name — more reliable than
      // a data-attr that lucide's <i>→<svg> swap strips.
      const currentIcon = btn
        .querySelector("[data-lucide]")
        ?.getAttribute("data-lucide");
      const isPinned = currentIcon === "pin-off";
      const base = article.dataset.entryBaseUrl;
      const url = `${base}/${isPinned ? "unpin" : "pin"}`;
      btn.disabled = true;
      try {
        const resp = await postJSON(url);
        if (!resp.ok) return;
        // Server returns the re-rendered article so the title-bar pin
        // glyph (rust-tinted pin beside the title, or the "Pinned"
        // banner on untitled entries) flips in lockstep with the
        // button. Cheaper than rebuilding those per-shape in JS.
        const data = await resp.json().catch(() => ({}));
        if (data.html) {
          const wrapper = document.createElement("div");
          wrapper.innerHTML = data.html.trim();
          const fresh = wrapper.firstElementChild;
          if (fresh) {
            article.replaceWith(fresh);
            wireEntry(fresh);
            refreshIcons();
          }
        }
      } finally {
        btn.disabled = false;
      }
    });
  }

  function wireDelete(article) {
    const btn = article.querySelector("[data-entry-delete]");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const ok = await (window.benchlogConfirm
        ? window.benchlogConfirm({
            title: "Delete entry?",
            body: "This journal entry will be permanently removed.",
            ok: "Delete entry",
          })
        : Promise.resolve(window.confirm("Delete this entry?")));
      if (!ok) return;
      const base = article.dataset.entryBaseUrl;
      const resp = await postJSON(`${base}/delete`);
      if (!resp.ok && resp.status !== 204) return;
      const isDetailView = article.hasAttribute("data-entry-detail-view");
      if (isDetailView) {
        // On the detail page there's nowhere useful to land — kick back
        // to the project's journal tab where the rest of the entries are.
        const listUrl = article.dataset.entryBaseUrl.replace(
          /\/journal\/[^/]+$/,
          "/journal"
        );
        window.location.href = listUrl;
        return;
      }
      const li = article.closest("li");
      (li || article).remove();
      maybeShowEmptyState();
    });
  }

  // ---- feed-level helpers ----

  function feedEl() {
    return document.querySelector("[data-journal-feed]");
  }

  function maybeShowEmptyState() {
    const feed = feedEl();
    if (!feed) return;
    if (feed.querySelector("[data-journal-entry]")) return;
    const existing = document.querySelector("[data-journal-empty]");
    if (existing) return;
    // We don't know at runtime whether the viewer is the owner because
    // the empty state is only meaningful when the owner just deleted
    // their last entry (non-owners can't delete). Mirror the server's
    // owner copy so the message is consistent.
    const card = document.createElement("div");
    card.className = "card text-center py-10";
    card.dataset.journalEmpty = "";
    card.innerHTML =
      '<p class="text-ink-muted mb-4">No entries yet. Post the first one as you make progress.</p>' +
      '<button type="button" class="btn-primary text-sm" data-journal-new>New entry</button>';
    feed.parentNode.insertBefore(card, feed.nextSibling);
    card
      .querySelector("[data-journal-new]")
      ?.addEventListener("click", openNewEntryModal);
  }

  // ---- new-entry modal ----

  const newModal = document.querySelector("[data-journal-new-modal]");
  const createUrl = section.dataset.createUrl || "";
  let newEditor = null;

  function resetNewModal() {
    if (!newModal) return;
    const titleInput = newModal.querySelector("[data-journal-new-title]");
    const publicInput = newModal.querySelector("[data-journal-new-public]");
    const errorEl = newModal.querySelector("[data-journal-new-error]");
    if (titleInput) titleInput.value = "";
    if (publicInput) publicInput.checked = false;
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.classList.add("hidden");
    }
    if (newEditor && typeof newEditor.setMarkdown === "function") {
      newEditor.setMarkdown("");
    }
  }

  function openNewEntryModal() {
    if (!newModal) return;
    resetNewModal();
    if (!newEditor) {
      const slot = newModal.querySelector("[data-journal-new-editor-slot]");
      newEditor = mountEditor(slot, "");
    }
    if (typeof newModal.showModal === "function") newModal.showModal();
    else newModal.setAttribute("open", "");
    requestAnimationFrame(() => {
      newModal.querySelector("[data-journal-new-title]")?.focus();
    });
  }

  function closeNewEntryModal() {
    if (!newModal) return;
    if (typeof newModal.close === "function") newModal.close();
    else newModal.removeAttribute("open");
  }

  async function submitNewEntry() {
    if (!newModal) return;
    const titleInput = newModal.querySelector("[data-journal-new-title]");
    const publicInput = newModal.querySelector("[data-journal-new-public]");
    const errorEl = newModal.querySelector("[data-journal-new-error]");
    const submitBtn = newModal.querySelector("[data-journal-new-submit]");
    const content = newEditor ? newEditor.getMarkdown() : "";
    const body = {
      title: titleInput?.value || "",
      content,
      is_public: !!publicInput?.checked,
    };
    submitBtn.disabled = true;
    errorEl?.classList.add("hidden");
    try {
      const resp = await postJSON(createUrl, body);
      if (!resp.ok) {
        const msg = await readError(resp, "Couldn't post the entry.");
        if (errorEl) {
          errorEl.textContent = msg;
          errorEl.classList.remove("hidden");
        }
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!data.html) {
        closeNewEntryModal();
        return;
      }
      // Strip the empty-state card (if present) and prepend the new entry.
      document.querySelector("[data-journal-empty]")?.remove();
      const feed = feedEl();
      if (feed) {
        const li = document.createElement("li");
        li.innerHTML = data.html.trim();
        feed.prepend(li);
        const fresh = li.querySelector("[data-journal-entry]");
        if (fresh) {
          wireEntry(fresh);
          refreshIcons(fresh);
        }
      }
      closeNewEntryModal();
    } catch (_) {
      if (errorEl) {
        errorEl.textContent = "Network error — please try again.";
        errorEl.classList.remove("hidden");
      }
    } finally {
      submitBtn.disabled = false;
    }
  }

  // Wire the "New entry" button(s) on both the header and the empty card.
  document
    .querySelectorAll("[data-journal-new]")
    .forEach((btn) => btn.addEventListener("click", openNewEntryModal));
  newModal
    ?.querySelector("[data-journal-new-close]")
    ?.addEventListener("click", closeNewEntryModal);
  newModal
    ?.querySelector("[data-journal-new-cancel]")
    ?.addEventListener("click", closeNewEntryModal);
  newModal
    ?.querySelector("[data-journal-new-submit]")
    ?.addEventListener("click", submitNewEntry);

  // Initial wire-up for every server-rendered entry on the page.
  document.querySelectorAll("[data-journal-entry]").forEach(wireEntry);
})();
