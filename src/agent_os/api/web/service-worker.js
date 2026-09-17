"use strict";

const SHELL_CACHE = "agent-os-shell-v3";
const SHELL_ASSETS = [
  "/app", "/assets/ceo.css", "/assets/ceo.js", "/assets/app-icon.svg", "/app.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)),
  )));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/v2/")) return;
  if (!SHELL_ASSETS.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request)));
});

self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) { payload = {}; }
  const title = String(payload.title || "Agent OS needs your attention").slice(0, 200);
  const body = String(payload.body || "Open Agent OS to review the update securely.").slice(0, 500);
  const candidate = String(payload.url || "/app#view=inbox");
  const target = candidate.startsWith("/app") ? candidate : "/app#view=inbox";
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: "/assets/app-icon.svg",
    badge: "/assets/app-icon.svg",
    tag: String(payload.tag || "agent-os-attention").slice(0, 128),
    data: {url: target},
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data?.url || "/app#view=inbox";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then((windows) => {
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (existing) {
      return existing.navigate(target).then((client) => client ? client.focus() : existing.focus());
    }
    return clients.openWindow(target);
  }));
});

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(
    (windows) => Promise.all(windows.map((client) => client.postMessage({type: "push-subscription-changed"}))),
  ));
});
