const RELOADED_AT = "shoal.staleReloadAt";
/** Don't reload again within this window, so a chunk that's really missing shows an error instead of looping. */
const COOLDOWN_MS = 10_000;

/** True for the errors a browser raises when a lazily-loaded chunk can't be fetched. */
export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/i.test(
    message,
  );
}

/**
 * Reload once to pick up the current build after a redeploy removed the chunks this page was built
 * against. Returns false (and does nothing) if it already reloaded moments ago.
 */
let reloading = false;

export function reloadForNewBuild(now = Date.now()): boolean {
  if (reloading) return true;
  let last = 0;
  try {
    last = Number(sessionStorage.getItem(RELOADED_AT) ?? 0);
    if (now - last < COOLDOWN_MS) return false;
    sessionStorage.setItem(RELOADED_AT, String(now));
  } catch {
    /* no storage: still reload, the server now sends a fresh index.html */
  }
  reloading = true;
  window.location.reload();
  return true;
}

/**
 * Vite fires this when a dynamic import or its preloads fail. The error still propagates, so the
 * route's error page sees it too, shows a spinner while the reload happens, or the error if it can't.
 */
export function recoverFromStaleChunks() {
  window.addEventListener("vite:preloadError", () => reloadForNewBuild());
}
