// Replaces `[data-excalidraw-embed]` placeholders (emitted by the
// markdown rewriter) with rendered SVGs of the referenced scene. The
// placeholder also carries `data-excalidraw-row-trigger`, so the global
// click handler in excalidraw-files-tab.js opens the modal on click —
// this script is purely about the visual SVG render.
//
// Bundles are lazy-loaded only when at least one placeholder exists on
// the page, so pages without embeds don't pay the ~5 MB download cost.

(function () {
  "use strict";

  let bundlePromise = null;

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.appendChild(s);
    });
  }

  function loadBundles() {
    if (bundlePromise) return bundlePromise;
    // Set asset path BEFORE the Excalidraw bundle parses — the bundle
    // captures it then; otherwise it falls back to the unpkg URL which
    // our CSP `font-src` blocks.
    if (!window.EXCALIDRAW_ASSET_PATH) {
      window.EXCALIDRAW_ASSET_PATH = "/static/js/excalidraw/";
    }
    bundlePromise = (async () => {
      await loadScript("/static/js/excalidraw/react.production.min.js");
      await loadScript("/static/js/excalidraw/react-dom.production.min.js");
      await loadScript("/static/js/excalidraw/excalidraw.production.min.js");
    })();
    return bundlePromise;
  }

  async function renderEmbed(placeholder) {
    const sceneUrl = placeholder.dataset.sceneUrl;
    if (!sceneUrl) return;
    // Clean up any prior render so re-runs (theme toggle, AJAX swap)
    // don't stack multiple SVGs/pills in one placeholder.
    placeholder
      .querySelectorAll(":scope > svg, :scope > .excalidraw-embed-edit-pill")
      .forEach((el) => el.remove());
    const existingFallback = placeholder.querySelector(
      ".excalidraw-embed-fallback",
    );
    if (existingFallback) existingFallback.hidden = false;
    placeholder.classList.remove("is-rendered");
    let scene;
    try {
      const resp = await fetch(sceneUrl, {
        credentials: "same-origin",
        cache: "no-cache",
      });
      if (!resp.ok) return; // leave fallback link visible
      scene = await resp.json();
    } catch (_) {
      return;
    }
    let svg;
    try {
      const { exportToSvg, restoreElements } = window.ExcalidrawLib;
      const elements = restoreElements
        ? restoreElements(scene.elements || [], null)
        : scene.elements || [];
      // Always render the canvas as if for light mode (saved bg, dark
      // strokes). Dark-mode adaptation happens via a CSS filter on the
      // <svg> element (see input.css `[data-theme="dark"] .excalidraw-embed > svg`),
      // mirroring how Excalidraw's own canvas does dark mode internally.
      // Pure-CSS theme means we don't have to re-render on theme toggle —
      // the browser flips the filter for us.
      svg = await exportToSvg({
        elements,
        appState: {
          ...(scene.appState || {}),
          exportBackground: true,
        },
        files: scene.files || {},
        exportPadding: 8,
      });
    } catch (err) {
      console.error("Excalidraw embed render failed", err);
      return;
    }
    svg.style.maxWidth = "100%";
    svg.style.height = "auto";
    svg.style.display = "block";
    svg.style.pointerEvents = "none"; // let clicks bubble up to the placeholder

    // Keep the fallback link in DOM but hidden so accessibility tools and
    // the modal-open click handler still see the original anchor's href.
    const fallback = placeholder.querySelector(".excalidraw-embed-fallback");
    if (fallback) fallback.hidden = true;
    placeholder.appendChild(svg);
    placeholder.classList.add("is-rendered");

    // Owner gets a small "Edit" pill for affordance discoverability. The
    // whole placeholder is clickable too (via the row-trigger handler);
    // the pill is just visual.
    if (placeholder.dataset.isOwner === "1") {
      const pill = document.createElement("span");
      pill.className = "excalidraw-embed-edit-pill";
      pill.textContent = "Edit";
      pill.setAttribute("aria-hidden", "true");
      placeholder.appendChild(pill);
    }
  }

  async function init() {
    const placeholders = document.querySelectorAll("[data-excalidraw-embed]");
    if (!placeholders.length) return;
    try {
      await loadBundles();
    } catch (err) {
      console.error(
        "Excalidraw bundle load failed; embeds fall back to link.",
        err,
      );
      return;
    }
    for (const placeholder of placeholders) {
      // eslint-disable-next-line no-await-in-loop
      await renderEmbed(placeholder);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Theme toggling is handled purely in CSS via a filter on the rendered
  // SVG — no JS re-render needed. base.html flips `<html data-theme="…">`,
  // and the matching rule in input.css applies an invert + hue-rotate
  // (matching how Excalidraw's own canvas adapts to dark mode).

  // Expose so AJAX-swapped content can re-render embeds without a full reload
  // (the description-edit endpoint returns rendered HTML; after the swap, the
  // page can call `BenchlogExcalidrawEmbed.refresh()` to render any new
  // placeholders).
  window.BenchlogExcalidrawEmbed = { refresh: init };
})();
