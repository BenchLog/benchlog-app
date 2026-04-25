// Owner-only links-page wiring: accordion, drag, modal triggers.
// Mounted by projects/links.html when is_owner=true.

(() => {
  const root = document.querySelector('[data-link-sections]');
  if (!root) return;
  const projectId = root.dataset.projectId;
  const linkReorderUrl = root.dataset.linkReorderUrl;
  const sectionReorderUrl = root.dataset.sectionReorderUrl;
  const sectionRenameUrlTemplate = root.dataset.sectionRenameUrlTemplate;
  const sectionCreateUrl = root.dataset.sectionCreateUrl;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  // ---------- expand/collapse ----------
  const collapsedKey = `benchlog:links:collapsed:${projectId}`;
  function loadCollapsed() {
    try {
      const raw = localStorage.getItem(collapsedKey);
      return new Set(JSON.parse(raw || '[]'));
    } catch {
      return new Set();
    }
  }
  function saveCollapsed(set) {
    localStorage.setItem(collapsedKey, JSON.stringify([...set]));
  }
  const collapsed = loadCollapsed();
  root.querySelectorAll('.link-section').forEach((sec) => {
    const id = sec.dataset.sectionId;
    if (collapsed.has(id)) sec.classList.add('is-collapsed');
  });
  root.addEventListener('click', (e) => {
    const head = e.target.closest('[data-section-toggle]');
    if (!head) return;
    if (e.target.closest('[data-section-drag]')) return;
    if (e.target.closest('[data-section-menu]')) return;
    const sec = head.closest('.link-section');
    if (!sec) return;
    const id = sec.dataset.sectionId;
    sec.classList.toggle('is-collapsed');
    if (sec.classList.contains('is-collapsed')) collapsed.add(id);
    else collapsed.delete(id);
    saveCollapsed(collapsed);
  });

  // ---------- kebab menu ----------
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-section-menu-trigger]');
    document.querySelectorAll('[data-section-menu-panel]').forEach((p) => {
      const isThis =
        trigger &&
        p === trigger.parentElement.querySelector('[data-section-menu-panel]');
      if (isThis) p.hidden = !p.hidden;
      else p.hidden = true;
    });
  });

  // ---------- reorder helpers ----------
  async function postReorder(url, body) {
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'X-CSRF-Token': csrf,
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: body.toString(),
      });
      if (!resp.ok) throw new Error(`Reorder ${resp.status}`);
    } catch (err) {
      console.error(err);
      location.reload();
    }
  }

  // Section reorder.
  if (typeof Sortable !== 'undefined' && sectionReorderUrl) {
    Sortable.create(root, {
      handle: '[data-section-drag]',
      draggable: '.link-section',
      animation: 150,
      ghostClass: 'sortable-ghost',
      onEnd: () => {
        const ids = [...root.querySelectorAll('.link-section')].map(
          (el) => el.dataset.sectionId,
        );
        const body = new URLSearchParams();
        ids.forEach((id) => body.append('section_ids', id));
        postReorder(sectionReorderUrl, body);
      },
    });
  }

  // Cross-section link drag — one Sortable per <ul.link-list>, all
  // sharing the same group so links flow between sections.
  function snapshotState() {
    const entries = [];
    root.querySelectorAll('[data-link-list]').forEach((list) => {
      const sectionId = list.dataset.sectionId;
      [...list.querySelectorAll('[data-link-id]')].forEach((el, i) => {
        entries.push({
          link_id: el.dataset.linkId,
          section_id: sectionId,
          position: i,
        });
        el.dataset.sectionId = sectionId;
      });
    });
    return entries;
  }
  function postLinkReorder() {
    const body = new URLSearchParams();
    body.set('payload', JSON.stringify(snapshotState()));
    postReorder(linkReorderUrl, body);
  }
  if (typeof Sortable !== 'undefined' && linkReorderUrl) {
    root.querySelectorAll('[data-link-list]').forEach((list) => {
      Sortable.create(list, {
        group: 'benchlog-links',
        handle: '.drag-handle',
        animation: 150,
        ghostClass: 'sortable-ghost',
        onEnd: postLinkReorder,
      });
    });
  }

  // ---------- modal triggers ----------
  document.addEventListener('click', (e) => {
    const addBtn = e.target.closest('[data-section-add-trigger]');
    if (addBtn) {
      e.preventDefault();
      document.dispatchEvent(
        new CustomEvent('benchlog:section-modal:open', {
          detail: { mode: 'create', url: sectionCreateUrl },
        }),
      );
      return;
    }
    const renameBtn = e.target.closest('[data-section-rename]');
    if (renameBtn) {
      e.preventDefault();
      const id = renameBtn.dataset.sectionId;
      const name = renameBtn.dataset.sectionName;
      const url = sectionRenameUrlTemplate.replace('SECTION_ID', id);
      document.dispatchEvent(
        new CustomEvent('benchlog:section-modal:open', {
          detail: { mode: 'rename', url, name },
        }),
      );
    }
  });

  document.addEventListener('click', (e) => {
    const addBtn = e.target.closest('[data-link-add-trigger]');
    if (addBtn) {
      e.preventDefault();
      document.dispatchEvent(
        new CustomEvent('benchlog:link-modal:open', {
          detail: {
            mode: 'create',
            sectionId: addBtn.dataset.linkSectionId || null,
            sectionName: addBtn.dataset.linkSectionName || null,
          },
        }),
      );
      return;
    }
    const editBtn = e.target.closest('[data-link-edit-trigger]');
    if (editBtn) {
      e.preventDefault();
      document.dispatchEvent(
        new CustomEvent('benchlog:link-modal:open', {
          detail: {
            mode: 'edit',
            linkId: editBtn.dataset.linkId,
            editUrl: editBtn.dataset.linkEditUrl,
            submitUrl: editBtn.dataset.linkSubmitUrl,
          },
        }),
      );
    }
  });
})();
