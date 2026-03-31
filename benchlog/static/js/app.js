// Keyboard shortcuts
document.addEventListener('keydown', (event) => {
    // Ignore when typing in inputs
    if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA' || event.target.tagName === 'SELECT') return;

    if (event.key === '/' || event.key === 's') {
        // Focus search — "/" or "s"
        event.preventDefault();
        window.location.href = '/search';
    } else if (event.key === 'n' && !event.metaKey && !event.ctrlKey) {
        // New project — "n"
        event.preventDefault();
        window.location.href = '/projects/new';
    } else if (event.key === 'h' && !event.metaKey && !event.ctrlKey) {
        // Home — "h"
        event.preventDefault();
        window.location.href = '/';
    }
});

// Paste-to-upload images in markdown textareas
document.addEventListener('paste', async (event) => {
    const textarea = event.target;
    if (textarea.tagName !== 'TEXTAREA') return;

    const items = event.clipboardData?.items;
    if (!items) return;

    for (const item of items) {
        if (!item.type.startsWith('image/')) continue;

        event.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;

        const formData = new FormData();
        formData.append('file', file, file.name || 'pasted-image.png');

        // Insert placeholder
        const start = textarea.selectionStart;
        const placeholder = '![Uploading...]()';
        textarea.setRangeText(placeholder, start, textarea.selectionEnd, 'end');

        try {
            const resp = await fetch('/images/upload', { method: 'POST', body: formData });
            const data = await resp.json();

            if (data.markdown) {
                textarea.value = textarea.value.replace(placeholder, data.markdown);
            }
        } catch {
            textarea.value = textarea.value.replace(placeholder, '![Upload failed]()');
        }
    }
});

// Reset forms after successful HTMX requests
document.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.successful && event.detail.elt.tagName === 'FORM') {
        event.detail.elt.reset();
    }
});

document.addEventListener('alpine:init', () => {
    Alpine.data('fileUpload', (slug, currentPath) => ({
        dragging: false,
        uploading: false,
        progress: 0,
        statusText: '',

        async uploadFiles(files) {
            if (!files.length) return;

            this.uploading = true;
            this.progress = 0;
            this.statusText = `Uploading ${files.length} file(s)...`;

            const formData = new FormData();
            formData.append('path', currentPath);
            for (const file of files) {
                formData.append('files', file);
            }

            try {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', `/projects/${slug}/files/upload`);
                xhr.setRequestHeader('HX-Request', 'true');

                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) {
                        this.progress = Math.round((e.loaded / e.total) * 100);
                        this.statusText = `Uploading... ${this.progress}%`;
                    }
                });

                xhr.addEventListener('load', () => {
                    if (xhr.status === 200) {
                        const redirect = xhr.getResponseHeader('HX-Redirect');
                        if (redirect) {
                            window.location.href = redirect;
                        } else {
                            window.location.reload();
                        }
                    } else {
                        this.statusText = 'Upload failed.';
                        this.uploading = false;
                    }
                });

                xhr.addEventListener('error', () => {
                    this.statusText = 'Upload failed.';
                    this.uploading = false;
                });

                xhr.send(formData);
            } catch (err) {
                this.statusText = 'Upload failed.';
                this.uploading = false;
            }
        },

        handleDrop(event) {
            this.dragging = false;
            this.uploadFiles(event.dataTransfer.files);
        },

        handleSelect(event) {
            this.uploadFiles(event.target.files);
        },
    }));
});
