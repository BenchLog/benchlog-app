// Singleton Excalidraw modal editor.
//
// Usage:
//   window.BenchlogExcalidrawModal.open({
//     projectUrl: "/u/alice/bench",
//     fileId: "...uuid...",
//     filename: "drawing-1.excalidraw",
//     sceneUrl: "/u/alice/bench/files/.../raw",
//     isOwner: true,
//     onClose: () => { /* e.g. soft-refresh the file tree */ },
//   });
//
// Behavior:
//   - Lazy-loads React + ReactDOM + Excalidraw the first time it's opened.
//   - Builds a fixed-position overlay div the first time it's opened.
//   - Fetches scene JSON via `sceneUrl`, mounts Excalidraw inside the overlay.
//   - Owner: editable + Save / Close buttons. Tracks dirty state.
//   - Non-owner: read-only.
//   - Save: PUT scene as JSON to `${projectUrl}/files/${fileId}/excalidraw`.
//   - Close (button or Escape): if dirty, confirm before discarding.
//
// The bundle's asset URLs depend on EXCALIDRAW_ASSET_PATH being set
// BEFORE the bundle parses; the lazy loader below sets it before
// inserting the <script> tag for the Excalidraw bundle.

(function () {
  "use strict";

  const SCENE_MIME = "application/vnd.benchlog.excalidraw+json";

  // --- bundle loader -----------------------------------------------------

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
    // CRITICAL: set the asset path BEFORE the Excalidraw bundle script
    // parses. The bundle captures it then; setting it later means assets
    // come from the baked-in unpkg URL (which our CSP `font-src` blocks).
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

  // --- overlay markup ----------------------------------------------------

  let overlay = null;
  let mountEl = null;
  let titleEl = null;
  let titleInput = null;
  let titleErrorEl = null;
  let saveBtn = null;
  let closeBtn = null;
  let statusEl = null;

  function buildOverlay() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "excalidraw-modal";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Excalidraw drawing editor");
    overlay.hidden = true;
    overlay.innerHTML = `
      <div class="excalidraw-modal-frame">
        <div class="excalidraw-modal-bar">
          <span class="excalidraw-modal-title" data-excalidraw-modal-title
                tabindex="0" title="Click to rename"></span>
          <input type="text" class="excalidraw-modal-title-input"
                 data-excalidraw-modal-title-input maxlength="256" hidden>
          <span class="excalidraw-modal-title-error"
                data-excalidraw-modal-title-error hidden></span>
          <span class="excalidraw-modal-status" data-excalidraw-modal-status></span>
          <span class="excalidraw-modal-spacer"></span>
          <button type="button" class="btn-primary text-sm" data-excalidraw-modal-save hidden>Save</button>
          <button type="button" class="btn-ghost text-sm" data-excalidraw-modal-close>Close</button>
        </div>
        <div class="excalidraw-modal-mount" data-excalidraw-modal-mount></div>
      </div>
    `;
    document.body.appendChild(overlay);
    mountEl = overlay.querySelector("[data-excalidraw-modal-mount]");
    titleEl = overlay.querySelector("[data-excalidraw-modal-title]");
    titleInput = overlay.querySelector("[data-excalidraw-modal-title-input]");
    titleErrorEl = overlay.querySelector("[data-excalidraw-modal-title-error]");
    statusEl = overlay.querySelector("[data-excalidraw-modal-status]");
    saveBtn = overlay.querySelector("[data-excalidraw-modal-save]");
    closeBtn = overlay.querySelector("[data-excalidraw-modal-close]");
  }

  // --- modal state -------------------------------------------------------

  let reactRoot = null;
  let dirty = false;
  let saving = false;
  let activeContext = null;
  let escHandler = null;

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.dataset.kind = kind || "";
  }

  function csrfToken() {
    return (
      document.querySelector('meta[name="csrf-token"]')?.content || ""
    );
  }

  // --- save --------------------------------------------------------------

  async function save() {
    if (!activeContext || saving) return;
    if (!activeContext._getSaveBody) return;
    saving = true;
    saveBtn.disabled = true;
    setStatus("Saving…", "info");
    const body = activeContext._getSaveBody();
    try {
      const resp = await fetch(
        `${activeContext.projectUrl}/files/${activeContext.fileId}/excalidraw`,
        {
          method: "PUT",
          credentials: "same-origin",
          headers: {
            "Content-Type": SCENE_MIME,
            "X-CSRF-Token": csrfToken(),
          },
          body,
        },
      );
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const data = await resp.json();
          if (data?.detail) detail = data.detail;
        } catch (_) {}
        throw new Error(detail);
      }
      // Stash the body we just persisted as the dirty-tracking baseline.
      // Excalidraw fires onChange for non-content events (cursor moves,
      // selection, view-state) — comparing against this snapshot via
      // serializeAsJSON ignores the noise and only flags real edits.
      activeContext._lastSavedBody = body;
      dirty = false;
      setStatus("Saved", "ok");
    } catch (err) {
      setStatus(`Save failed: ${err.message}`, "err");
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
  }

  // --- rename ------------------------------------------------------------

  function setTitleError(msg) {
    if (msg) {
      titleErrorEl.textContent = msg;
      titleErrorEl.hidden = false;
    } else {
      titleErrorEl.textContent = "";
      titleErrorEl.hidden = true;
    }
  }

  function startTitleEdit() {
    if (!activeContext?.isOwner) return;
    if (activeContext?.allowRename === false) return;
    setTitleError("");
    titleInput.value = activeContext.filename || "";
    titleEl.hidden = true;
    titleInput.hidden = false;
    titleInput.focus();
    // Select the basename (everything before the .excalidraw extension)
    // so the user can type a new name without first deleting the suffix.
    const stem = titleInput.value.replace(/\.excalidraw$/i, "");
    titleInput.setSelectionRange(0, stem.length);
  }

  function stopTitleEdit() {
    titleInput.hidden = true;
    titleEl.hidden = false;
    setTitleError("");
  }

  function validateFilename(name) {
    if (!name) return "Filename is required.";
    if (name.includes("/")) return "Filename can't contain '/'.";
    if (!/\.excalidraw$/i.test(name)) return "Filename must end in .excalidraw.";
    return null;
  }

  async function commitRename() {
    if (!activeContext?.isOwner) {
      stopTitleEdit();
      return;
    }
    const newName = titleInput.value.trim();
    if (newName === activeContext.filename) {
      stopTitleEdit();
      return;
    }
    const err = validateFilename(newName);
    if (err) {
      setTitleError(err);
      return;
    }

    const body = new URLSearchParams();
    body.set("_csrf", csrfToken());
    body.set("path", activeContext.filePath || "");
    body.set("filename", newName);
    body.set("description", activeContext.fileDescription || "");
    body.set("update_refs", "1");

    try {
      const resp = await fetch(
        `${activeContext.projectUrl}/files/${activeContext.fileId}`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "X-CSRF-Token": csrfToken(),
            "Content-Type": "application/x-www-form-urlencoded",
            Accept: "application/json",
          },
          body: body.toString(),
        },
      );
      if (resp.status === 204 || resp.ok) {
        activeContext.filename = newName;
        activeContext._renamed = true;
        titleEl.textContent = newName;
        stopTitleEdit();
        return;
      }
      let detail = `Rename failed (${resp.status}).`;
      try {
        const data = await resp.json();
        if (data?.detail) detail = data.detail;
      } catch (_) {}
      setTitleError(detail);
    } catch (_) {
      setTitleError("Rename failed — check your connection.");
    }
  }

  // --- close -------------------------------------------------------------

  function tryClose() {
    if (saving) return;
    if (
      dirty &&
      !window.confirm("Discard unsaved changes?")
    ) {
      return;
    }
    close();
  }

  function close() {
    if (reactRoot) {
      try {
        reactRoot.unmount();
      } catch (_) {}
      reactRoot = null;
    }
    if (escHandler) {
      document.removeEventListener("keydown", escHandler);
      escHandler = null;
    }
    overlay.hidden = true;
    document.body.style.overflow = "";
    const prevContext = activeContext;
    activeContext = null;
    dirty = false;
    saving = false;
    setStatus("");
    stopTitleEdit();
    if (prevContext?.onClose) {
      try {
        prevContext.onClose();
      } catch (_) {}
    } else if (prevContext?._renamed) {
      // No explicit onClose handler but the user renamed during the
      // session — reload so the Files tab row reflects the new name.
      window.location.reload();
    }
  }

  // --- open --------------------------------------------------------------

  async function open(opts) {
    const { projectUrl, fileId, filename, sceneUrl, isOwner } = opts;
    if (!projectUrl || !fileId || !sceneUrl) {
      throw new Error("BenchlogExcalidrawModal.open: missing required options");
    }

    buildOverlay();
    titleEl.textContent = filename || "Drawing";
    const renameAllowed = !!isOwner && opts.allowRename !== false;
    titleEl.classList.toggle("is-editable", renameAllowed);
    titleEl.title = renameAllowed ? "Click to rename" : "";
    setTitleError("");
    saveBtn.hidden = !isOwner;
    setStatus("Loading…", "info");
    overlay.hidden = false;
    document.body.style.overflow = "hidden";
    activeContext = opts;
    activeContext._renamed = false;

    // Wire up controls (idempotent — overlay built only once).
    saveBtn.onclick = save;
    closeBtn.onclick = tryClose;
    titleEl.onclick = renameAllowed ? startTitleEdit : null;
    titleEl.onkeydown = renameAllowed
      ? (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            startTitleEdit();
          }
        }
      : null;
    titleInput.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commitRename();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        stopTitleEdit();
      }
    };
    titleInput.onblur = () => {
      // Submit on blur if changed; otherwise just close the editor. If
      // the input is hidden because we already accepted/cancelled, skip.
      if (titleInput.hidden) return;
      commitRename();
    };
    escHandler = (e) => {
      if (e.key === "Escape") {
        // Don't close the modal if the title input was the focus —
        // its own keydown handler already handled the Esc.
        if (!titleInput.hidden) return;
        e.preventDefault();
        tryClose();
      }
    };
    document.addEventListener("keydown", escHandler);

    try {
      await loadBundles();
    } catch (err) {
      setStatus(`Failed to load editor: ${err.message}`, "err");
      return;
    }

    // Fetch scene. `cache: "no-cache"` forces revalidation against the
    // server (304 if unchanged). Without it, browsers cache the GET
    // response on a heuristic, so reopening after save shows the old
    // content. We don't lose much — the route emits ETag/Last-Modified
    // via FileResponse, so unchanged files still hit the conditional path.
    let scene;
    try {
      const resp = await fetch(sceneUrl, {
        credentials: "same-origin",
        cache: "no-cache",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      scene = await resp.json();
    } catch (err) {
      setStatus(`Failed to load scene: ${err.message}`, "err");
      return;
    }

    // initialData shape, with very deliberate appState handling.
    //
    // The runtime appState contains Map/Set fields (collaborators,
    // selectedElementIds, selectedGroupIds, ...) that Excalidraw's
    // internal code calls `.forEach()` on. JSON.stringify turns those
    // into plain `{}`, and on reload they don't have `.forEach`, so
    // mounting crashes with "o.forEach is not a function". We tried
    // `restoreAppState`; it merges with defaults rather than healing the
    // broken fields, so the bad shape still wins.
    //
    // For v2 we whitelist a small set of safe-to-round-trip fields and
    // drop everything else. Excalidraw will fill in the rest from its
    // own defaults. Trade-off: zoom/scroll between sessions reset to the
    // default. Better than crashing.
    const { Excalidraw, restoreElements } = window.ExcalidrawLib;
    // `theme` is intentionally omitted — we always override with BenchLog's
    // active theme below so the editor follows the host page rather than
    // whatever theme the file was last saved with.
    const SAFE_APPSTATE_KEYS = [
      "viewBackgroundColor",
      "gridSize",
      "currentItemStrokeColor",
      "currentItemBackgroundColor",
      "currentItemFillStyle",
      "currentItemStrokeWidth",
      "currentItemStrokeStyle",
      "currentItemRoughness",
      "currentItemOpacity",
      "currentItemFontFamily",
      "currentItemFontSize",
      "currentItemTextAlign",
      "currentItemStartArrowhead",
      "currentItemEndArrowhead",
    ];
    const safeAppState = {};
    const savedAppState = scene.appState || {};
    for (const k of SAFE_APPSTATE_KEYS) {
      if (k in savedAppState) safeAppState[k] = savedAppState[k];
    }
    // Match BenchLog's current theme. base.html's inline script writes
    // `data-theme="dark"|"light"` on <html> from the user's preference
    // (system/light/dark) and prefers-color-scheme; we read that.
    safeAppState.theme =
      document.documentElement.dataset.theme === "dark" ? "dark" : "light";

    const initialData = {
      elements: restoreElements
        ? restoreElements(scene.elements || [], null)
        : (scene.elements || []),
      appState: safeAppState,
      files: scene.files || {},
    };

    // We delay building the canonical save body until save-time, via
    // `serializeAsJSON` which strips the ephemeral appState fields.
    //
    // Track latest live state via the onChange closure. We deliberately
    // omit `excalidrawAPI` — that callback is one of the v1 mounting
    // suspects, and onChange gives us everything we need anyway.
    let liveElements = initialData.elements;
    let liveAppState = initialData.appState;
    let liveFiles = initialData.files;

    const React = window.React;

    const buildBody = () => {
      const { serializeAsJSON } = window.ExcalidrawLib;
      if (serializeAsJSON) {
        return serializeAsJSON(liveElements, liveAppState, liveFiles, "local");
      }
      return JSON.stringify({
        type: "excalidraw",
        version: 2,
        source: "benchlog",
        elements: liveElements,
        appState: liveAppState,
        files: liveFiles,
      });
    };

    // Dirty tracking baselines on the first onChange Excalidraw fires
    // after mount — NOT on the initialData we passed in. Reason:
    // Excalidraw normalizes/migrates elements and fills in appState
    // defaults during mount, so `serializeAsJSON(initialData)` does NOT
    // match `serializeAsJSON(state-at-first-onChange)`. Pre-seeding from
    // initialData makes every fresh open look dirty even when the user
    // hasn't touched anything. Letting Excalidraw's own first emission
    // set the baseline avoids the mismatch entirely.
    activeContext._lastSavedBody = null;

    const App = () =>
      React.createElement(Excalidraw, {
        initialData,
        viewModeEnabled: !isOwner,
        onChange: (elements, appState, files) => {
          liveElements = elements;
          liveAppState = appState;
          liveFiles = files;
          if (!isOwner) return;
          if (activeContext._lastSavedBody === null) {
            // First emission post-mount — adopt as baseline, don't flag dirty.
            activeContext._lastSavedBody = buildBody();
            return;
          }
          const nowDirty = buildBody() !== activeContext._lastSavedBody;
          if (nowDirty !== dirty) {
            dirty = nowDirty;
            setStatus(dirty ? "Unsaved changes" : "", dirty ? "info" : "");
          }
        },
      });

    // Same closure that onChange uses for dirty-tracking; save() reads it
    // off the context to PUT the body to the server.
    activeContext._getSaveBody = buildBody;

    reactRoot = window.ReactDOM.createRoot(mountEl);
    reactRoot.render(React.createElement(App));
    setStatus("");
  }

  window.BenchlogExcalidrawModal = { open, close };
})();
