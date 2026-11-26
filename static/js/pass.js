/* Refresh the short-lived check-in pass before it expires.
 *
 * The image endpoint returns a freshly signed, meal-bound token and is marked
 * no-store, so a cache-busting query is enough. No libraries, no network calls
 * beyond this origin.
 */
(() => {
  const image = document.querySelector("[data-pass-qr]");
  if (!image) return;
  const ttl = Number(image.dataset.refresh || "90");
  if (!Number.isFinite(ttl) || ttl <= 0) return;
  const refreshEvery = Math.max(30, ttl - 15) * 1000;

  const refresh = () => {
    const url = new URL(image.src, window.location.origin);
    url.searchParams.set("t", String(Date.now()));
    image.src = url.toString();
  };
  window.setInterval(refresh, refreshEvery);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
})();
