/*
 * `files/…` typeahead for toastui-editor (v3.x, markdown mode only).
 *
 * Triggers when the user types `files/` on the current line. A panel
 * renders below the editor wrap (not a floating popover at the cursor —
 * simpler, and it survives resize + scroll identically). Arrow keys move
 * selection, Enter / Tab insert, Esc dismisses. Clicking a row inserts.
 *
 * Context-aware insertion:
 *   - If `files/<partial>` sits directly after `](` on the current line
 *     (user is in the URL slot of a markdown link), replace just the
 *     trigger with `files/<full_path>`. Cursor ends up after the URL,
 *     inside the closing `)`.
 *   - Otherwise (free prose), replace with a full snippet
 *     `[<filename>](files/<full_path>)`. The filename becomes the visible
 *     link text — short and descriptive in maker journals.
 *
 * WYSIWYG mode bails silently. ProseMirror coordinate / selection APIs
 * differ enough that wiring the same UX there is a rabbit hole for a
 * later phase.
 *
 * Enabled per-editor: `toastui-init.js` calls `initFileLinkAutocomplete`
 * when the mount carries a parsed `__fileLinkIndex`. Absent index →
 * the function never runs, so the bio editor stays pristine.
 */
(() => {
  "use strict";

  const TRIGGER = "files/";
  const MAX_VISIBLE = 8;

  /**
   * Compute `full_path` for an index entry. Root files (path === "") drop
   * the leading slash so downstream markdown renders `files/foo.png`
   * (matches the `rewrite_project_file_links` regex exactly).
   */
  function fullPathFor(entry) {
    return entry.path ? `${entry.path}/${entry.filename}` : entry.filename;
  }

  /**
   * Rank index entries by substring match quality.
   *   - Earliest index of the needle in `full_path` wins.
   *   - Ties broken by shorter `full_path` (tighter match surface).
   *   - Case-insensitive throughout.
   * Returns a new array, capped at MAX_VISIBLE.
   */
  function filterIndex(index, partial) {
    const needle = partial.toLowerCase();
    if (!needle) {
      // No partial → show the first N entries so the user still sees
      // something useful right after typing `files/`.
      return index.slice(0, MAX_VISIBLE);
    }
    const scored = [];
    for (const entry of index) {
      const hay = fullPathFor(entry).toLowerCase();
      const pos = hay.indexOf(needle);
      if (pos === -1) continue;
      scored.push({ entry, pos, len: hay.length });
    }
    scored.sort((a, b) => a.pos - b.pos || a.len - b.len);
    return scored.slice(0, MAX_VISIBLE).map((s) => s.entry);
  }

  /**
   * Find the active `files/…` trigger span on the current line, or null if
   * there isn't one. "Active" means: the most recent `files/` on the line
   * up to the cursor, with no whitespace, `)`, or `]` between it and the
   * cursor (those characters close out the token).
   */
  function findTrigger(line, col) {
    const upto = line.slice(0, col);
    const idx = upto.lastIndexOf(TRIGGER);
    if (idx === -1) return null;
    const partial = upto.slice(idx + TRIGGER.length);
    // Any of these close out the trigger — once typed, the user isn't in
    // a token any more and we shouldn't resurrect the panel.
    if (/[\s)\]]/.test(partial)) return null;
    const precedingTwo = upto.slice(Math.max(0, idx - 2), idx);
    return {
      startCol: idx,
      partial,
      urlContext: precedingTwo === "](",
    };
  }

  /**
   * Build the panel DOM. Returns the root element + row container so the
   * caller can rerender rows without re-creating the outer structure.
   */
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

  function imageHintIcon() {
    // Lucide `image` SVG, inlined — avoids a Lucide re-init after the list
    // re-renders on every keystroke.
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';
    const wrap = document.createElement("span");
    wrap.className = "file-link-autocomplete-img";
    wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML = svg;
    return wrap;
  }

  function initFileLinkAutocomplete(mount) {
    const editor = mount.__toastuiEditor;
    const index = mount.__fileLinkIndex;
    if (!editor || !Array.isArray(index)) return;
    if (mount.__fileLinkAutocompleteBound) return;
    mount.__fileLinkAutocompleteBound = true;

    // Fallback anchor for the "can't get caret coords" branch of
    // `positionPanel`. The shared partial wraps the mount in
    // `.toastui-editor-wrap`, but the inline-edit flow builds its mount
    // inside `.toastui-editor-resizer` only — fall back to that, and
    // finally to the mount itself so init never silently bails.
    const wrap =
      mount.closest(".toastui-editor-wrap") ||
      mount.closest(".toastui-editor-resizer") ||
      mount;

    const { root: panel, list } = buildPanel();
    // Panel is fixed-positioned near the caret. Attach to body so it
    // doesn't inherit transforms / overflow clips from the editor chrome.
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
      matches.forEach((entry, i) => {
        const row = document.createElement("li");
        row.className = "file-link-autocomplete-row";
        if (i === activeIndex) row.classList.add("is-active");
        row.setAttribute("role", "option");
        row.dataset.index = String(i);

        if (entry.is_image) row.appendChild(imageHintIcon());

        if (entry.path) {
          const prefix = document.createElement("span");
          prefix.className = "file-link-autocomplete-path";
          prefix.textContent = `${entry.path}/`;
          row.appendChild(prefix);
        }
        const name = document.createElement("span");
        name.className = "file-link-autocomplete-name";
        name.textContent = entry.filename;
        row.appendChild(name);

        row.addEventListener("mousedown", (evt) => {
          // mousedown (not click) so the editor blur handler doesn't
          // race ahead and hide the panel before the click lands.
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

    /**
     * Is the editor currently in markdown mode? v3 exposes `isMarkdownMode`
     * on the editor instance. Older builds might be missing it — if so,
     * play it safe and bail.
     */
    function inMarkdownMode() {
      return typeof editor.isMarkdownMode === "function" && editor.isMarkdownMode();
    }

    /** Best-effort: grab viewport pixel coords of the caret in markdown
     *  mode. toastui's markdown editor wraps a ProseMirror view; we ask it
     *  for `coordsAtPos(selection.from)`. Returns null if any rung of that
     *  chain isn't present. */
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

    /** Position the panel just below the caret, clamping to the viewport so
     *  it never clips off the right edge or falls below the fold. Falls back
     *  to the editor's bottom-left when coords aren't available. */
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
      // Width is capped via CSS `max-width`; measure after it's briefly
      // visible off-screen to clamp left into the viewport.
      panel.style.visibility = "hidden";
      panel.hidden = false;
      const panelRect = panel.getBoundingClientRect();
      const maxLeft = window.innerWidth - panelRect.width - margin;
      if (left > maxLeft) left = Math.max(margin, maxLeft);
      const maxTop = window.innerHeight - panelRect.height - margin;
      if (top > maxTop) {
        // Not enough room below — flip above the caret.
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
      // Markdown-mode selection is [[startLine, startCol], [endLine, endCol]]
      // using 1-based lines and 1-based columns. Only act on a collapsed
      // caret — typing into a range selection isn't the trigger-typing UX
      // we're modelling.
      const [start, end] = selection;
      if (start[0] !== end[0] || start[1] !== end[1]) {
        hide();
        return;
      }
      const line = end[0];
      // `end[1]` is 1-based; convert to a 0-based string index for slicing.
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
      matches = filterIndex(index, trigger.partial);
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
      const entry = matches[activeIndex];
      const fullPath = fullPathFor(entry);
      const insertText = currentTrigger.urlContext
        ? `${TRIGGER}${fullPath}`
        : `[${entry.filename}](${TRIGGER}${fullPath})`;

      // toastui v3 markdown-mode coords: `getSelection` returns 1-based line
      // + 1-based column, but our `startCol` comes from a JS string index
      // (0-based). Convert both ends with +1 so we select the actual trigger
      // span — without this, selection shifts left by one char, eating the
      // preceding `(` in URL context and leaving a trailing `/` in free prose.
      const line = currentTrigger.line;
      const startCol = currentTrigger.startCol + 1;
      const endCol = startCol + TRIGGER.length + currentTrigger.partial.length;
      try {
        editor.setSelection([line, startCol], [line, endCol]);
        editor.replaceSelection(insertText);
      } catch (_) {
        // Defensive: if the API shape drifts, just hide rather than blow up.
      }
      hide();
      // Return focus to the editor so keyboard flow continues.
      if (typeof editor.focus === "function") editor.focus();
    }

    editor.on("change", refresh);
    // Switching to WYSIWYG mid-flow should immediately collapse the panel.
    if (typeof editor.on === "function") {
      try {
        editor.on("changeMode", hide);
      } catch (_) {
        /* older builds may lack this event — harmless */
      }
    }

    // Key handling. We attach at document level (capture phase) so we can
    // preempt toastui's own handling when the panel is visible — the
    // editor doesn't expose a keydown hook we can piggy-back on
    // consistently across versions. Filter to events targeting the
    // editor's writer DOM so we don't hijack keys everywhere on the page.
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

    // Click outside → hide. Inside the panel itself is handled by mousedown
    // on the row; clicks on the editor surface fall through to refresh().
    document.addEventListener("mousedown", (evt) => {
      if (panel.hidden) return;
      if (panel.contains(evt.target)) return;
      if (writerRoot.contains(evt.target)) return;
      hide();
    });

    // Blur hide with a micro-delay so a click on a panel row still lands —
    // the row's mousedown fires first, we do the insert, then focus comes
    // back to the editor. Without the delay, the blur-hide would fire
    // between the row's mousedown and the insert, hiding the matches
    // array we're about to read.
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
