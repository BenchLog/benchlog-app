// Live markdown preview component
document.addEventListener('alpine:init', () => {
    Alpine.data('markdownEditor', () => ({
        source: '',
        showPreview: false,
        _md: null,

        init() {
            if (window.markdownit) {
                this._md = window.markdownit({
                    html: false,
                    linkify: true,
                    typographer: true,
                });
            }
            const textarea = this.$el.querySelector('textarea');
            if (textarea) {
                this.source = textarea.value;
                textarea.addEventListener('input', () => {
                    this.source = textarea.value;
                });
            }
        },

        get rendered() {
            if (!this.source || !this._md) return '';
            return this._md.render(this.source);
        },
    }));
});
