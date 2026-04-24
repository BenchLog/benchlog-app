/*
 * Cover-image crop picker.
 *
 * Fixed 16:9 viewport (the crop frame) over a pan/zoomable source image.
 * State is an affine transform on the image: `offsetX`, `offsetY`, `scale`
 * (image-pixel units, transform-origin 0 0). The viewport is stationary.
 * Saved coordinates are normalized to [0, 1] against the image's natural
 * dimensions so they're resolution-independent and can render via pure
 * CSS (see project_card.html).
 *
 * Interaction:
 *   - Drag: pointerdown/move/up with setPointerCapture. Single-pointer only.
 *   - Wheel zoom on desktop: zooms toward the cursor (the point under
 *     the cursor stays under the cursor).
 *   - Pinch zoom on touch: two simultaneous pointers, ratio of current
 *     distance to starting distance drives the scale multiplier, anchored
 *     on the pinch midpoint.
 *   - Zoom slider: maps 0..1 linearly to [minScale, maxScale] and anchors
 *     on viewport center.
 *   - Esc cancels, Enter saves (handled by <dialog> + submit).
 *
 * Saved coords satisfy (with W = naturalWidth, H = naturalHeight):
 *   crop_width  = viewportW / (W * scale)
 *   crop_height = viewportH / (H * scale)
 *   crop_x      = -offsetX / (W * scale)
 *   crop_y      = -offsetY / (H * scale)
 *
 * Loading an existing crop is the inverse — derive scale from crop_width
 * alone (viewport width is fixed, and 16:9 math makes crop_height redundant).
 */
