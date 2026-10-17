// App shell is cached so the app opens instantly and offline.
// listings.json is always fetched fresh by the page (with a localStorage fallback).
const CACHE = "aos-bargains-v6";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png", "icons/badge-96.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.endsWith("listings.json")) return; // page handles this itself

  // Network first so app updates show up, cache as fallback when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("index.html")))
  );
});

// ---------- Push notifications from the watcher ----------
self.addEventListener("push", (e) => {
  let msg = {};
  try { msg = e.data ? e.data.json() : {}; } catch { msg = { body: e.data && e.data.text() }; }
  const opts = {
    body: msg.body || "",
    icon: "icons/icon-192.png",
    badge: "icons/badge-96.png",           // small monochrome icon in the Android status bar
    tag: msg.tag || undefined,
    renotify: !!msg.tag,
    data: { url: msg.url || "" },
  };
  if (msg.image) opts.image = msg.image;  // big picture on Android
  e.waitUntil(Promise.all([
    self.registration.showNotification(msg.title || "AoS Bargains", opts),
    self.navigator.setAppBadge ? self.navigator.setAppBadge().catch(() => {}) : null,
  ]));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = e.notification.data && e.notification.data.url;
  const appUrl = self.registration.scope;
  e.waitUntil((async () => {
    if (target) return clients.openWindow(target);            // straight to the Tradera listing
    const wins = await clients.matchAll({ type: "window", includeUncontrolled: true });
    const app = wins.find((w) => w.url.startsWith(appUrl));
    return app ? app.focus() : clients.openWindow(appUrl);
  })());
});
