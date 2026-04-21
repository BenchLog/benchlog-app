/*
 * `files/…` + `journal/…` typeahead for toastui-editor (v3.x,
 * markdown mode only).
 *
 * Triggers when the user types `files/` or `journal/` on the current
 * line. A panel renders below the editor wrap (not a floating popover
 * at the cursor — simpler, and it survives resize + scroll identically).
 * Arrow keys move selection, Enter / Tab insert, Esc dismisses. Clicking
 * a row inserts.
 *
 * Context-aware insertion:
 *   - If `<trigger>/<partial>` sits directly after `](` on the current
 *     line (user is in the URL slot of a markdown link), replace just
 *     the trigger with `<trigger>/<target>`. Cursor ends up after the
 *     URL, inside the closing `)`.
 *   - Otherwise (free prose), replace with a full snippet
 *     `[<label>](<trigger>/<target>)`. The file's filename or the
 *     entry's title becomes the visible link text — short and
 *     descriptive in maker journals.
 *
 * Two data sources feed the same panel so the UX is one trigger-plus-
 * prefix with results mixed by kind. Each row shows a small type
 * indicator (image / file / journal icon) so the user can tell a file
 * reference from a journal entry at a glance.
 *
 * WYSIWYG mode bails silently. ProseMirror coordinate / selection APIs
 * differ enough that wiring the same UX there is a rabbit hole for a
 * later phase.
 *
 * Enabled per-editor: `toastui-init.js` calls `initFileLinkAutocomplete`
 * when the mount carries a parsed `__fileLinkIndex`. The parallel
 * `__journalEntryIndex` is optional — absent / empty just means no
 * journal suggestions surface.
 */
