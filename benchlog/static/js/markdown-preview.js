// Live markdown preview component
document.addEventListener('alpine:init', () => {
    Alpine.data('markdownEditor', () => ({
        source: '',
        showPreview: false,

        init() {
            // Initialize source from the textarea content
            const textarea = this.$el.querySelector('textarea');
            if (textarea) {
                this.source = textarea.value;
                textarea.addEventListener('input', () => {
                    this.source = textarea.value;
                });
            }
        },

        get rendered() {
            if (!this.source || !window.markdownit) return '';
            const md = window.markdownit({
                html: false,
                linkify: true,
                typographer: true,
            });
            return md.render(this.source);
        },
    }));
});