(() => {
  "use strict";

  const modal = document.querySelector("[data-cover-crop-modal]");
  if (!modal) return;

  const stage = modal.querySelector("[data-cover-crop-stage]");
  const img = modal.querySelector("[data-cover-crop-image]");
  const form = modal.querySelector("[data-cover-crop-form]");
  const zoomInput = modal.querySelector("[data-cover-crop-zoom]");
  const resetBtn = modal.querySelector("[data-cover-crop-reset]");
  const saveBtn = modal.querySelector("[data-cover-crop-save]");
  const errorEl = modal.querySelector("[data-cover-crop-error]");
  const csrfEl = modal.querySelector("[data-cover-crop-csrf]");
  const cancelEls = modal.querySelectorAll("[data-cover-crop-cancel]");

  const cropUrl = modal.dataset.coverCropUrl;
  const imageUrl = modal.dataset.coverCropImageUrl;

  // Pan/zoom state. `scale` is natural-px → stage-px; offsetX/Y are the
  // top-left corner of the scaled image in stage coordinates.
  const state = {
    scale: 1,
    offsetX: 0,
    offsetY: 0,
    minScale: 1,
    maxScale: 10,
    naturalWidth: 0,
    naturalHeight: 0,
    viewportWidth: 0,
    viewportHeight: 0,
  };

  // Tracked active pointers keyed by pointerId. Each entry records where
  // the pointer currently is and (for single-finger drag) the offsets at
  // the time the pointer went down, so drag math is straightforward:
  // `newOffset = startOffset + (currentPt - downPt)`.
  const activePointers = new Map();

  // Pinch gesture baseline: starting finger distance, starting scale, and
  // the stage-px midpoint at gesture start.
  let pinchBaseline = null;

  // --------- math helpers --------- //

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function clampOffsets(next) {
    // The scaled image must fully cover the viewport — no letterboxing.
    // Equivalently: offset must be in [viewportSize - imageSize, 0].
    const scaledW = state.naturalWidth * state.scale;
    const scaledH = state.naturalHeight * state.scale;
    next.offsetX = clamp(next.offsetX, state.viewportWidth - scaledW, 0);
    next.offsetY = clamp(next.offsetY, state.viewportHeight - scaledH, 0);
    return next;
  }

  function applyTransform() {
    img.style.transform =
      `translate3d(${state.offsetX}px, ${state.offsetY}px, 0) scale(${state.scale})`;
  }

  function measureViewport() {
    const rect = stage.getBoundingClientRect();
    state.viewportWidth = rect.width;
    state.viewportHeight = rect.height;
  }

  function computeMinScale() {
    // Smallest scale at which the image still fully covers the viewport.
    // Works for narrower-than-16:9 images (bound by width) and wider ones
    // (bound by height) alike.
    const wFit = state.viewportWidth / state.naturalWidth;
    const hFit = state.viewportHeight / state.naturalHeight;
    return Math.max(wFit, hFit);
  }

  function centerImage() {
    state.offsetX = (state.viewportWidth - state.naturalWidth * state.scale) / 2;
    state.offsetY = (state.viewportHeight - state.naturalHeight * state.scale) / 2;
  }

  function syncZoomSlider() {
    if (state.maxScale <= state.minScale) {
      zoomInput.value = "0";
      return;
    }
    const t = (state.scale - state.minScale) / (state.maxScale - state.minScale);
    zoomInput.value = String(clamp(t, 0, 1));
  }

  function setScale(nextScale, anchor) {
    // Zoom while keeping `anchor` (a stage-px point) fixed on the image.
    const targetScale = clamp(nextScale, state.minScale, state.maxScale);
    if (targetScale === state.scale) return;
    const ax = anchor?.x ?? state.viewportWidth / 2;
    const ay = anchor?.y ?? state.viewportHeight / 2;
    // Image-point currently at anchor: (ax - offsetX) / scale. Keep it at
    // the same stage position under the new scale.
    const imageX = (ax - state.offsetX) / state.scale;
    const imageY = (ay - state.offsetY) / state.scale;
    state.scale = targetScale;
    state.offsetX = ax - imageX * state.scale;
    state.offsetY = ay - imageY * state.scale;
    clampOffsets(state);
    applyTransform();
    syncZoomSlider();
  }

  // --------- initial state --------- //

  function loadExistingCrop() {
    const cw = parseFloat(modal.dataset.coverCropWidth);
    const cx = parseFloat(modal.dataset.coverCropX);
    const cy = parseFloat(modal.dataset.coverCropY);
    if (!Number.isFinite(cw) || !Number.isFinite(cx) || !Number.isFinite(cy)) {
      return false;
    }
    // Invert: viewportW / (naturalW * scale) = cw  →  scale = viewportW / (naturalW * cw)
    const scale = state.viewportWidth / (state.naturalWidth * cw);
    state.scale = clamp(scale, state.minScale, state.maxScale);
    state.offsetX = -cx * state.naturalWidth * state.scale;
    state.offsetY = -cy * state.naturalHeight * state.scale;
    clampOffsets(state);
    return true;
  }

  function resetToFit() {
    state.scale = state.minScale;
    centerImage();
    clampOffsets(state);
    applyTransform();
    syncZoomSlider();
  }

  function initAfterImageLoad() {
    state.naturalWidth = img.naturalWidth;
    state.naturalHeight = img.naturalHeight;
    measureViewport();
    if (!state.naturalWidth || !state.naturalHeight || !state.viewportWidth) {
      return;
    }
    state.minScale = computeMinScale();
    // Allow zooming up to 4× min-cover so tiny images don't land in a
    // single-pixel grid while still giving room to frame detail shots.
    state.maxScale = Math.max(state.minScale * 4, state.minScale);

    if (!loadExistingCrop()) {
      state.scale = state.minScale;
      centerImage();
      clampOffsets(state);
    }
    applyTransform();
    syncZoomSlider();
  }

  // --------- pointer handling --------- //

  function stagePoint(evt) {
    const rect = stage.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  function pointerDistance(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function onPointerDown(e) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const pt = stagePoint(e);
    activePointers.set(e.pointerId, {
      x: pt.x,
      y: pt.y,
      downX: pt.x,
      downY: pt.y,
      startOffsetX: state.offsetX,
      startOffsetY: state.offsetY,
    });
    stage.setPointerCapture?.(e.pointerId);
    if (activePointers.size === 2) {
      const pts = [...activePointers.values()];
      pinchBaseline = {
        distance: pointerDistance(pts[0], pts[1]),
        scale: state.scale,
        midpoint: {
          x: (pts[0].x + pts[1].x) / 2,
          y: (pts[0].y + pts[1].y) / 2,
        },
      };
    } else {
      pinchBaseline = null;
    }
    e.preventDefault();
  }

  function onPointerMove(e) {
    const tracked = activePointers.get(e.pointerId);
    if (!tracked) return;
    const pt = stagePoint(e);
    tracked.x = pt.x;
    tracked.y = pt.y;

    if (activePointers.size >= 2 && pinchBaseline) {
      const pts = [...activePointers.values()];
      const dist = pointerDistance(pts[0], pts[1]);
      if (pinchBaseline.distance > 0) {
        const nextScale = pinchBaseline.scale * (dist / pinchBaseline.distance);
        setScale(nextScale, pinchBaseline.midpoint);
      }
      return;
    }

    if (activePointers.size === 1) {
      state.offsetX = tracked.startOffsetX + (pt.x - tracked.downX);
      state.offsetY = tracked.startOffsetY + (pt.y - tracked.downY);
      clampOffsets(state);
      applyTransform();
    }
  }

  function onPointerUp(e) {
    activePointers.delete(e.pointerId);
    stage.releasePointerCapture?.(e.pointerId);
    if (activePointers.size < 2) {
      pinchBaseline = null;
    }
  }

  function onWheel(e) {
    e.preventDefault();
    // Moderate zoom-per-notch — matches typical OS track scroll at ~5% per
    // wheel tick and doesn't overshoot on trackpad inertia.
    const factor = Math.exp(-e.deltaY * 0.0015);
    setScale(state.scale * factor, stagePoint(e));
  }

  function onZoomSlider() {
    const t = parseFloat(zoomInput.value);
    if (!Number.isFinite(t)) return;
    const target = state.minScale + t * (state.maxScale - state.minScale);
    setScale(target, {
      x: state.viewportWidth / 2,
      y: state.viewportHeight / 2,
    });
  }

  // --------- open/close --------- //

  function showError(msg) {
    if (!errorEl) return;
    errorEl.textContent = msg || "";
    errorEl.hidden = !msg;
  }

  function openModal() {
    showError("");
    if (saveBtn) saveBtn.disabled = false;
    if (!img.getAttribute("src")) {
      img.setAttribute("src", imageUrl);
    }
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.setAttribute("open", "");
    }
    // Stage dimensions are unknown until the dialog is laid out — re-init
    // on every open so the viewport math stays right if the window was
    // resized between opens.
    requestAnimationFrame(() => {
      if (img.complete && img.naturalWidth > 0) {
        initAfterImageLoad();
      }
    });
  }

  function closeModal() {
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.removeAttribute("open");
    }
    activePointers.clear();
    pinchBaseline = null;
  }

  // --------- save --------- //

  function computeCrop() {
    // The visible region of the image is the viewport rectangle projected
    // back into image-pixel space, then normalized by the image's natural
    // dimensions. Because the viewport is 16:9 in CSS pixels, the image-
    // pixel aspect (crop_w * W) / (crop_h * H) is always 16:9 by
    // construction — we don't try to "fix" the normalized ratio here,
    // since crop_w/crop_h only equals 16/9 for square images (for tall
    // or wide images, the normalized ratio is a different value the
    // server validates against stored W/H).
    const scaledW = state.naturalWidth * state.scale;
    const scaledH = state.naturalHeight * state.scale;
    return {
      crop_x: clamp(-state.offsetX / scaledW, 0, 1),
      crop_y: clamp(-state.offsetY / scaledH, 0, 1),
      crop_width: clamp(state.viewportWidth / scaledW, 0, 1),
      crop_height: clamp(state.viewportHeight / scaledH, 0, 1),
    };
  }

  async function save(evt) {
    if (evt) evt.preventDefault();
    if (!state.naturalWidth || !state.naturalHeight) return;
    showError("");
    const body = computeCrop();
    saveBtn.disabled = true;
    try {
      const resp = await fetch(cropUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-CSRF-Token": csrfEl?.value || "",
        },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        window.location.reload();
        return;
      }
      let detail = "Couldn't save cover crop.";
      try {
        const data = await resp.json();
        if (data && typeof data.detail === "string") detail = data.detail;
      } catch (_) {
        /* keep the default message */
      }
      showError(detail);
      saveBtn.disabled = false;
    } catch (err) {
      showError("Network error — please try again.");
      saveBtn.disabled = false;
    }
  }

  // --------- wire events --------- //

  stage.addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerUp);
  stage.addEventListener("pointercancel", onPointerUp);
  stage.addEventListener("lostpointercapture", onPointerUp);
  stage.addEventListener("wheel", onWheel, { passive: false });

  zoomInput.addEventListener("input", onZoomSlider);
  resetBtn.addEventListener("click", (e) => {
    e.preventDefault();
    resetToFit();
  });

  cancelEls.forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      closeModal();
    });
  });

  form.addEventListener("submit", save);

  modal.addEventListener("cancel", (e) => {
    // <dialog> fires "cancel" on Esc — suppress default so our cleanup runs
    // alongside the close.
    e.preventDefault();
    closeModal();
  });

  img.addEventListener("load", () => {
    if (modal.hasAttribute("open")) {
      initAfterImageLoad();
    }
  });

  // --------- public API + trigger hook --------- //

  window.openCoverCropper = function openCoverCropper() {
    openModal();
  };

  // Hijack the "Set as cover / Adjust crop" button's parent form so the
  // click opens the modal instead of submitting the bare /cover route.
  // Without JS the form posts as-is — the server skips the crop fields.
  document.querySelectorAll("[data-cover-crop-open]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      openModal();
    });
  });
})();
