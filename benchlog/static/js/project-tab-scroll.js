/*
 * Auto-scroll the tab bar to the top of the viewport on tab switches.
 *
 * Clicking a tab triggers a real page load (each tab is its own URL),
 * so the browser would otherwise scroll to the top, leaving the cover
 * image / project header taking up most of the screen. For a user who
 * just chose a tab, the content under that tab is what they came for —
 * not the banner they already saw. So when the destination page loads,
 * scroll down until the sticky tab bar pins to the top. If the page is
 * too short to scroll that far, the browser clamps to the max scroll
 * automatically (which is fine — the tab bar is already on screen).
 *
 * A sessionStorage hand-off (set on click, read on next load) limits
 * this behavior to actual tab-switch navigations: a fresh visit, a
 * back-button, or a refresh land at the natural top.
 *
 * Scoped to <nav data-project-tabs> so non-tab links (header breadcrumbs,
 * footer, modals) don't trigger the hand-off. New-window clicks
 * (meta/ctrl/etc.) are skipped because the current page isn't actually
 * navigating.
 */
(() => {
  "use strict";

  const KEY = "benchlog:tabs:scroll";
  // Restore only if the hand-off is recent — guards against a stale
  // entry surviving an unrelated navigation that happens to land on the
  // same URL later.
  const TTL_MS = 5000;

  const nav = document.querySelector("[data-project-tabs]");
  if (nav) {
    nav.addEventListener("click", (e) => {
      const link = e.target.closest("a[href]");
      if (!link) return;
      // Only intercept plain left-clicks. Modifier-clicks open in a new
      // tab, so the current page isn't navigating and the hand-off
      // would just confuse the next legit click.
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      try {
        sessionStorage.setItem(KEY, JSON.stringify({
          target: link.href,
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
    if (!saved || typeof saved.target !== "string") {
      sessionStorage.removeItem(KEY);
      return;
    }
    if (saved.target !== window.location.href) return;
    if (Date.now() - saved.ts > TTL_MS) {
      sessionStorage.removeItem(KEY);
      return;
    }
    // One-shot: clear before scrolling so a refresh doesn't re-jump.
    sessionStorage.removeItem(KEY);
    // requestAnimationFrame so the layout is computed before we read
    // the tab bar's position; `behavior: "instant"` avoids a smooth-
    // scroll jolt on first paint.
    requestAnimationFrame(() => {
      const targetNav = document.querySelector("[data-project-tabs]");
      if (!targetNav) return;
      const y = targetNav.getBoundingClientRect().top + window.scrollY;
      window.scrollTo({ top: y, behavior: "instant" });
    });
  } catch (_) {
    // Malformed entry — drop it and bail.
    try { sessionStorage.removeItem(KEY); } catch (_) {}
  }
})();
