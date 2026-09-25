/* Power Sentinel service worker.

   STRATEGY, and why it is not the usual cache-first:

   This is a power dashboard. Stale is not a minor annoyance here -- showing
   yesterday's bundle during an outage is the failure mode the whole app
   exists to prevent. The server is also ~50 ms away over LAN/tailnet, so
   network-first costs almost nothing.

     app shell (html/css/js)  NETWORK-FIRST with a 3.5 s timeout, cache as
                              the offline fallback. Online you always get the
                              current build, so a deploy takes effect on the
                              next load with no version bumping at all.
     immutable assets         CACHE-FIRST (icons, vendored uPlot). These only
                              change when their filename does.
     /api/*                   NEVER TOUCHED. Not cached, not intercepted.

   BUILD_STAMP is rewritten by deploy-web.sh on every deploy. That guarantees
   this file differs byte-for-byte each time, which is what makes the browser
   notice a new worker and install it. Without it a changed app but unchanged
   sw.js would leave old clients on the old worker indefinitely. */

const BUILD_STAMP = "__BUILD_STAMP__";
const CACHE = "power-sentinel-" + BUILD_STAMP;
const NET_TIMEOUT = 3500;

const SHELL = [
  "./",
  "index.html",
  "manifest.webmanifest",
  "css/tokens.css",
  "css/layout.css",
  "css/components.css",
  "css/controls.css",
  "css/pending.css",
  "css/log.css",
  "js/app.js",
  "js/api.js",
  "js/format.js",
  "js/now.js",
  "js/timeline.js",
  "js/spark.js",
  "js/history.js",
  "js/powerlog.js",
  "js/health.js",
  "js/readiness.js",
  "js/config.js",
  "js/configdefs.js",
  "js/pending.js",
  "js/catalog.js",
  "js/applychanges.js",
  "js/confirm.js",
  "js/savebar.js",
  "js/cfgdom.js",
  "js/parkconfig.js",
  "js/upsconfig.js",
  "js/resetcfg.js",
  "js/controls.js",
  "js/upsoff.js",
  "js/pwa.js",
];

const IMMUTABLE = [
  "vendor/uPlot.iife.min.js",
  "vendor/uPlot.min.css",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "icons/icon-512-maskable.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Per-asset catch: one missing file must never fail the whole install and
    // leave the app with no offline copy at all.
    await Promise.all(SHELL.concat(IMMUTABLE).map((url) =>
      cache.add(new Request(url, { cache: "reload" })).catch(() => null)));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((k) => k.startsWith("power-sentinel-") && k !== CACHE)
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

function isImmutable(pathname) {
  return IMMUTABLE.some((a) => pathname.endsWith(a.replace("./", "")));
}

async function cacheFirst(request) {
  const hit = await caches.match(request, { ignoreSearch: false });
  if (hit) return hit;
  const res = await fetch(request);
  if (res && res.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, res.clone());
  }
  return res;
}

async function networkFirst(request) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), NET_TIMEOUT);
  try {
    const res = await fetch(request, { signal: controller.signal });
    clearTimeout(timer);
    if (res && res.ok) {
      const cache = await caches.open(CACHE);
      cache.put(request, res.clone());
    }
    return res;
  } catch (err) {
    clearTimeout(timer);
    const hit = await caches.match(request, { ignoreSearch: false });
    if (hit) return hit;
    // A navigation with nothing cached: hand back the shell rather than the
    // browser's dinosaur, so the app can render its own offline banner.
    if (request.mode === "navigate") {
      const shell = await caches.match("index.html");
      if (shell) return shell;
    }
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  /* Live power data must NEVER be cached or intercepted -- a stale reading
     served during an outage is the worst possible failure for this app.
     Checked first, by path substring so it holds under any reverse-proxy
     prefix, and before the method check so it also covers the PUT/POST
     control and config calls. Returning without event.respondWith() hands
     the request straight to the network, untouched. */
  if (url.pathname.includes("/api/")) return;
  if (event.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;

  if (isImmutable(url.pathname)) {
    event.respondWith(cacheFirst(event.request));
    return;
  }
  event.respondWith(networkFirst(event.request));
});

// Lets the page ask the waiting worker to take over immediately.
self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});
