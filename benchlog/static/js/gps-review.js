/*
 * Shared modal + batch-action helper for the GPS-quarantine review flow.
 * Used by both /files (post-upload modal + pending-review section) and
 * /files/{id} (post-version-upload modal).
 *
 * Exposes `window.benchlogGpsReview`:
 *
 *   show(uploads, options)
 *     Populate and open the GPS review dialog with a list of quarantined
 *     uploads ([{version_id, version_number, filename, thumbnail_url}, ...]).
 *     Wires the dialog's batch + per-row buttons. On any successful action
 *     the page reloads (or `options.onDone()` runs if provided).
 *
 *   wirePendingSection(options)
 *     Wires the server-rendered `[data-pending-review]` section's batch +
 *     per-row buttons to the same batch endpoints. Idempotent: safe to call
 *     even when the section isn't on the page.
 *
 * options:
 *   batchUrlBase  (string, required)  Base path for the three batch endpoints,
 *                                     e.g. "/u/alice/desk-lamp/files".
 *                                     Final URLs become `${base}/strip-gps-batch`,
 *                                     `${base}/release-batch`, `${base}/discard-batch`.
 *   csrfToken     (string, required)  CSRF token for the POST headers.
 *   onDone        (function, optional) Called after a successful batch/per-row
 *                                     action with `{action, versionIds}` instead
 *                                     of `location.reload()`. Use this when the
 *                                     caller has unsaved state that a reload
 *                                     would discard (e.g. an open editor).
 */
(() => {
  "use strict";

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  async function batchAct(batchUrlBase, action, version_ids, csrfToken) {
    const url = `${batchUrlBase}/${action}-batch`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "X-CSRF-Token": csrfToken,
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      body: JSON.stringify({ version_ids }),
    });
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${detail}`);
    }
    return resp.json();
  }

  function show(uploads, opts) {
    const modal = document.querySelector("[data-gps-review-modal]");
    if (!modal) {
      // Modal template not on page — nothing to do but reload so the
      // server-rendered pending-review section surfaces the items.
      (opts.onDone || (() => location.reload()))();
      return;
    }
    const list = modal.querySelector("[data-gps-review-list]");
    const summary = modal.querySelector("[data-gps-review-summary]");
    const errorEl = modal.querySelector("[data-gps-review-error]");

    let versionIds = uploads.map(u => u.version_id);

    summary.textContent =
      `${uploads.length} photo${uploads.length === 1 ? "" : "s"} contain location data. ` +
      `Pick one for all, or review individually below.`;

    list.innerHTML = "";
    for (const u of uploads) {
      const li = document.createElement("li");
      li.dataset.versionId = u.version_id;
      const thumb = u.thumbnail_url
        ? `<img src="${u.thumbnail_url}?v=${u.version_number}" alt="" loading="lazy">`
        : '<i data-lucide="image"></i>';
      li.innerHTML = `
        <div class="gps-review-thumb">${thumb}</div>
        <div class="gps-review-meta"><strong>${escapeHtml(u.filename)}</strong></div>
        <div class="gps-review-actions">
          <button type="button" class="btn-ghost text-xs" data-gps-row-strip>Strip</button>
          <button type="button" class="btn-ghost text-xs" data-gps-row-keep>Keep</button>
          <button type="button" class="btn-ghost text-xs text-rust-deep" data-gps-row-discard>Discard</button>
        </div>`;
      list.appendChild(li);
    }
    if (window.lucide?.createIcons) window.lucide.createIcons();
    errorEl.hidden = true;
    errorEl.textContent = "";
    modal.showModal();

    const defaultDone = () => location.reload();
    const done = (action, ids) => {
      if (typeof opts.onDone === "function") {
        modal.close();
        opts.onDone({ action, versionIds: ids });
      } else {
        defaultDone();
      }
    };

    const wireBatch = (selector, action, confirmMsg) => {
      const btn = modal.querySelector(selector);
      if (!btn || btn.dataset.gpsWired === "1") return;
      btn.dataset.gpsWired = "1";
      btn.addEventListener("click", async () => {
        if (confirmMsg && !confirm(confirmMsg)) return;
        try {
          const ids = versionIds.slice();
          await batchAct(opts.batchUrlBase, action, ids, opts.csrfToken);
          done(action, ids);
        } catch (e) {
          errorEl.textContent = `${action} failed: ${e.message}`;
          errorEl.hidden = false;
        }
      });
    };
    wireBatch("[data-gps-review-strip-all]", "strip-gps");
    wireBatch("[data-gps-review-keep-all]", "release");
    wireBatch(
      "[data-gps-review-discard-all]",
      "discard",
      "Delete these photos? This cannot be undone.",
    );

    // Per-row click delegate. Wired once per page (idempotent via
    // dataset flag) so subsequent show() calls don't double-attach.
    if (list.dataset.gpsWired !== "1") {
      list.dataset.gpsWired = "1";
      list.addEventListener("click", async (e) => {
        const target = e.target.closest("button");
        if (!target) return;
        const li = target.closest("li[data-version-id]");
        if (!li) return;
        const versionId = li.dataset.versionId;
        let action;
        if (target.matches("[data-gps-row-strip]")) action = "strip-gps";
        else if (target.matches("[data-gps-row-keep]")) action = "release";
        else if (target.matches("[data-gps-row-discard]")) action = "discard";
        else return;
        try {
          await batchAct(opts.batchUrlBase, action, [versionId], opts.csrfToken);
          li.remove();
          versionIds = versionIds.filter(id => id !== versionId);
          if (!versionIds.length) done(action, [versionId]);
        } catch (err) {
          errorEl.textContent = `${action} failed: ${err.message}`;
          errorEl.hidden = false;
        }
      });
    }
  }

  function wirePendingSection(opts) {
    const section = document.querySelector("[data-pending-review]");
    if (!section) return;

    const allIds = () =>
      Array.from(section.querySelectorAll("li[data-version-id]"))
        .map(li => li.dataset.versionId);

    const wireBatch = (selector, action, confirmMsg) => {
      const btn = section.querySelector(selector);
      if (!btn) return;
      btn.addEventListener("click", async () => {
        if (confirmMsg && !confirm(confirmMsg)) return;
        try {
          await batchAct(opts.batchUrlBase, action, allIds(), opts.csrfToken);
          location.reload();
        } catch (e) {
          alert(`${action} failed: ${e.message}`);
        }
      });
    };
    wireBatch("[data-pending-strip-all]", "strip-gps");
    wireBatch("[data-pending-keep-all]", "release");
    wireBatch(
      "[data-pending-discard-all]",
      "discard",
      "Delete these photos? This cannot be undone.",
    );

    section.addEventListener("click", async (e) => {
      const target = e.target.closest("button");
      if (!target) return;
      const li = target.closest("li[data-version-id]");
      if (!li) return;
      const versionId = li.dataset.versionId;
      let action;
      if (target.matches("[data-pending-strip]")) action = "strip-gps";
      else if (target.matches("[data-pending-keep]")) action = "release";
      else if (target.matches("[data-pending-discard]")) action = "discard";
      else return;
      try {
        await batchAct(opts.batchUrlBase, action, [versionId], opts.csrfToken);
        li.remove();
        if (!section.querySelector("li[data-version-id]")) location.reload();
      } catch (err) {
        alert(`${action} failed: ${err.message}`);
      }
    });
  }

  window.benchlogGpsReview = { show, wirePendingSection, batchAct };
})();
