/*
 * Shared combobox mount — pills + search input + dropdown + keyboard nav.
 *
 * Used by the tag, category, and collections pickers. All three previously
 * carried their own near-identical copies of the same mechanics (outside-
 * click, dialog-aware dropdown positioning, lucide icon refresh, etc.);
 * this is the one canonical implementation.
 *
 * Hosts call `window.benchlogMountCombobox(root, config)` after the root
 * element + its `<script type="application/json" data-combobox-config>`
 * block are in the DOM. See `components/_combobox.html` for the partial
 * that renders the markup.
 *
 * Returns a small handle: {getSelected, setSelected, addOption,
 * removeOption, destroy} so hosts can drive the widget programmatically
 * (e.g., to push a freshly-created option into the options catalog).
 */
(() => {
  "use strict";

  function defaultMatch(option, queryLower) {
    return (option.label || "").toLowerCase().includes(queryLower);
  }

  function defaultPillRenderer(option, container) {
    const span = document.createElement("span");
    span.textContent = option.label;
    container.appendChild(span);
  }

  function defaultOptionRenderer(option, container, isCreate) {
    container.textContent = isCreate
      ? `Create "${option.label}"`
      : option.label;
  }

  function refreshLucide() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  function mountCombobox(root, cfg) {
    const inner = root.querySelector("[data-combobox-inner]");
    const pillsEl = root.querySelector("[data-combobox-pills]");
    const search = root.querySelector("[data-combobox-search]");
    const dropdown = root.querySelector("[data-combobox-dropdown]");
    const hidden = root.querySelector("[data-combobox-hidden]");
    const hiddenMulti = root.querySelector("[data-combobox-hidden-multi]");

    const options = Array.isArray(cfg.options) ? cfg.options.slice() : [];
    let selected = Array.isArray(cfg.selected) ? cfg.selected.slice() : [];
    const multi = !!cfg.multi;
    const name = cfg.name || "value";
    const allowCreate = !!cfg.allowCreate;
    const existingOnly = !!cfg.existingOnly;
    const matchFn = typeof cfg.matchFn === "function" ? cfg.matchFn : defaultMatch;
    const pillRenderer =
      typeof cfg.pillRenderer === "function" ? cfg.pillRenderer : defaultPillRenderer;
    const optionRenderer =
      typeof cfg.optionRenderer === "function" ? cfg.optionRenderer : defaultOptionRenderer;
    const onAdd = typeof cfg.onAdd === "function" ? cfg.onAdd : null;
    const onRemove = typeof cfg.onRemove === "function" ? cfg.onRemove : null;
    const onCreate = typeof cfg.onCreate === "function" ? cfg.onCreate : null;
    const onStatus = typeof cfg.onStatus === "function" ? cfg.onStatus : null;
    const onChange = typeof cfg.onChange === "function" ? cfg.onChange : null;
    // Optional pool filter — runs after the "already selected" pruning,
    // letting hosts hide options that conflict with the current selection
    // beyond simple identity (e.g. category picker hides ancestors/
    // descendants of any selected leaf so the taxonomy stays clean).
    const poolFilter =
      typeof cfg.poolFilter === "function" ? cfg.poolFilter : null;
    const normalizeCreate =
      typeof cfg.normalizeCreate === "function" ? cfg.normalizeCreate : (s) => s.trim();
    const changeEventName = cfg.changeEventName || "combobox-change";
    const extraChangeEvents = Array.isArray(cfg.extraChangeEvents)
      ? cfg.extraChangeEvents
      : [];

    const byValue = new Map();
    options.forEach((o) => byValue.set(o.value, o));
    let active = -1;

    // Undo/redo stack for local (non-async-hooked) mutations. Hosts
    // wiring onAdd/onRemove bypass this — rolling back a server round-trip
    // with Cmd+Z would be confusing. Tag form uses it; collections picker
    // (with async hooks) does not.
    const HISTORY_LIMIT = 50;
    const history = { past: [], future: [] };
    const pushHistory = () => {
      history.past.push([...selected]);
      if (history.past.length > HISTORY_LIMIT) history.past.shift();
      history.future.length = 0;
    };

    // Inner/outer find helpers so consumers can splice options in at runtime.
    const findOption = (value) => byValue.get(value);

    const currentQuery = () => search.value.trim();

    const visibleOptions = () => {
      const qRaw = currentQuery();
      const q = qRaw.toLowerCase();
      let pool = options.filter((o) => !selected.includes(o.value));
      if (poolFilter) {
        pool = pool.filter((o) => poolFilter(o, selected));
      }
      const matches = (q
        ? pool.filter((o) => matchFn(o, q))
        : pool
      ).slice(0, 15);
      const items = matches.map((o) => ({ kind: "existing", option: o }));
      if (allowCreate && qRaw) {
        const normalized = normalizeCreate(qRaw);
        if (normalized) {
          // Only offer "Create" when the typed value doesn't match any
          // existing option exactly (by label, case-insensitive) AND the
          // host hasn't already added it under a matching value.
          const lower = normalized.toLowerCase();
          const exact = options.find(
            (o) => (o.label || "").toLowerCase() === lower ||
                   String(o.value).toLowerCase() === lower
          );
          if (!exact) {
            items.push({
              kind: "create",
              option: { value: normalized, label: normalized },
            });
          }
        }
      }
      return items;
    };

    const syncHidden = () => {
      if (!hidden && !hiddenMulti) return;
      if (multi) {
        if (!hiddenMulti) return;
        hiddenMulti.innerHTML = "";
        selected.forEach((value) => {
          const inp = document.createElement("input");
          inp.type = "hidden";
          inp.name = name;
          inp.value = value;
          hiddenMulti.appendChild(inp);
        });
      } else if (hidden) {
        hidden.value = selected.join(", ");
      }
    };

    const dispatchChange = () => {
      const payload = { selected: [...selected] };
      const dispatch = (type) =>
        root.dispatchEvent(
          new CustomEvent(type, { bubbles: true, detail: payload })
        );
      dispatch(changeEventName);
      extraChangeEvents.forEach(dispatch);
      // Direct-invocation callback — belt-and-suspenders alongside the
      // event path so consumers aren't stuck debugging bubbling/dialog
      // stacking context issues. The event still fires for listeners
      // that prefer the DOM API.
      if (onChange) {
        try { onChange(payload.selected); } catch (_) { /* swallow */ }
      }
    };

    const renderPills = () => {
      pillsEl.innerHTML = "";
      selected.forEach((value) => {
        const opt = findOption(value) || { value, label: String(value) };
        const pill = document.createElement("span");
        pill.className = "tag-pill";
        pill.dataset.pillValue = value;
        const text = document.createElement("span");
        text.style.display = "inline-flex";
        text.style.alignItems = "center";
        pillRenderer(opt, text);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tag-pill-remove";
        btn.setAttribute("aria-label", "Remove " + (opt.label || String(value)));
        btn.dataset.removeValue = value;
        btn.innerHTML = '<i data-lucide="x" class="w-3 h-3"></i>';
        pill.appendChild(text);
        pill.appendChild(btn);
        pillsEl.appendChild(pill);
      });
      // Pill changes are infrequent — safe to ask lucide to swap every
      // <i data-lucide> placeholder for the real <svg> markup.
      refreshLucide();
    };

    // When this combobox lives inside a <dialog>, the default
    // absolute-positioned dropdown anchors to its parent — but the dialog's
    // stacking context can clip it visually below the dialog border. Switch
    // to fixed positioning anchored to the input's viewport rect so the
    // dropdown rides the same top-layer as the dialog itself. Outside a
    // dialog, leave the default CSS alone.
    const hostDialog = root.closest("dialog");
    const positionDropdown = () => {
      if (!hostDialog) return;
      const rect = search.getBoundingClientRect();
      dropdown.style.position = "fixed";
      dropdown.style.top = `${Math.round(rect.bottom + 4)}px`;
      dropdown.style.left = `${Math.round(rect.left)}px`;
      dropdown.style.width = `${Math.round(rect.width)}px`;
      dropdown.style.zIndex = "1000";
    };

    // Show/hide is controlled by explicit user intent (focus, typing,
    // arrow keys) — NOT side-effects of state changes. A pill add or
    // remove must not pop the dropdown open if the user wasn't already
    // interacting with the input. Pass `{show: true}` to force open.
    const renderDropdown = (opts) => {
      const force = opts && opts.show === true;
      const items = visibleOptions();
      dropdown.innerHTML = "";
      if (items.length === 0) {
        dropdown.hidden = true;
        search.setAttribute("aria-expanded", "false");
        active = -1;
        return;
      }
      if (active >= items.length) active = items.length - 1;
      if (active < 0) active = 0;
      items.forEach((item, i) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tag-option" + (i === active ? " is-active" : "");
        btn.setAttribute("role", "option");
        btn.setAttribute("aria-selected", i === active ? "true" : "false");
        btn.style.display = "flex";
        btn.style.alignItems = "center";
        if (item.kind === "existing") {
          btn.dataset.addValue = item.option.value;
          optionRenderer(item.option, btn, false);
        } else {
          btn.dataset.createValue = item.option.value;
          optionRenderer(item.option, btn, true);
        }
        dropdown.appendChild(btn);
      });
      const wasOpen = !dropdown.hidden;
      const shouldOpen =
        force || wasOpen || document.activeElement === search;
      if (shouldOpen) {
        dropdown.hidden = false;
        search.setAttribute("aria-expanded", "true");
        positionDropdown();
      }
      // Custom optionRenderers may inject `<i data-lucide>` placeholders;
      // refresh once per render so they swap to SVGs.
      refreshLucide();
    };

    const setStatus = (state, msg) => {
      if (onStatus) onStatus(state, msg);
    };

    let pendingOps = 0;

    const applySnapshot = (snap) => {
      selected = [...snap];
      syncHidden();
      renderPills();
      renderDropdown();
      dispatchChange();
    };
    const undo = () => {
      if (!history.past.length) return false;
      history.future.push([...selected]);
      applySnapshot(history.past.pop());
      return true;
    };
    const redo = () => {
      if (!history.future.length) return false;
      history.past.push([...selected]);
      applySnapshot(history.future.pop());
      return true;
    };

    const add = async (value, option) => {
      if (selected.includes(value)) return;
      if (existingOnly && !byValue.has(value)) return;
      // Optimistic.
      if (!onAdd) pushHistory();
      selected.push(value);
      search.value = "";
      active = 0;
      syncHidden();
      renderPills();
      renderDropdown();
      dispatchChange();
      if (onAdd) {
        pendingOps++;
        setStatus("saving");
        try {
          const result = await onAdd(value, option || findOption(value));
          pendingOps--;
          if (result && result.ok === false) {
            // Roll back.
            selected = selected.filter((v) => v !== value);
            syncHidden();
            renderPills();
            renderDropdown();
            dispatchChange();
            setStatus("error", result.error || "Couldn't save");
          } else if (pendingOps === 0) {
            setStatus("saved");
          }
        } catch (e) {
          pendingOps--;
          selected = selected.filter((v) => v !== value);
          syncHidden();
          renderPills();
          renderDropdown();
          dispatchChange();
          setStatus("error", (e && e.message) || "Couldn't save");
        }
      }
    };

    const remove = async (value) => {
      if (!selected.includes(value)) return;
      const prior = [...selected];
      if (!onRemove) pushHistory();
      selected = selected.filter((v) => v !== value);
      syncHidden();
      renderPills();
      renderDropdown();
      dispatchChange();
      if (onRemove) {
        pendingOps++;
        setStatus("saving");
        try {
          const result = await onRemove(value);
          pendingOps--;
          if (result && result.ok === false) {
            selected = prior;
            syncHidden();
            renderPills();
            renderDropdown();
            dispatchChange();
            setStatus("error", result.error || "Couldn't save");
          } else if (pendingOps === 0) {
            setStatus("saved");
          }
        } catch (e) {
          pendingOps--;
          selected = prior;
          syncHidden();
          renderPills();
          renderDropdown();
          dispatchChange();
          setStatus("error", (e && e.message) || "Couldn't save");
        }
      }
    };

    const createEntry = async (text) => {
      const trimmed = normalizeCreate(text);
      if (!trimmed) return;
      if (onCreate) {
        pendingOps++;
        setStatus("saving");
        try {
          const result = await onCreate(trimmed);
          pendingOps--;
          if (!result || result.ok === false) {
            setStatus("error", (result && result.error) || "Couldn't save");
            return;
          }
          const newOption = result.option || { value: result.value, label: trimmed };
          if (!byValue.has(newOption.value)) {
            options.push(newOption);
            byValue.set(newOption.value, newOption);
          }
          selected.push(newOption.value);
          search.value = "";
          active = 0;
          syncHidden();
          renderPills();
          renderDropdown();
          dispatchChange();
          if (pendingOps === 0) setStatus("saved");
        } catch (e) {
          pendingOps--;
          setStatus("error", (e && e.message) || "Couldn't save");
        }
      } else {
        // Local-only create: treat the typed text as the value.
        const value = trimmed;
        if (selected.includes(value)) return;
        if (!byValue.has(value)) {
          const opt = { value, label: trimmed };
          options.push(opt);
          byValue.set(value, opt);
        }
        selected.push(value);
        search.value = "";
        active = 0;
        syncHidden();
        renderPills();
        renderDropdown();
        dispatchChange();
      }
    };

    // ---- events ---- //

    // Focus-preservation: clicking a pill × normally moves focus to the
    // <button> being clicked, ripping it off the input. We prevent the
    // default mousedown so focus stays where it was — if the user was
    // typing, their cursor + selection survive the click; if they
    // weren't focused on the input, we don't grab focus either.
    const onPillsMousedown = (e) => {
      if (e.target.closest("[data-remove-value]")) e.preventDefault();
    };
    const onPillsClick = (e) => {
      const btn = e.target.closest("[data-remove-value]");
      if (btn) remove(btn.dataset.removeValue);
    };
    pillsEl.addEventListener("mousedown", onPillsMousedown);
    pillsEl.addEventListener("click", onPillsClick);

    const onDropdownMousedown = (e) => {
      // mousedown (not click) so we fire before the search input's blur.
      const addBtn = e.target.closest("[data-add-value]");
      if (addBtn) {
        e.preventDefault();
        add(addBtn.dataset.addValue);
        search.focus();
        return;
      }
      const createBtn = e.target.closest("[data-create-value]");
      if (createBtn) {
        e.preventDefault();
        createEntry(createBtn.dataset.createValue);
        search.focus();
      }
    };
    dropdown.addEventListener("mousedown", onDropdownMousedown);

    const onInnerClick = (e) => {
      if (e.target.closest("button")) return;
      search.focus();
    };
    inner.addEventListener("click", onInnerClick);

    const onSearchInput = () => {
      active = 0;
      renderDropdown({ show: true });
    };
    const onSearchFocus = () => {
      renderDropdown({ show: true });
    };
    search.addEventListener("input", onSearchInput);
    search.addEventListener("focus", onSearchFocus);

    const onKeydown = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!onAdd && !onRemove) {
        // Local-only widgets support undo/redo while the search input
        // is empty — native text-input undo still works when the user
        // has typed something (we don't intercept).
        if (
          mod && (e.key === "z" || e.key === "Z") && !e.shiftKey && !search.value
        ) {
          if (undo()) e.preventDefault();
          return;
        }
        if (
          mod && !search.value &&
          (((e.key === "z" || e.key === "Z") && e.shiftKey) ||
            e.key === "y" || e.key === "Y")
        ) {
          if (redo()) e.preventDefault();
          return;
        }
      }
      const items = visibleOptions();
      if (e.key === "ArrowDown") {
        if (items.length) {
          e.preventDefault();
          active = (active + 1) % items.length;
          renderDropdown({ show: true });
        }
      } else if (e.key === "ArrowUp") {
        if (items.length) {
          e.preventDefault();
          active = (active - 1 + items.length) % items.length;
          renderDropdown({ show: true });
        }
      } else if (e.key === "Enter") {
        if (items.length && active >= 0) {
          const item = items[active];
          e.preventDefault();
          if (item.kind === "existing") add(item.option.value);
          else createEntry(item.option.value);
        } else if (allowCreate && search.value.trim()) {
          e.preventDefault();
          createEntry(search.value);
        }
      } else if (e.key === "Tab") {
        if (items.length && active >= 0) {
          e.preventDefault();
          const item = items[active];
          if (item.kind === "existing") add(item.option.value);
          else createEntry(item.option.value);
        }
      } else if (e.key === "Escape") {
        if (search.value) {
          e.preventDefault();
          search.value = "";
          renderDropdown();
        }
      } else if ((e.key === "," || (e.key === " " && cfg.commitOnSpace)) && allowCreate) {
        if (search.value.trim()) {
          e.preventDefault();
          createEntry(search.value);
        }
      } else if (e.key === "Backspace" && !search.value && selected.length) {
        e.preventDefault();
        const last = selected[selected.length - 1];
        remove(last);
      }
    };
    search.addEventListener("keydown", onKeydown);

    const onOutsideClick = (e) => {
      // Skip detached targets. Pill × and dropdown options rebuild their
      // container via renderPills() / renderDropdown() on click, which
      // means e.target is no longer in the DOM by the time this handler
      // runs — `root.contains` would then falsely report "outside" and
      // hide the dropdown while the search input still has focus.
      if (!e.target.isConnected) return;
      if (root.contains(e.target)) return;
      if (dropdown.contains(e.target)) return;
      dropdown.hidden = true;
      search.setAttribute("aria-expanded", "false");
      active = -1;
    };
    document.addEventListener("click", onOutsideClick);

    const onScroll = () => {
      if (!dropdown.hidden) positionDropdown();
    };
    const onResize = () => {
      if (!dropdown.hidden) positionDropdown();
    };
    if (hostDialog) {
      window.addEventListener("scroll", onScroll, true);
      window.addEventListener("resize", onResize);
      hostDialog.addEventListener("close", () => {
        dropdown.hidden = true;
      });
    }

    const form = root.closest("form");
    const onFormSubmit = () => {
      if (!allowCreate) return;
      if (search.value.trim()) createEntry(search.value);
    };
    if (form && allowCreate && !onCreate) {
      form.addEventListener("submit", onFormSubmit);
    }

    // Initial render.
    syncHidden();
    renderPills();

    return {
      getSelected: () => [...selected],
      setSelected: (values) => {
        selected = values.slice();
        syncHidden();
        renderPills();
        renderDropdown();
        dispatchChange();
      },
      addOption: (option) => {
        if (!byValue.has(option.value)) {
          options.push(option);
          byValue.set(option.value, option);
        }
      },
      removeOption: (value) => {
        const i = options.findIndex((o) => o.value === value);
        if (i >= 0) {
          options.splice(i, 1);
          byValue.delete(value);
        }
      },
      findOption,
      destroy: () => {
        pillsEl.removeEventListener("click", onPillsClick);
        dropdown.removeEventListener("mousedown", onDropdownMousedown);
        inner.removeEventListener("click", onInnerClick);
        search.removeEventListener("input", onSearchInput);
        search.removeEventListener("focus", onSearchFocus);
        search.removeEventListener("keydown", onKeydown);
        document.removeEventListener("click", onOutsideClick);
        if (hostDialog) {
          window.removeEventListener("scroll", onScroll, true);
          window.removeEventListener("resize", onResize);
        }
        if (form) form.removeEventListener("submit", onFormSubmit);
      },
    };
  }

  window.benchlogMountCombobox = mountCombobox;
})();
