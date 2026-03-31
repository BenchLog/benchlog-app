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
