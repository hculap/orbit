// Service worker — push notifications + stale-while-revalidate cache.
//
// Served by FastAPI at the origin root (/sw.js) so its scope covers
// everything. The build SHA is injected at serve time via the
// `__SW_VERSION__` placeholder below — each deploy produces byte-different
// content, which is what triggers the browser to install the new SW and
// fire our `update_available` postMessage.
//
// Cache strategy:
//   - /static/*, /, /api/version, app shell -> stale-while-revalidate.
//     Cached response wins (instant render); background fetch refreshes
//     the cache for next time and notifies the client when the bytes
//     actually changed.
//   - /api/* (everything else)              -> pass-through. Live data
//     must stay live.
//   - /share/preview/*, /api/orchestrator/uploads/* -> pass-through.
//     Binary, user-mutable.
//
// Push + notificationclick handlers are preserved unchanged so existing
// subscriptions keep working when the SW migrates from /static/sw.js to
// /sw.js (one fresh subscription on first new SW activation).

const SW_VERSION = '__SW_VERSION__';
const CACHE_NAME = 'hub-cache-' + SW_VERSION;

// URLs that benefit from stale-while-revalidate. We never cache POST/PATCH
// (those mutate state) and we never cache anything under /api/ except
// /api/version (used by the toast logic to detect new builds out-of-band).
function isCacheable(request, url) {
  if (request.method !== 'GET') return false;
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith('/static/')) return true;
  if (url.pathname === '/' || url.pathname === '/sw.js') return true;
  if (url.pathname === '/api/version') return true;
  return false;
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const networkPromise = fetch(request).then(async (response) => {
    // Don't cache opaque or error responses. 304 is handled by the browser
    // before we see it (returns the cached body with refreshed headers).
    if (response && response.ok && response.type === 'basic') {
      try {
        await cache.put(request, response.clone());
        // If the bytes actually differ from what we returned to the page
        // (above), tell open clients so the toast can light up.
        if (cached) {
          const [newText, oldText] = await Promise.all([
            response.clone().text(),
            cached.clone().text(),
          ]).catch(() => [null, null]);
          if (newText !== null && oldText !== null && newText !== oldText) {
            broadcastUpdateAvailable();
          }
        }
      } catch (e) { /* opaque body or quota error — skip cache write */ }
    }
    return response;
  }).catch((err) => {
    // Network failed; if we have nothing cached, propagate the error so
    // the caller's normal fetch handler kicks in (404/offline UI etc.).
    if (cached) return cached;
    throw err;
  });
  // Cached wins for latency; the network promise still resolves in the
  // background and updates the cache for the NEXT load.
  return cached || networkPromise;
}

async function broadcastUpdateAvailable() {
  const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of clients) {
    client.postMessage({ type: 'update_available', version: SW_VERSION });
  }
}

// ----------------------------------------------------------------------
// Lifecycle: install → activate → fetch
// ----------------------------------------------------------------------

self.addEventListener('install', (event) => {
  // Activate as soon as install completes — no waiting room. We don't
  // pre-cache anything explicitly; the SW fills its cache lazily on the
  // first fetch of each asset (the page load itself).
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Purge caches from previous builds. We keep only the cache matching
    // SW_VERSION so a deploy reliably evicts stale assets.
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter(k => k.startsWith('hub-cache-') && k !== CACHE_NAME)
        .map(k => caches.delete(k)),
    );
    await self.clients.claim();
    // Tell any already-open tabs that a new version just took control.
    broadcastUpdateAvailable();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  let url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (!isCacheable(request, url)) return;  // pass-through
  event.respondWith(staleWhileRevalidate(request));
});

// ----------------------------------------------------------------------
// Push notifications — original behaviour, untouched.
// ----------------------------------------------------------------------

const IDB_NAME = 'hub';
const IDB_STORE = 'state';
const SUPPRESS_GRACE_MS = 5000; // last visible-focus window during which we don't notify

function openIdb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(IDB_STORE)) {
        db.createObjectStore(IDB_STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function readUiState() {
  try {
    const db = await openIdb();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readonly');
      const req = tx.objectStore(IDB_STORE).get('ui-state');
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    return null;
  }
}

// Resolve a path relative to this SW's registration scope so subpath deploys
// (e.g. nginx mounted at /dashboard) still load icons correctly.
function scopedUrl(path) {
  try {
    return new URL(path, self.registration.scope).toString();
  } catch (e) {
    return path;
  }
}

// (install + activate are registered at the top of the file — caching
// version owns the lifecycle and also handles skipWaiting + clients.claim.)

self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let data = {};
    try {
      data = event.data ? event.data.json() : {};
    } catch (e) {
      data = {};
    }
    const title = data.title || 'Orchestrator';
    const body = data.body || '';
    const sessionId = data.data && data.data.session_id;

    // Suppress if user is actively viewing the orchestrator section.
    const state = await readUiState();
    if (
      state &&
      state.section === 'orchestrator' &&
      state.visible &&
      Date.now() - (state.lastFocusTs || 0) < SUPPRESS_GRACE_MS
    ) {
      return; // silent
    }

    await self.registration.showNotification(title, {
      body,
      icon: scopedUrl('icon-192.png'),
      badge: scopedUrl('icon-192.png'),
      data: { session_id: sessionId },
      tag: sessionId ? 'orch-' + sessionId : 'orch',
      renotify: true,
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const sessionId = event.notification.data && event.notification.data.session_id;
    const allClients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    for (const client of allClients) {
      if ('focus' in client) {
        try {
          await client.focus();
        } catch (e) { /* focus may reject */ }
        client.postMessage({ type: 'notification-click', session_id: sessionId });
        return;
      }
    }
    if (self.clients.openWindow) {
      // Open a fresh window at the app root (one level up from the SW scope,
      // which is /static/). The app reads ?session=<id> on load.
      const root = scopedUrl('../');
      const url = sessionId
        ? root + '?session=' + encodeURIComponent(sessionId)
        : root;
      await self.clients.openWindow(url);
    }
  })());
});
