/*
 * Shared file-upload pipeline used by the Files tab (tree + empty-state drop
 * zone), the file detail page (new-version drop zone), and the Gallery tab
 * (Upload image button). Keeps the recursive-folder-walk, bounded-concurrency
 * uploader, and status-banner wiring in one place.
 *
 * Exposes `window.benchlogFileUpload`:
 *
 *   handleDrop(dataTransfer, options)
 *     Walks the DataTransfer for files (including folders via
 *     webkitGetAsEntry) and uploads each one.
 *
 *   handleFiles(fileList, options)
 *     Takes an already-flattened FileList (e.g. from <input type="file">)
 *     and uploads each file. No folder recursion.
 *
 * options:
 *   uploadUrl  (string, required)  POST endpoint for each upload
 *   basePath   (string, optional)  'path' form value for uploads (the
 *                                   drop target's folder); combined with
 *                                   any relativePath discovered during a
 *                                   folder walk
 *   extraFields (object, optional) extra name/value form fields to include
 *                                   on every upload (e.g. show_in_gallery=1)
 *   statusEl   (element, optional) element that receives text updates for
 *                                   "Uploading N / M…" / "Refreshing…"
 *   concurrency (number, optional) parallel upload limit (default 4)
 *   reloadOnDone (boolean, default true) reload the page after uploads
 *                                   finish so the server re-renders the
 *                                   mutated state
 *   onComplete (function, optional) fallback for when reloadOnDone is false;
 *                                   receives ({ failures: string[] })
 */
(() => {
  "use strict";

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  function setStatus(el, text) {
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
  }

  function clearStatus(el) {
    if (!el) return;
    el.hidden = true;
    el.textContent = "";
  }

  async function walkEntry(entry, prefix, out) {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) =>
        entry.file(resolve, reject)
      );
      out.push({ file, relativePath: prefix });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const childPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
      while (true) {
        const batch = await new Promise((resolve, reject) =>
          reader.readEntries(resolve, reject)
        );
        if (!batch.length) break;
        for (const child of batch) {
          await walkEntry(child, childPrefix, out);
        }
      }
    }
  }

  async function collectDroppedFiles(dataTransfer) {
    const out = [];
    const items = dataTransfer.items;
    if (items && items.length && items[0].webkitGetAsEntry) {
      const entries = [];
      for (const item of items) {
        const entry = item.webkitGetAsEntry?.();
        if (entry) entries.push(entry);
      }
      for (const entry of entries) {
        await walkEntry(entry, "", out);
      }
    } else if (dataTransfer.files) {
      for (const file of dataTransfer.files) {
        out.push({ file, relativePath: "" });
      }
    }
    return out;
  }

  async function runWithConcurrency(items, limit, fn) {
    let i = 0;
    const workers = Array.from(
      { length: Math.min(Math.max(limit, 1), items.length) },
      async () => {
        while (i < items.length) {
          const idx = i++;
          await fn(items[idx], idx);
        }
      }
    );
    await Promise.all(workers);
  }

  async function uploadOne(item, opts) {
    const finalPath = [opts.basePath || "", item.relativePath]
      .filter(Boolean)
      .join("/");
    const formData = new FormData();
    formData.append("_csrf", csrfToken());
    formData.append("path", finalPath);
    formData.append("description", "");
    if (opts.extraFields) {
      for (const [k, v] of Object.entries(opts.extraFields)) {
        formData.append(k, v);
      }
    }
    formData.append("upload", item.file, item.file.name);
    try {
      const resp = await fetch(opts.uploadUrl, {
        method: "POST",
        body: formData,
        headers: { Accept: "application/json" },
      });
      if (resp.ok) {
        try {
          const body = await resp.json();
          return { ok: true, body };
        } catch {
          return { ok: true, body: null };
        }
      }
      let msg = `${item.file.name} (${resp.status})`;
      try {
        const payload = await resp.json();
        if (payload.detail) msg = `${item.file.name} — ${payload.detail}`;
      } catch {}
      return { ok: false, msg };
    } catch {
      return { ok: false, msg: `${item.file.name} — network error` };
    }
  }

  async function runUploads(items, opts) {
    const statusEl = opts.statusEl || null;
    if (!items.length) {
      clearStatus(statusEl);
      return { failures: [], successes: [] };
    }
    let done = 0;
    const failures = [];
    const successes = [];
    setStatus(statusEl, `Uploading 0 / ${items.length}…`);
    const concurrency = opts.concurrency ?? 4;
    await runWithConcurrency(items, concurrency, async (item) => {
      const result = await uploadOne(item, opts);
      done += 1;
      if (result.ok) {
        if (result.body) successes.push(result.body);
      } else {
        failures.push(result.msg);
      }
      setStatus(
        statusEl,
        `Uploaded ${done - failures.length} / ${items.length}` +
          (failures.length ? `, ${failures.length} failed` : "")
      );
    });
    if (failures.length) {
      alert(`Some files failed to upload:\n\n${failures.join("\n")}`);
    }
    if (opts.reloadOnDone !== false) {
      setStatus(statusEl, "Refreshing…");
      location.reload();
    } else if (typeof opts.onComplete === "function") {
      opts.onComplete({ failures, successes });
    }
    return { failures, successes };
  }

  async function handleDrop(dataTransfer, opts = {}) {
    if (!opts.uploadUrl) {
      throw new Error("uploadUrl is required");
    }
    setStatus(opts.statusEl, "Reading dropped items…");
    const items = await collectDroppedFiles(dataTransfer);
    return runUploads(items, opts);
  }

  async function handleFiles(fileList, opts = {}) {
    if (!opts.uploadUrl) {
      throw new Error("uploadUrl is required");
    }
    const items = [];
    for (const file of fileList || []) {
      items.push({ file, relativePath: "" });
    }
    return runUploads(items, opts);
  }

  function isExternalFileDrag(e) {
    return Array.from(e.dataTransfer?.types || []).includes("Files");
  }

  window.benchlogFileUpload = {
    handleDrop,
    handleFiles,
    isExternalFileDrag,
  };
})();