(() => {
  "use strict";

  const FILE_TRIGGER = "files/";
  const JOURNAL_TRIGGER = "journal/";
  const MAX_VISIBLE = 8;

  /**
   * Compute `full_path` for a file index entry. Root files (path === "")
   * drop the leading slash so downstream markdown renders
   * `files/foo.png` (matches the `rewrite_project_file_links` regex
   * exactly).
   */
  function filePath(entry) {
    return entry.path ? `${entry.path}/${entry.filename}` : entry.filename;
  }

  /**
   * Rank file-index entries by substring match quality.
   *   - Earliest index of the needle in `full_path` wins.
   *   - Ties broken by shorter `full_path` (tighter match surface).
   *   - Case-insensitive throughout.
   */
  function filterFiles(index, partial) {
    const needle = partial.toLowerCase();
    if (!needle) {
      return index.slice(0, MAX_VISIBLE).map((entry) => ({
        kind: "file",
        entry,
      }));
    }
    const scored = [];
    for (const entry of index) {
      const hay = filePath(entry).toLowerCase();
      const pos = hay.indexOf(needle);
      if (pos === -1) continue;
      scored.push({ entry, pos, len: hay.length });
    }
    scored.sort((a, b) => a.pos - b.pos || a.len - b.len);
    return scored.map((s) => ({ kind: "file", entry: s.entry }));
  }

  /**
   * Rank journal-entry index rows by substring match against slug +
   * title. Same ranking rules as files so the two lists feel the same.
   */
  function filterEntries(index, partial) {
    const needle = partial.toLowerCase();
    if (!needle) {
      return index.slice(0, MAX_VISIBLE).map((entry) => ({
        kind: "entry",
        entry,
      }));
    }
    const scored = [];
    for (const entry of index) {
      const hay = `${entry.slug} ${entry.title}`.toLowerCase();
      const pos = hay.indexOf(needle);
      if (pos === -1) continue;
      scored.push({ entry, pos, len: hay.length });
    }
    scored.sort((a, b) => a.pos - b.pos || a.len - b.len);
    return scored.map((s) => ({ kind: "entry", entry: s.entry }));
  }

  /**
   * Find the active `files/…` or `journal/…` trigger span on the
   * current line, or null if there isn't one. "Active" means: the most
   * recent trigger on the line up to the cursor, with no whitespace,
   * `)`, or `]` between it and the cursor (those characters close out
   * the token).
   */
  function findTrigger(line, col) {
    const upto = line.slice(0, col);
    const triggers = [FILE_TRIGGER, JOURNAL_TRIGGER];
    // Pick the trigger closest to the cursor so `files/` inside a path
    // that also appears earlier on the line as `journal/` (or vice
    // versa) uses the nearest marker.
    let best = null;
    for (const t of triggers) {
      const idx = upto.lastIndexOf(t);
      if (idx === -1) continue;
      if (best === null || idx > best.idx) {
        best = { trigger: t, idx };
      }
    }
    if (best === null) return null;
    const partial = upto.slice(best.idx + best.trigger.length);
    if (/[\s)\]]/.test(partial)) return null;
    const precedingTwo = upto.slice(Math.max(0, best.idx - 2), best.idx);
    return {
      trigger: best.trigger,
      kind: best.trigger === FILE_TRIGGER ? "file" : "entry",
      startCol: best.idx,
      partial,
      urlContext: precedingTwo === "](",
    };
  }

  function buildPanel() {
    const root = document.createElement("div");
    root.className = "file-link-autocomplete";
    root.hidden = true;
    root.setAttribute("role", "listbox");
    const list = document.createElement("ul");
    list.className = "file-link-autocomplete-list";
    root.appendChild(list);
    return { root, list };
  }

  function typeIcon(kind, hint) {
    // Lucide-style SVGs, inlined — avoids a Lucide re-init after the
    // list re-renders on every keystroke.
    let svg;
    if (kind === "file" && hint === "image") {
      svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';
    } else if (kind === "file") {
      svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
    } else {
      // Journal entry — "book-open" glyph.
      svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3Z"/></svg>';
    }
    const wrap = document.createElement("span");
    wrap.className = "file-link-autocomplete-img";
    wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML = svg;
    return wrap;
  }

  function initFileLinkAutocomplete(mount) {
    const editor = mount.__toastuiEditor;
    const fileIndex = mount.__fileLinkIndex;
    const entryIndex = mount.__journalEntryIndex || [];
    if (!editor || !Array.isArray(fileIndex)) return;
    if (mount.__fileLinkAutocompleteBound) return;
    mount.__fileLinkAutocompleteBound = true;

    const wrap =
      mount.closest(".toastui-editor-wrap") ||
      mount.closest(".toastui-editor-resizer") ||
      mount;

    const { root: panel, list } = buildPanel();
    document.body.appendChild(panel);

    let matches = [];
    let activeIndex = 0;
    let currentTrigger = null;

    function hide() {
      panel.hidden = true;
      matches = [];
      activeIndex = 0;
      currentTrigger = null;
    }

    function render() {
      list.innerHTML = "";
      matches.forEach((match, i) => {
        const row = document.createElement("li");
        row.className = "file-link-autocomplete-row";
        if (i === activeIndex) row.classList.add("is-active");
        row.setAttribute("role", "option");
        row.dataset.index = String(i);

        if (match.kind === "file") {
          row.appendChild(
            typeIcon("file", match.entry.is_image ? "image" : "file"),
          );
          if (match.entry.path) {
            const prefix = document.createElement("span");
            prefix.className = "file-link-autocomplete-path";
            prefix.textContent = `${match.entry.path}/`;
            row.appendChild(prefix);
          }
          const name = document.createElement("span");
          name.className = "file-link-autocomplete-name";
          name.textContent = match.entry.filename;
          row.appendChild(name);
        } else {
          row.appendChild(typeIcon("entry"));
          const name = document.createElement("span");
          name.className = "file-link-autocomplete-name";
          name.textContent = match.entry.title || match.entry.slug;
          row.appendChild(name);
          const slug = document.createElement("span");
          slug.className = "file-link-autocomplete-path";
          slug.textContent = ` · ${match.entry.slug}`;
          row.appendChild(slug);
        }

        row.addEventListener("mousedown", (evt) => {
          evt.preventDefault();
          activeIndex = i;
          insertSelected();
        });
        list.appendChild(row);
      });
    }

    function updateActive() {
      const rows = list.querySelectorAll(".file-link-autocomplete-row");
      rows.forEach((r, i) => r.classList.toggle("is-active", i === activeIndex));
    }

    function inMarkdownMode() {
      return typeof editor.isMarkdownMode === "function" && editor.isMarkdownMode();
    }

    function caretCoords() {
      try {
        const md = typeof editor.getCurrentModeEditor === "function"
          ? editor.getCurrentModeEditor()
          : null;
        const view = md && md.view;
        if (!view || typeof view.coordsAtPos !== "function") return null;
        const pos = view.state.selection.from;
        return view.coordsAtPos(pos);
      } catch (_) {
        return null;
      }
    }

    function positionPanel() {
      const coords = caretCoords();
      const PANEL_OFFSET = 4;
      const margin = 8;
      let top;
      let left;
      if (coords) {
        top = coords.bottom + PANEL_OFFSET;
        left = coords.left;
      } else {
        const rect = wrap.getBoundingClientRect();
        top = rect.bottom + PANEL_OFFSET;
        left = rect.left;
      }
      panel.style.visibility = "hidden";
      panel.hidden = false;
      const panelRect = panel.getBoundingClientRect();
      const maxLeft = window.innerWidth - panelRect.width - margin;
      if (left > maxLeft) left = Math.max(margin, maxLeft);
      const maxTop = window.innerHeight - panelRect.height - margin;
      if (top > maxTop) {
        const aboveTop = (coords ? coords.top : wrap.getBoundingClientRect().top)
          - panelRect.height - PANEL_OFFSET;
        top = Math.max(margin, aboveTop);
      }
      panel.style.top = `${Math.round(top)}px`;
      panel.style.left = `${Math.round(left)}px`;
      panel.style.visibility = "";
    }

    function refresh() {
      if (!inMarkdownMode()) {
        hide();
        return;
      }
      const selection = editor.getSelection();
      if (!Array.isArray(selection) || selection.length < 2) {
        hide();
        return;
      }
      const [start, end] = selection;
      if (start[0] !== end[0] || start[1] !== end[1]) {
        hide();
        return;
      }
      const line = end[0];
      const col = Math.max(0, end[1] - 1);
      const markdown = editor.getMarkdown();
      const lines = markdown.split("\n");
      const lineText = lines[line - 1] || "";
      const trigger = findTrigger(lineText, col);
      if (!trigger) {
        hide();
        return;
      }
      currentTrigger = { line, ...trigger };
      if (trigger.kind === "file") {
        matches = filterFiles(fileIndex, trigger.partial).slice(0, MAX_VISIBLE);
      } else {
        matches = filterEntries(entryIndex, trigger.partial).slice(0, MAX_VISIBLE);
      }
      if (matches.length === 0) {
        hide();
        return;
      }
      activeIndex = 0;
      render();
      positionPanel();
    }

    function insertSelected() {
      if (!currentTrigger || matches.length === 0) return;
      const match = matches[activeIndex];
      let target;
      let label;
      if (match.kind === "file") {
        target = filePath(match.entry);
        label = match.entry.filename;
      } else {
        target = match.entry.slug;
        label = match.entry.title || match.entry.slug;
      }
      const trigger = currentTrigger.trigger;
      const insertText = currentTrigger.urlContext
        ? `${trigger}${target}`
        : `[${label}](${trigger}${target})`;

      const line = currentTrigger.line;
      const startCol = currentTrigger.startCol + 1;
      const endCol = startCol + trigger.length + currentTrigger.partial.length;
      try {
        editor.setSelection([line, startCol], [line, endCol]);
        editor.replaceSelection(insertText);
      } catch (_) {
        /* defensive: drop silently if API shape drifts */
      }
      hide();
      if (typeof editor.focus === "function") editor.focus();
    }

    editor.on("change", refresh);
    if (typeof editor.on === "function") {
      try {
        editor.on("changeMode", hide);
      } catch (_) {
        /* older builds may lack this event — harmless */
      }
    }

    const writerRoot = mount;
    function onKeydown(evt) {
      if (panel.hidden) return;
      if (!writerRoot.contains(evt.target)) return;
      if (evt.key === "ArrowDown") {
        evt.preventDefault();
        activeIndex = (activeIndex + 1) % matches.length;
        updateActive();
      } else if (evt.key === "ArrowUp") {
        evt.preventDefault();
        activeIndex = (activeIndex - 1 + matches.length) % matches.length;
        updateActive();
      } else if (evt.key === "Enter" || evt.key === "Tab") {
        evt.preventDefault();
        insertSelected();
      } else if (evt.key === "Escape") {
        evt.preventDefault();
        hide();
        if (typeof editor.focus === "function") editor.focus();
      }
    }
    document.addEventListener("keydown", onKeydown, true);

    document.addEventListener("mousedown", (evt) => {
      if (panel.hidden) return;
      if (panel.contains(evt.target)) return;
      if (writerRoot.contains(evt.target)) return;
      hide();
    });

    if (typeof editor.on === "function") {
      try {
        editor.on("blur", () => {
          setTimeout(() => {
            if (!panel.matches(":hover")) hide();
          }, 120);
        });
      } catch (_) {
        /* not all versions fire blur — not fatal */
      }
    }
  }

  window.initFileLinkAutocomplete = initFileLinkAutocomplete;
})();
