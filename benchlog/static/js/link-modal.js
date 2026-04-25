// Owner-only modal wiring: link create/edit + section create/rename.
// Listens for `benchlog:link-modal:open` and `benchlog:section-modal:open`
// custom events dispatched by links-page.js.

(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const root = document.querySelector('[data-link-sections]');
  if (!root) return;
  const fetchMetadataUrl = root.dataset.linkFetchMetadataUrl;
  const linkCreateUrl = root.dataset.linkCreateUrl;

  // ---------- link modal ----------
  const linkModal = document.querySelector('[data-link-modal]');
  if (!linkModal) return;
  const linkForm = linkModal.querySelector('[data-link-form]');
  const linkTitle = linkModal.querySelector('[data-link-modal-title]');
  const linkSubmitLabel = linkModal.querySelector('[data-link-submit-label]');
  const urlEl = linkForm.querySelector('[data-link-url]');
  const titleEl = linkForm.querySelector('[data-link-title]');
  const noteEl = linkForm.querySelector('[data-link-note]');
  const noteCount = linkForm.querySelector('[data-link-note-count]');
  const errEl = linkForm.querySelector('[data-link-error]');
  const previewEl = linkForm.querySelector('[data-link-preview]');
  const previewThumb = linkForm.querySelector('[data-link-preview-thumb]');
  const previewSiteName = linkForm.querySelector('[data-link-preview-site-name]');
  const previewTitle = linkForm.querySelector('[data-link-preview-title]');
  const previewDesc = linkForm.querySelector('[data-link-preview-desc]');
  const previewWarning = linkForm.querySelector('[data-link-preview-warning]');
  const previewRefresh = linkForm.querySelector('[data-link-preview-refresh]');
  const ogTitle = linkForm.querySelector('[data-link-og-title]');
  const ogDesc = linkForm.querySelector('[data-link-og-description]');
  const ogImage = linkForm.querySelector('[data-link-og-image]');
  const ogSite = linkForm.querySelector('[data-link-og-site]');
  const ogFavicon = linkForm.querySelector('[data-link-og-favicon]');
  const sectionCb = linkForm.querySelector('#link-section-cb');

  let submitUrl = linkCreateUrl;
  let inflightFetch = null;

  function showLinkError(msg) {
    errEl.textContent = msg;
    errEl.hidden = false;
  }
  function clearPreview() {
    previewEl.hidden = true;
    previewThumb.innerHTML = '';
    previewSiteName.textContent = '';
    previewTitle.textContent = '';
    previewDesc.textContent = '';
    previewWarning.hidden = true;
    ogTitle.value = '';
    ogDesc.value = '';
    ogImage.value = '';
    ogSite.value = '';
    ogFavicon.value = '';
  }
  function applyPreview(md) {
    previewEl.hidden = false;
    if (md.image_url) {
      previewThumb.innerHTML = `<img src="${md.image_url}" alt="" loading="lazy">`;
    } else {
      previewThumb.innerHTML =
        '<i data-lucide="image-off" class="w-5 h-5 text-ink-muted"></i>';
      window.lucide?.createIcons?.();
    }
    previewSiteName.textContent = md.site_name || '';
    previewTitle.textContent = md.title || '';
    previewDesc.textContent = md.description || '';
    if (md.warning) {
      previewWarning.textContent = md.warning;
      previewWarning.hidden = false;
    } else {
      previewWarning.hidden = true;
    }
    ogTitle.value = md.title || '';
    ogDesc.value = md.description || '';
    ogImage.value = md.image_url || '';
    ogSite.value = md.site_name || '';
    ogFavicon.value = md.favicon_url || '';
    // Auto-fill title only when empty.
    if (!titleEl.value && md.title) titleEl.value = md.title;
  }
  async function fetchAndPreview(url) {
    if (!url) {
      clearPreview();
      return;
    }
    const body = new URLSearchParams();
    body.set('_csrf', csrf);
    body.set('url', url);
    if (inflightFetch) inflightFetch.abort?.();
    const ctrl = new AbortController();
    inflightFetch = ctrl;
    try {
      const resp = await fetch(fetchMetadataUrl, {
        method: 'POST',
        headers: {
          'X-CSRF-Token': csrf,
          'Content-Type': 'application/x-www-form-urlencoded',
          Accept: 'application/json',
        },
        body: body.toString(),
        signal: ctrl.signal,
      });
      if (!resp.ok) {
        applyPreview({
          title: null,
          description: null,
          image_url: null,
          site_name: null,
          favicon_url: null,
          warning: `Couldn't load preview (HTTP ${resp.status}).`,
        });
        return;
      }
      const md = await resp.json();
      applyPreview(md);
    } catch (err) {
      if (err.name === 'AbortError') return;
      applyPreview({
        title: null,
        description: null,
        image_url: null,
        site_name: null,
        favicon_url: null,
        warning: "Couldn't load preview.",
      });
    } finally {
      if (inflightFetch === ctrl) inflightFetch = null;
    }
  }

  urlEl.addEventListener('blur', () => fetchAndPreview(urlEl.value.trim()));
  previewRefresh.addEventListener('click', () =>
    fetchAndPreview(urlEl.value.trim()),
  );

  function updateNoteCount() {
    noteCount.textContent = String((noteEl.value || '').length);
  }
  noteEl.addEventListener('input', updateNoteCount);

  // Section combobox mount. The combobox is multi-select by default;
  // we want exactly one section per link, so we enforce single-select
  // by trimming the selected list down to its most recent entry on
  // every change. The `enforcing` guard prevents the recursive event
  // that setSelected dispatches from re-triggering us.
  let sectionHandle = null;
  if (sectionCb && window.benchlogMountCombobox) {
    const cfgEl = sectionCb.querySelector('[data-combobox-config]');
    if (cfgEl) {
      const cfg = JSON.parse(cfgEl.textContent);
      cfg.matchFn = (option, q) =>
        option.label.toLowerCase().includes(q.toLowerCase());
      cfg.normalizeCreate = (s) => (s || '').trim();
      cfg.optionRenderer = (option, container, isCreate) => {
        container.textContent = isCreate
          ? `Create "${option.label}"`
          : option.label;
      };
      cfg.pillRenderer = (option, container) => {
        const span = document.createElement('span');
        span.textContent = option.label;
        container.appendChild(span);
      };
      sectionHandle = window.benchlogMountCombobox(sectionCb, cfg);

      // Single-select UX: once a section is picked, hide the search
      // input. The user removes the current pill (via the × on it) to
      // pick a different one. This avoids the "I picked one and the
      // dropdown stayed open inviting me to pick another" confusion.
      const searchEl = sectionCb.querySelector('[data-combobox-search]');
      const dropdownEl = sectionCb.querySelector('[data-combobox-dropdown]');
      const updateSingleSelectUI = () => {
        const count = sectionHandle.getSelected().length;
        if (searchEl) searchEl.hidden = count > 0;
        if (dropdownEl && count > 0) dropdownEl.hidden = true;
      };
      sectionCb.addEventListener('combobox-change', updateSingleSelectUI);
      updateSingleSelectUI();
    }
  }

  function setSectionValue(name) {
    if (!sectionHandle) return;
    if (!name) {
      sectionHandle.setSelected([]);
      return;
    }
    // If the section isn't already in the catalog (e.g., the page
    // loaded with sections X, Y; the user is editing a link belonging
    // to section Z that exists but happens to have been excluded —
    // unlikely in practice, but cheap insurance), add it on the fly so
    // the pill renders.
    if (!sectionHandle.findOption(name)) {
      sectionHandle.addOption({ value: name, label: name });
    }
    sectionHandle.setSelected([name]);
  }

  function openLink(detail) {
    errEl.hidden = true;
    clearPreview();
    if (detail.mode === 'edit') {
      linkTitle.textContent = 'Edit link';
      linkSubmitLabel.textContent = 'Save changes';
      submitUrl = detail.submitUrl;
      fetch(detail.editUrl, {
        headers: { Accept: 'application/json' },
      })
        .then((r) => r.json())
        .then((data) => {
          urlEl.value = data.url || '';
          titleEl.value = data.title || '';
          noteEl.value = data.note || '';
          updateNoteCount();
          setSectionValue(data.section_name || '');
          applyPreview({
            title: data.og_title,
            description: data.og_description,
            image_url: data.og_image_url,
            site_name: data.og_site_name,
            favicon_url: data.favicon_url,
            warning: null,
          });
        })
        .catch(() => showLinkError('Could not load link.'));
    } else {
      linkTitle.textContent = 'Add link';
      linkSubmitLabel.textContent = 'Save link';
      submitUrl = linkCreateUrl;
      urlEl.value = '';
      titleEl.value = '';
      noteEl.value = '';
      updateNoteCount();
      setSectionValue(detail.sectionName || '');
    }
    linkModal.showModal();
    requestAnimationFrame(() => urlEl.focus());
  }

  document.addEventListener('benchlog:link-modal:open', (e) =>
    openLink(e.detail || {}),
  );
  linkModal.querySelectorAll('[data-link-modal-cancel]').forEach((b) =>
    b.addEventListener('click', () => linkModal.close()),
  );

  linkForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    errEl.hidden = true;
    const data = new FormData(linkForm);
    data.set('_csrf', csrf);
    try {
      const resp = await fetch(submitUrl, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf, Accept: 'application/json' },
        body: data,
      });
      if (resp.status === 302 || resp.redirected || resp.ok) {
        location.reload();
        return;
      }
      let msg = `Save failed (${resp.status}).`;
      try {
        const payload = await resp.json();
        if (payload && payload.detail) msg = payload.detail;
      } catch {}
      showLinkError(msg);
    } catch {
      showLinkError('Save failed — check your connection.');
    }
  });

  // ---------- section modal ----------
  const sectionModal = document.querySelector('[data-section-modal]');
  if (!sectionModal) return;
  const sectionForm = sectionModal.querySelector('[data-section-form]');
  const sectionTitle = sectionModal.querySelector('[data-section-modal-title]');
  const sectionSubmitLabel = sectionModal.querySelector(
    '[data-section-submit-label]',
  );
  const sectionNameEl = sectionModal.querySelector('[data-section-name]');
  const sectionErr = sectionModal.querySelector('[data-section-error]');
  let sectionSubmitUrl = '';

  function showSectionError(msg) {
    sectionErr.textContent = msg;
    sectionErr.hidden = false;
  }
  document.addEventListener('benchlog:section-modal:open', (e) => {
    const d = e.detail || {};
    sectionErr.hidden = true;
    sectionSubmitUrl = d.url;
    if (d.mode === 'rename') {
      sectionTitle.textContent = 'Rename section';
      sectionSubmitLabel.textContent = 'Save';
      sectionNameEl.value = d.name || '';
    } else {
      sectionTitle.textContent = 'New section';
      sectionSubmitLabel.textContent = 'Create';
      sectionNameEl.value = '';
    }
    sectionModal.showModal();
    requestAnimationFrame(() => sectionNameEl.focus());
  });
  sectionModal.querySelectorAll('[data-section-modal-cancel]').forEach((b) =>
    b.addEventListener('click', () => sectionModal.close()),
  );
  sectionForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    sectionErr.hidden = true;
    const body = new URLSearchParams();
    body.set('_csrf', csrf);
    body.set('name', sectionNameEl.value.trim());
    try {
      const resp = await fetch(sectionSubmitUrl, {
        method: 'POST',
        headers: {
          'X-CSRF-Token': csrf,
          'Content-Type': 'application/x-www-form-urlencoded',
          Accept: 'application/json',
        },
        body: body.toString(),
      });
      if (resp.status === 302 || resp.redirected || resp.ok) {
        location.reload();
        return;
      }
      let msg = `Save failed (${resp.status}).`;
      try {
        const payload = await resp.json();
        if (payload && payload.detail) msg = payload.detail;
      } catch {}
      showSectionError(msg);
    } catch {
      showSectionError('Save failed — check your connection.');
    }
  });
})();
