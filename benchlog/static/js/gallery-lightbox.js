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

    let index = 0;

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

    function closeLightbox() {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
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
