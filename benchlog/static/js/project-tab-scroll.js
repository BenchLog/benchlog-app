/*
 * Preserve scroll position across project tab switches.
 *
 * Clicking a tab triggers a real page load (each tab is its own URL),
 * so the browser scrolls to the top by default. When a user is reading
 * deep into a journal page and switches to Files, that's a context loss.
 * This script saves window.scrollY when a project tab link is clicked
 * and restores it on the destination page if the load is the click's
 * target and recent. sessionStorage scopes the entry to the current
 * browser tab/window so multiple BenchLog tabs don't fight.
 *
 * Scoped to <nav data-project-tabs> so non-tab links (header breadcrumbs,
 * footer, modals) don't save anything. New-window clicks (meta/ctrl/etc.)
 * are skipped because the current page isn't actually navigating.
 */
(() => {
  "use strict";

  const KEY = "benchlog:tabs:scroll";
  // Restore only if the save is recent — guards against a stale entry
  // surviving an unrelated navigation that happens to land on the same
  // URL later.
  const TTL_MS = 5000;

  const nav = document.querySelector("[data-project-tabs]");
  if (nav) {
    nav.addEventListener("click", (e) => {
      const link = e.target.closest("a[href]");
      if (!link) return;
      // Only intercept plain left-clicks. Modifier-clicks open in a new
      // tab, so the current page isn't navigating and saving scrollY
      // would just confuse the next legit click.
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      try {
        sessionStorage.setItem(KEY, JSON.stringify({
          target: link.href,
          y: window.scrollY,
          ts: Date.now(),
        }));
      } catch (_) {
        // sessionStorage disabled / full — degrade silently to default
        // browser scroll-to-top behavior.
      }
    });
  }

  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (!saved || typeof saved.y !== "number") {
      sessionStorage.removeItem(KEY);
      return;
    }
    if (saved.target !== window.location.href) return;
    if (Date.now() - saved.ts > TTL_MS) {
      sessionStorage.removeItem(KEY);
      return;
    }
    // One-shot: clear before restoring so a refresh doesn't re-jump.
    sessionStorage.removeItem(KEY);
    // requestAnimationFrame so the layout is computed before we scroll;
    // `behavior: "instant"` avoids the smooth-scroll jolt on first paint.
    requestAnimationFrame(() => {
      window.scrollTo({ top: saved.y, behavior: "instant" });
    });
  } catch (_) {
    // Malformed entry — drop it and bail.
    try { sessionStorage.removeItem(KEY); } catch (_) {}
  }
})();
