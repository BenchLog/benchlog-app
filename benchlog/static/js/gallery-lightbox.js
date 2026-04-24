/*
 * Gallery lightbox.
 *
 * Click a thumbnail (or anywhere with [data-lightbox-trigger]) → open a native
 * <dialog> overlay showing the full-size image. Prev/Next via arrow buttons,
 * keyboard arrows, or single-finger horizontal swipe. Esc closes (handled by
 * <dialog>). Backdrop click also closes.
 *
 * Image data is read from the inline <script id="gallery-lightbox-data"> JSON
 * block emitted by the gallery template — no AJAX, no separate endpoint.
 *
 * Adjacent images are preloaded into hidden <img> tags after each render so
 * navigation is instant once the user starts moving through the set.
 */
(() => {
  "use strict";

  document.addEventListener("DOMContentLoaded", () => {
    const dialog = document.querySelector("dialog.gallery-lightbox");
    const dataEl = document.getElementById("gallery-lightbox-data");
    if (!dialog || !dataEl) return;

    let images = [];
    try {
      images = JSON.parse(dataEl.textContent || "[]");
    } catch (_err) {
      images = [];
    }
    if (!Array.isArray(images) || images.length === 0) return;

    const viewport = dialog.querySelector("[data-lightbox-viewport]");
    const imageEl = dialog.querySelector("[data-lightbox-image]");
    const filenameEl = dialog.querySelector("[data-lightbox-filename]");
    const descriptionEl = dialog.querySelector("[data-lightbox-description]");
    const counterEl = dialog.querySelector("[data-lightbox-counter]");
    const closeBtn = dialog.querySelector("[data-lightbox-close]");
    const prevBtn = dialog.querySelector("[data-lightbox-prev]");
    const nextBtn = dialog.querySelector("[data-lightbox-next]");
    const preloadPrev = dialog.querySelector("[data-lightbox-preload-prev]");
    const preloadNext = dialog.querySelector("[data-lightbox-preload-next]");

    // Owner toolbar elements (may not exist for anonymous viewers).
    const toolbar = dialog.querySelector("[data-lightbox-toolbar]");
    const coverBtn = dialog.querySelector("[data-lightbox-cover-btn]");
    const coverLabel = dialog.querySelector("[data-lightbox-cover-label]");
    const hideBtn = dialog.querySelector("[data-lightbox-hide-btn]");
    const viewBtn = dialog.querySelector("[data-lightbox-view-btn]");
    const errorEl = dialog.querySelector("[data-lightbox-error]");
    const csrfInput = toolbar
      ? toolbar.querySelector('input[name="_csrf"]')
      : null;

    // Per-image mutable state mirrors what the toolbar shows. Seeded from the
    // JSON payload; mutated locally on successful POSTs so the user sees the
    // new state immediately without a page reload. Hidden images are spliced
    // out of the sequence on hide, so visibility is implicit (every image
    // currently in `images[]` is visible) and not tracked here.
    const state = images.map((item) => ({
      is_cover: !!item.is_cover,
    }));

    let index = 0;
    // Did the user mutate cover/visibility from the toolbar? If so, the
    // grid behind the lightbox is stale; reload the page on close so the
    // cover badge moves to the new tile and any newly-hidden images
    // disappear from the visible grid (or move into the hidden section).
    let mutated = false;

    // ---------- render ---------- //

    function render() {
      const item = images[index];
      if (!item) return;
      imageEl.src = item.full_url;
      imageEl.alt = item.filename || "";
      filenameEl.textContent = item.filename || "";
      descriptionEl.textContent = item.description || "";
      descriptionEl.hidden = !item.description;
      counterEl.textContent = `${index + 1} of ${images.length}`;

      const atFirst = index <= 0;
      const atLast = index >= images.length - 1;
      prevBtn.hidden = atFirst;
      nextBtn.hidden = atLast;

      // Preload neighbors so subsequent next/prev clicks display instantly.
      preloadPrev.src = atFirst ? "" : images[index - 1].full_url;
      preloadNext.src = atLast ? "" : images[index + 1].full_url;

      // Reset any in-progress drag offset on the image.
      imageEl.style.transform = "";

      renderToolbar();
    }

    function renderToolbar() {
      if (!toolbar) return;
      const item = images[index];
      const s = state[index];
      if (!item || !s) return;

      // Cover button: label + visual flip when the image is the current cover.
      coverLabel.textContent = s.is_cover ? "Current cover" : "Set as cover";
      coverBtn.dataset.active = s.is_cover ? "true" : "false";
      coverBtn.setAttribute(
        "aria-label",
        s.is_cover ? "Clear project cover" : "Set as project cover",
      );

      // View file: anchor href points to the per-image detail URL.
      viewBtn.setAttribute("href", item.detail_url || "#");

      // Reset any sticky error from the previous image.
      hideError();
    }

    function showError(message) {
      if (!errorEl) return;
      errorEl.textContent = message;
      errorEl.hidden = false;
      // Auto-dismiss after 3s.
      clearTimeout(showError._t);
      showError._t = setTimeout(() => {
        errorEl.hidden = true;
        errorEl.textContent = "";
      }, 3000);
    }

    function hideError() {
      if (!errorEl) return;
      clearTimeout(showError._t);
      errorEl.hidden = true;
      errorEl.textContent = "";
    }

    // ---------- open / close ---------- //

    function openLightbox(startIndex) {
      const next = Number(startIndex);
      index = Number.isFinite(next) ? Math.max(0, Math.min(images.length - 1, next)) : 0;
      render();
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
      // Land focus on the dialog so keyboard arrows fire even when no button
      // has explicit focus. The dialog itself isn't focusable by default, so
      // give it tabindex via attribute.
      dialog.focus();
    }

    function closeLightbox({ skipReload = false } = {}) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
      if (mutated && !skipReload) {
        // Browsers preserve scroll position on reload, so the user lands
        // back where they were in the grid.
        window.location.reload();
      }
    }

    // ---------- navigation ---------- //

    function next() {
      if (index >= images.length - 1) return;
      index += 1;
      render();
    }

    function prev() {
      if (index <= 0) return;
      index -= 1;
      render();
    }

    // ---------- click triggers ---------- //

    document.addEventListener("click", (e) => {
      const trigger = e.target.closest("[data-lightbox-trigger]");
      if (!trigger) return;
      // Don't hijack the visibility-toggle form's submit button — its click
      // bubbles up through the tile wrapper.
      if (e.target.closest(".gallery-tile-action")) return;
      const idx = Number(trigger.dataset.index);
      if (!Number.isFinite(idx)) return;
      e.preventDefault();
      openLightbox(idx);
    });

    // ---------- keyboard ---------- //

    dialog.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") {
        e.preventDefault();
        next();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        prev();
      }
    });

    // ---------- close button + backdrop ---------- //

    closeBtn.addEventListener("click", (e) => {
      e.preventDefault();
      closeLightbox();
    });

    dialog.addEventListener("click", (e) => {
      // A click whose target is the dialog itself (not any child) lands on
      // the backdrop area — close.
      if (e.target === dialog) {
        closeLightbox();
      }
    });

    dialog.addEventListener("cancel", (e) => {
      // <dialog> fires "cancel" on Esc — let the default close behavior run.
      // We just hook this to keep symmetry with the cover-cropper pattern.
      e.preventDefault();
      closeLightbox();
    });

    prevBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      prev();
    });
    nextBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      next();
    });

    // Keep clicks on the image itself from bubbling to the dialog and
    // triggering the backdrop-close path.
    imageEl.addEventListener("click", (e) => {
      e.stopPropagation();
    });

    // ---------- owner actions ---------- //

    function csrfToken() {
      return csrfInput?.value || "";
    }

    async function postOwnerAction(url) {
      const formData = new FormData();
      formData.append("_csrf", csrfToken());
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        body: formData,
      });
      let body = null;
      try {
        body = await resp.json();
      } catch (_) {
        /* leave body null — handled below */
      }
      if (!resp.ok) {
        const detail = body && typeof body.detail === "string"
          ? body.detail
          : "Couldn't save — try again.";
        const err = new Error(detail);
        err.detail = detail;
        throw err;
      }
      return body || {};
    }

    if (coverBtn) {
      coverBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const item = images[index];
        if (!item) return;
        coverBtn.disabled = true;
        try {
          const result = await postOwnerAction(`${item.detail_url}/cover`);
          // Walk all images: only one can be the cover at a time. The server
          // confirms which (the current index, if is_cover==true). Clearing
          // the cover means no image is_cover.
          const newCover = result.is_cover === true;
          for (let i = 0; i < state.length; i += 1) {
            state[i].is_cover = newCover && i === index;
          }
          mutated = true;
          renderToolbar();
        } catch (err) {
          showError(err.detail || "Couldn't save — try again.");
        } finally {
          coverBtn.disabled = false;
        }
      });
    }

    if (hideBtn) {
      hideBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const item = images[index];
        if (!item) return;
        hideBtn.disabled = true;
        try {
          await postOwnerAction(`${item.detail_url}/gallery-visibility`);
          // Drop the now-hidden image from the in-memory sequence and advance
          // to whatever sits at the same index (the next image). If we just
          // hid the last image, fall back to the new last index. If nothing
          // is left, close the lightbox.
          images.splice(index, 1);
          state.splice(index, 1);
          mutated = true;
          if (images.length === 0) {
            closeLightbox();
            return;
          }
          if (index >= images.length) {
            index = images.length - 1;
          }
          render();
        } catch (err) {
          showError(err.detail || "Couldn't save — try again.");
        } finally {
          hideBtn.disabled = false;
        }
      });
    }

    if (viewBtn) {
      viewBtn.addEventListener("click", (e) => {
        // Let modifier-click and middle-click follow the anchor's real href
        // (open-in-new-tab / new-window). Skipping the dialog-close here is
        // cosmetic — the original tab stays on the gallery.
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
          return;
        }
        // Plain click: take over navigation so we can close the dialog first,
        // which keeps the back button returning to the gallery rather than
        // the lightbox-open state.
        e.preventDefault();
        e.stopPropagation();
        const item = images[index];
        const target = item?.detail_url;
        if (!target) {
          // Shouldn't happen — server always seeds detail_url — but surface
          // it inline rather than silently closing to a dead state.
          showError("Couldn't open file — try again.");
          return;
        }
        // Skip the close-handler reload — we're navigating away, so the
        // gallery doesn't need to refresh in place.
        closeLightbox({ skipReload: true });
        window.location.assign(target);
      });
    }

    // ---------- swipe (Pointer Events) ---------- //

    const SWIPE_THRESHOLD = 50;
    const pointers = new Map();
    let dragging = null;

    viewport.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      // Single-pointer only; bail if another finger is already down.
      if (pointers.size > 1) {
        dragging = null;
        return;
      }
      dragging = {
        id: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        committed: false,
      };
      try {
        viewport.setPointerCapture(e.pointerId);
      } catch (_err) {
        /* some browsers throw on synthetic events */
      }
    });

    viewport.addEventListener("pointermove", (e) => {
      if (!dragging || dragging.id !== e.pointerId) return;
      const dx = e.clientX - dragging.startX;
      const dy = e.clientY - dragging.startY;
      // Only commit to a horizontal swipe once horizontal movement clearly
      // dominates — keeps vertical scroll usable on tall lightbox content.
      if (!dragging.committed) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        if (Math.abs(dx) <= Math.abs(dy)) {
          dragging = null;
          return;
        }
        dragging.committed = true;
      }
      // Visual feedback — drag the image with the finger.
      imageEl.style.transform = `translateX(${dx}px)`;
    });

    function endSwipe(e) {
      pointers.delete(e.pointerId);
      if (!dragging || dragging.id !== e.pointerId) return;
      const dx = e.clientX - dragging.startX;
      const dy = e.clientY - dragging.startY;
      const committed = dragging.committed;
      dragging = null;
      try {
        viewport.releasePointerCapture(e.pointerId);
      } catch (_err) {
        /* noop */
      }
      // Snap back regardless — render() resets the transform on nav.
      imageEl.style.transform = "";
      if (!committed) return;
      if (Math.abs(dx) > SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
        if (dx < 0) next();
        else prev();
      }
    }

    viewport.addEventListener("pointerup", endSwipe);
    viewport.addEventListener("pointercancel", endSwipe);
    viewport.addEventListener("lostpointercapture", endSwipe);

    // ---------- public API ---------- //

    window.openGalleryLightbox = openLightbox;
  });
})();
