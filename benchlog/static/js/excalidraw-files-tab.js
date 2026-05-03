// Files-tab wiring for the Excalidraw modal.
//
// Triggers handled here:
//   - [data-excalidraw-new-trigger]: top-right "New drawing" button.
//     POSTs to `${createUrl}` with a timestamped name, then opens the
//     modal on the new file.
//   - [data-excalidraw-row-trigger]: a file row link or anchor for a
//     `.excalidraw` file. Intercepts left-click only — modifier-key
//     clicks (cmd/ctrl/shift, middle button) still navigate normally to
//     the file's detail page so right-click "open in new tab" works.

(function () {
  "use strict";

  function csrfToken() {
    return (
      document.querySelector('meta[name="csrf-token"]')?.content || ""
    );
  }

  function timestampedName() {
    return `drawing-${new Date()
      .toISOString()
      .replace(/[-:T.]/g, "")
      .slice(0, 14)}.excalidraw`;
  }

  function openModalFor(trigger) {
    const projectUrl = trigger.dataset.projectUrl;
    const fileId = trigger.dataset.fileId;
    const filename = trigger.dataset.filename || "Drawing";
    const filePath = trigger.dataset.filePath || "";
    const fileDescription = trigger.dataset.fileDescription || "";
    const sceneUrl = trigger.dataset.sceneUrl;
    const isOwner = trigger.dataset.isOwner === "1";
    // Embeds set data-allow-rename="0" — rename belongs on the Files tab,
    // not inline in a doc. Defaults true for triggers that omit the attr.
    const allowRename = trigger.dataset.allowRename !== "0";
    if (!window.BenchlogExcalidrawModal) {
      window.alert("Editor failed to load.");
      return;
    }
    window.BenchlogExcalidrawModal.open({
      projectUrl,
      fileId,
      filename,
      filePath,
      fileDescription,
      sceneUrl,
      isOwner,
      allowRename,
    });
  }

  // --- new drawing button ------------------------------------------------

  document.addEventListener("click", async (event) => {
    const newBtn = event.target.closest("[data-excalidraw-new-trigger]");
    if (!newBtn) return;
    event.preventDefault();

    const createUrl = newBtn.dataset.createUrl;
    if (!createUrl) return;
    const path = newBtn.dataset.path || "";

    const fd = new FormData();
    fd.append("_csrf", csrfToken());
    fd.append("name", timestampedName());
    if (path) fd.append("path", path);

    let payload;
    try {
      const resp = await fetch(createUrl, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) {
        let msg = `Could not create drawing (${resp.status}).`;
        try {
          const data = await resp.json();
          if (data?.detail) msg = data.detail;
        } catch (_) {}
        window.alert(msg);
        return;
      }
      payload = await resp.json();
    } catch (_) {
      window.alert("Could not create drawing — network error.");
      return;
    }

    const projectUrl = newBtn.dataset.projectUrl;
    if (!window.BenchlogExcalidrawModal) {
      // Fall back to navigating somewhere useful.
      window.location.assign(`${projectUrl}/files/${payload.id}`);
      return;
    }
    window.BenchlogExcalidrawModal.open({
      projectUrl,
      fileId: payload.id,
      filename: payload.filename,
      filePath: payload.path || "",
      fileDescription: "",
      sceneUrl: `${projectUrl}/files/${payload.id}/raw`,
      isOwner: true,
      onClose: () => {
        // Reload so the new file appears in the tree. Cheap; matches the
        // existing upload-then-reload pattern in this tab.
        window.location.reload();
      },
    });
  });

  // --- row click ---------------------------------------------------------

  document.addEventListener("click", (event) => {
    // Modifier keys / middle click → let the browser navigate.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
      return;
    }
    const trigger = event.target.closest("[data-excalidraw-row-trigger]");
    if (!trigger) return;
    event.preventDefault();
    openModalFor(trigger);
  });
})();
