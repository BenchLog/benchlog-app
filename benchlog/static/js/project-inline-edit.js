/*
 * Inline edit controls for the project detail header.
 *
 * Hooks up (owner-only):
 *   - status chip dropdown
 *   - public chip dropdown (private ↔ public)
 *   - pinned chip dropdown (pin / unpin)
 *   - click-to-edit title
 *   - slug change modal (from "More actions")
 *   - tag and category add modals + per-chip × remove
 *   - related-project × remove confirm prompt
 *
 * Every mutation targets the same partial-update endpoint (`/settings`)
 * except the related-project remove, which continues to hit its existing
 * route.
 */
(() => {
  "use strict";

  const header = document.querySelector("[data-project-header]");
  if (!header) return;
  const isOwner = header.dataset.isOwner === "1";
  if (!isOwner) return;

  const settingsUrl = header.dataset.settingsUrl;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  const STATUS_LABELS = {
    idea: "Idea",
    in_progress: "In progress",
    completed: "Completed",
    archived: "Archived",
  };

  // ---- shared POST helper ----
  async function postSettings(pairs) {
    const body = new URLSearchParams();
    body.set("_csrf", csrf);
    for (const [k, v] of pairs) {
      // URLSearchParams.append keeps multi-value fields (categories[]) intact.
      body.append(k, v);
    }
    const resp = await fetch(settingsUrl, {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrf,
      },
      body: body.toString(),
    });
    return resp;
  }

  async function readError(resp, fallback) {
    try {
      const data = await resp.json();
      if (data && typeof data.detail === "string") return data.detail;
    } catch (_) { /* fallthrough */ }
    return fallback;
  }

  function closeMenu(el) {
    const details = el.closest("details[data-menu]");
    if (details) details.open = false;
  }

  function refreshIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  // ---- status dropdown ----
  const statusMenu = header.querySelector("[data-status-menu]");
  if (statusMenu) {
    const label = statusMenu.querySelector("[data-status-label]");
    statusMenu.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-status-option]");
      if (!btn) return;
      const value = btn.dataset.statusOption;
      const resp = await postSettings([["status", value]]);
      if (resp.ok) {
        if (label) label.textContent = (STATUS_LABELS[value] || value).toLowerCase();
        // Only show the "other" options — hide whatever is now selected
        // and reveal the rest. Same pattern as the public/pinned menus.
        statusMenu.querySelectorAll("[data-status-option]").forEach((opt) => {
          opt.classList.toggle("hidden", opt.dataset.statusOption === value);
        });
        closeMenu(btn);
      }
    });
  }

  // ---- public chip dropdown ----
  const publicMenu = header.querySelector("[data-public-menu]");
  if (publicMenu) {
    const summary = publicMenu.querySelector("summary");
    const label = publicMenu.querySelector("[data-public-label]");
    const icon = publicMenu.querySelector("[data-public-icon]");
    const makePublicBtn = publicMenu.querySelector('[data-public-option="1"]');
    const makePrivateBtn = publicMenu.querySelector('[data-public-option="0"]');
    publicMenu.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-public-option]");
      if (!btn) return;
      const value = btn.dataset.publicOption;
      const resp = await postSettings([["is_public", value]]);
      if (resp.ok) {
        const isPublic = value === "1";
        if (label) label.textContent = isPublic ? "Public" : "Private";
        if (summary) {
          summary.classList.toggle("text-rust-deep", isPublic);
          summary.classList.toggle("hover:text-rust", !isPublic);
          summary.title = isPublic
            ? "Public — anyone with the link can view"
            : "Private — only you can see this project";
        }
        if (icon) {
          icon.setAttribute("data-lucide", isPublic ? "globe" : "lock");
          // Replace the rendered <svg> with a fresh <i> so lucide re-renders.
          const fresh = document.createElement("i");
          fresh.setAttribute("data-lucide", isPublic ? "globe" : "lock");
          fresh.className = "w-3.5 h-3.5";
          fresh.setAttribute("data-public-icon", "");
          icon.replaceWith(fresh);
          refreshIcons();
        }
        // Swap which option the dropdown offers — only the "other" state
        // is ever visible, so the user never sees a no-op choice.
        if (makePublicBtn) makePublicBtn.classList.toggle("hidden", isPublic);
        if (makePrivateBtn) makePrivateBtn.classList.toggle("hidden", !isPublic);
        closeMenu(btn);
      }
    });
  }

  // ---- pinned chip dropdown ----
  const pinnedMenu = header.querySelector("[data-pinned-menu]");
  if (pinnedMenu) {
    const summary = pinnedMenu.querySelector("summary");
    const label = pinnedMenu.querySelector("[data-pinned-label]");
    const icon = pinnedMenu.querySelector("[data-pinned-icon]");
    const pinBtn = pinnedMenu.querySelector('[data-pinned-option="1"]');
    const unpinBtn = pinnedMenu.querySelector('[data-pinned-option="0"]');
    pinnedMenu.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-pinned-option]");
      if (!btn) return;
      const value = btn.dataset.pinnedOption;
      const resp = await postSettings([["pinned", value]]);
      if (resp.ok) {
        const pinned = value === "1";
        if (label) label.textContent = pinned ? "Pinned" : "Pin to top";
        if (summary) {
          summary.classList.toggle("text-rust", pinned);
          summary.classList.toggle("hover:text-rust", !pinned);
          summary.title = pinned
            ? "Pinned to the top of your projects list"
            : "Pin to the top of your projects list";
        }
        if (icon) {
          if (pinned) icon.style.opacity = "";
          else icon.style.opacity = "0.6";
        }
        // Swap which option the dropdown offers — only the "other" state
        // is ever visible so the user never picks a no-op choice.
        if (pinBtn) pinBtn.classList.toggle("hidden", pinned);
        if (unpinBtn) unpinBtn.classList.toggle("hidden", !pinned);
        closeMenu(btn);
      }
    });
  }

  // ---- click-to-edit title ----
  // The input itself carries a dark background (bg-black/85) when on the
  // banner so it reads cleanly without needing a separate full-banner
  // scrim. The "scrim only behind the input" treatment lives entirely in
  // the input's own classes — no JS toggle needed.
  const titleRead = header.querySelector("[data-project-title-read]");
  const titleEdit = header.querySelector("[data-project-title-edit]");
  const titleError = header.querySelector("[data-project-title-error]");
  if (titleRead && titleEdit) {
    const openTitleEdit = () => {
      if (titleError) {
        titleError.textContent = "";
        titleError.classList.add("hidden");
      }
      titleEdit.value = titleRead.textContent.trim();
      titleRead.classList.add("hidden");
      titleEdit.classList.remove("hidden");
      titleEdit.focus();
      titleEdit.select();
    };
    const closeTitleEdit = () => {
      titleEdit.classList.add("hidden");
      titleRead.classList.remove("hidden");
    };
    const saveTitle = async () => {
      const next = titleEdit.value.trim();
      const original = (titleEdit.dataset.originalTitle || "").trim();
      if (!next) {
        // Empty is invalid — revert silently (Esc-like behavior).
        closeTitleEdit();
        titleEdit.value = original;
        return;
      }
      if (next === original) {
        closeTitleEdit();
        return;
      }
      const resp = await postSettings([["title", next]]);
      if (resp.ok) {
        titleRead.textContent = next;
        titleEdit.dataset.originalTitle = next;
        document.title = next + " · BenchLog";
        closeTitleEdit();
      } else {
        const msg = await readError(resp, "Couldn't save the title.");
        if (titleError) {
          titleError.textContent = msg;
          titleError.classList.remove("hidden");
        }
        titleEdit.focus();
      }
    };

    titleRead.addEventListener("click", openTitleEdit);
    titleEdit.addEventListener("blur", () => {
      // Defer blur to allow key events to fire first.
      setTimeout(() => {
        if (!titleEdit.classList.contains("hidden")) saveTitle();
      }, 0);
    });
    titleEdit.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveTitle();
      } else if (e.key === "Escape") {
        e.preventDefault();
        titleEdit.value = titleEdit.dataset.originalTitle || "";
        if (titleError) {
          titleError.textContent = "";
          titleError.classList.add("hidden");
        }
        closeTitleEdit();
      }
    });
  }

  // ---- click-to-edit short description ----
  // Mirrors the title editor: click read view → swap to textarea, save on
  // blur/Enter, cancel on Esc. Empty value clears the field on the server
  // and reverts the read view to the placeholder prompt.
  const shortDescWrap = header.querySelector("[data-project-short-desc]");
  const shortDescRead = header.querySelector("[data-project-short-desc-read]");
  const shortDescEdit = header.querySelector("[data-project-short-desc-edit]");
  const shortDescError = header.querySelector("[data-project-short-desc-error]");
  const SHORT_DESC_PLACEHOLDER = "Add a one-line summary for cards…";
  if (shortDescWrap && shortDescRead && shortDescEdit) {
    const setReadDisplay = (value) => {
      if (value) {
        shortDescRead.textContent = value;
        shortDescRead.classList.remove("opacity-70");
      } else {
        shortDescRead.textContent = SHORT_DESC_PLACEHOLDER;
        shortDescRead.classList.add("opacity-70");
      }
    };
    const showShortDescError = (msg) => {
      if (!shortDescError) return;
      shortDescError.textContent = msg || "";
      shortDescError.classList.toggle("hidden", !msg);
    };
    const openShortDescEdit = () => {
      showShortDescError("");
      shortDescEdit.value = shortDescEdit.dataset.originalShortDesc || "";
      shortDescRead.classList.add("hidden");
      shortDescEdit.classList.remove("hidden");
      shortDescEdit.focus();
      shortDescEdit.select();
    };
    const closeShortDescEdit = () => {
      shortDescEdit.classList.add("hidden");
      shortDescRead.classList.remove("hidden");
    };
    const saveShortDesc = async () => {
      // Collapse internal whitespace so a paste with newlines becomes a
      // single line — matches the server's _clean_short_description.
      const next = (shortDescEdit.value || "").replace(/\s+/g, " ").trim();
      const original = shortDescEdit.dataset.originalShortDesc || "";
      if (next === original) {
        closeShortDescEdit();
        return;
      }
      const resp = await postSettings([["short_description", next]]);
      if (resp.ok) {
        shortDescEdit.dataset.originalShortDesc = next;
        shortDescEdit.value = next;
        setReadDisplay(next);
        closeShortDescEdit();
      } else {
        const msg = await readError(resp, "Couldn't save the summary.");
        showShortDescError(msg);
        shortDescEdit.focus();
      }
    };

    shortDescRead.addEventListener("click", openShortDescEdit);
    shortDescEdit.addEventListener("blur", () => {
      setTimeout(() => {
        if (!shortDescEdit.classList.contains("hidden")) saveShortDesc();
      }, 0);
    });
    shortDescEdit.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        // Plain Enter saves; Shift+Enter inserts a newline (folded back to
        // a space on save). Single line is the intent on cards.
        e.preventDefault();
        saveShortDesc();
      } else if (e.key === "Escape") {
        e.preventDefault();
        shortDescEdit.value = shortDescEdit.dataset.originalShortDesc || "";
        showShortDescError("");
        closeShortDescEdit();
      }
    });
  }

  // ---- slug modal ----
  const slugModal = document.querySelector("[data-slug-modal]");
  const slugModalOpen = document.querySelector("[data-slug-modal-open]");
  if (slugModal && slugModalOpen) {
    const input = slugModal.querySelector("[data-slug-modal-input]");
    const warning = slugModal.querySelector("[data-slug-modal-warning]");
    const error = slugModal.querySelector("[data-slug-modal-error]");
    const saveBtn = slugModal.querySelector("[data-slug-modal-save]");
    const cancelBtn = slugModal.querySelector("[data-slug-modal-cancel]");
    const showError = (msg) => {
      if (!error) return;
      error.textContent = msg || "";
      error.classList.toggle("hidden", !msg);
    };
    const updateWarning = () => {
      if (!warning) return;
      const orig = (input.dataset.originalSlug || "").toLowerCase();
      warning.hidden = input.value.trim().toLowerCase() === orig;
    };
    const openModal = () => {
      showError("");
      input.value = input.dataset.originalSlug || "";
      updateWarning();
      if (typeof slugModal.showModal === "function") slugModal.showModal();
      else slugModal.setAttribute("open", "");
      requestAnimationFrame(() => input.focus());
    };
    slugModalOpen.addEventListener("click", () => {
      // Close the parent details menu first.
      closeMenu(slugModalOpen);
      openModal();
    });
    input?.addEventListener("input", updateWarning);
    cancelBtn?.addEventListener("click", () => slugModal.close());
    saveBtn?.addEventListener("click", async () => {
      const next = input.value.trim();
      if (!next) {
        showError("Slug is required.");
        return;
      }
      saveBtn.disabled = true;
      showError("");
      try {
        const resp = await postSettings([["slug", next]]);
        if (resp.status === 204) {
          slugModal.close();
          return;
        }
        if (resp.ok) {
          const data = await resp.json().catch(() => ({}));
          if (data && data.redirect) {
            window.location.href = data.redirect;
            return;
          }
          slugModal.close();
        } else {
          const msg = await readError(resp, "Couldn't save the slug.");
          showError(msg);
        }
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  // Shared helper for the auto-save modals (tags + categories). The
  // combobox dispatches a custom event after each add/remove; we POST
  // the latest selection to /settings on every event. A short-lived
  // status pill ("Saved" / "Saving…" / "Couldn't save") gives the user
  // feedback without a Save button.
  function wireAutoSaveModal({
    modal,
    openBtn,
    statusEl,
    errorEl,
    eventName,
    buildPairs,
    onSaved,
  }) {
    if (!modal || !openBtn) return;
    let pendingTimer = null;
    let inflight = false;
    let queued = false;

    const showStatus = (text, mode) => {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.classList.remove("hidden", "text-rust", "text-rust-deep");
      if (mode === "error") statusEl.classList.add("text-rust-deep");
      if (text === "") statusEl.classList.add("hidden");
    };
    const showError = (msg) => {
      if (!errorEl) return;
      errorEl.textContent = msg || "";
      errorEl.classList.toggle("hidden", !msg);
    };

    openBtn.addEventListener("click", () => {
      showError("");
      showStatus("");
      if (typeof modal.showModal === "function") modal.showModal();
      else modal.setAttribute("open", "");
    });

    const flush = async () => {
      if (inflight) {
        queued = true;
        return;
      }
      inflight = true;
      queued = false;
      showStatus("Saving…");
      try {
        const pairs = buildPairs();
        const resp = await postSettings(pairs);
        if (resp.ok) {
          showStatus("Saved");
          // Auto-clear after a moment so the modal stays quiet at rest.
          setTimeout(() => showStatus(""), 1200);
          showError("");
          if (onSaved) {
            try { onSaved(); } catch (_) { /* swallow */ }
          }
        } else {
          const msg = await readError(resp, "Couldn't save.");
          showError(msg);
          showStatus("Couldn't save", "error");
        }
      } catch (_) {
        showError("Network error — please try again.");
        showStatus("Couldn't save", "error");
      } finally {
        inflight = false;
        if (queued) flush();
      }
    };

    // Debounce a touch so a fast pill toggle doesn't fire two POSTs.
    modal.addEventListener(eventName, () => {
      clearTimeout(pendingTimer);
      pendingTimer = setTimeout(flush, 150);
    });
  }

  function rebuildTagChipsRow(hiddenInput) {
    const container = document.querySelector(
      "[data-project-tags-row] .project-meta-row-chips"
    );
    if (!container) return;
    const slugs = (hiddenInput?.value || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    container.innerHTML = "";
    slugs.forEach((slug) => {
      const a = document.createElement("a");
      a.href = "/explore?tag=" + encodeURIComponent(slug);
      a.className =
        "inline-flex items-center rounded-full border border-edge bg-rust/10 text-rust-deep px-2 py-0.5 text-xs font-mono tracking-wide no-underline hover:border-rust";
      a.textContent = "#" + slug;
      container.appendChild(a);
    });
  }

  function rebuildCategoryChipsRow(catsModal, hiddenWrap) {
    const container = document.querySelector(
      "[data-project-categories-row] .project-meta-row-chips"
    );
    if (!container) return;
    const ids = [
      ...(hiddenWrap?.querySelectorAll("input[type=hidden]") || []),
    ]
      .map((i) => i.value)
      .filter(Boolean);
    // Pull the combobox config blob to map id → breadcrumb_parts. This
    // mirrors how the dropdown renders categories, so the rebuilt chip
    // row stays visually identical to the server-rendered version.
    const cfgEl = catsModal.querySelector(
      "[data-category-input] [data-combobox-config]"
    );
    let options = [];
    if (cfgEl) {
      try { options = JSON.parse(cfgEl.textContent).options || []; }
      catch (_) { options = []; }
    }
    const byId = new Map(options.map((o) => [String(o.value), o]));
    container.innerHTML = "";
    ids.forEach((id) => {
      const opt = byId.get(String(id));
      const parts = (opt && opt.parts) || [opt?.label || "Unknown"];
      const a = document.createElement("a");
      a.href = "/explore?category=" + encodeURIComponent(id);
      a.title = (opt && opt.label) || parts.join(" › ");
      a.className =
        "inline-flex items-center gap-1 rounded-full border border-edge text-ink-muted px-2 py-0.5 text-xs tracking-wide no-underline hover:border-ink hover:text-ink";
      parts.forEach((part, idx) => {
        if (idx > 0) {
          const sep = document.createElement("i");
          sep.setAttribute("data-lucide", "chevron-right");
          sep.setAttribute("aria-hidden", "true");
          sep.className = "w-3 h-3 opacity-70";
          a.appendChild(sep);
        }
        const seg = document.createElement("span");
        seg.textContent = part;
        a.appendChild(seg);
      });
      container.appendChild(a);
    });
    // Rebuild fires once per save (not per keystroke), so asking lucide
    // to swap the <i data-lucide> placeholders is cheap.
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  // ---- tag manage modal ----
  const tagsModal = document.querySelector("[data-tags-modal]");
  const tagsOpenBtn = header.querySelector("[data-project-tags-open]");
  if (tagsModal && tagsOpenBtn) {
    // Shared combobox partial writes the CSV hidden input. (Kinded attr
    // `data-tag-input` still scopes the lookup to this modal.)
    const hidden = tagsModal.querySelector(
      "[data-tag-input] [data-combobox-hidden]"
    );
    wireAutoSaveModal({
      modal: tagsModal,
      openBtn: tagsOpenBtn,
      statusEl: tagsModal.querySelector("[data-tags-modal-status]"),
      errorEl: tagsModal.querySelector("[data-tags-modal-error]"),
      eventName: "tag-input-change",
      buildPairs: () => [["tags", hidden?.value || ""]],
      onSaved: () => rebuildTagChipsRow(hidden),
    });
  }

  // ---- category manage modal ----
  const catsModal = document.querySelector("[data-categories-modal]");
  const catsOpenBtn = header.querySelector("[data-project-categories-open]");
  if (catsModal && catsOpenBtn) {
    const hiddenWrap = catsModal.querySelector(
      "[data-category-input] [data-combobox-hidden-multi]"
    );
    wireAutoSaveModal({
      modal: catsModal,
      openBtn: catsOpenBtn,
      statusEl: catsModal.querySelector("[data-categories-modal-status]"),
      errorEl: catsModal.querySelector("[data-categories-modal-error]"),
      eventName: "category-input-change",
      onSaved: () => rebuildCategoryChipsRow(catsModal, hiddenWrap),
      buildPairs: () => {
        const values = [
          ...(hiddenWrap?.querySelectorAll("input[type=hidden]") || []),
        ]
          .map((i) => i.value)
          .filter(Boolean);
        // Always send at least the field name so the server recognizes
        // the key and can clear the set if the user removed every pick.
        return values.length
          ? values.map((v) => ["categories", v])
          : [["categories", ""]];
      },
    });
  }
})();
