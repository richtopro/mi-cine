/* Mi Cine — service worker
   Hace de "puente" entre el reproductor de vídeo y Google Drive:
   el <video> pide ./drive-stream/ID y aquí se reenvía a la API de Drive
   con tu sesión, respetando los saltos (Range) para poder avanzar y retroceder. */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const m = url.pathname.match(/\/drive-stream\/([\w-]+)$/);
  if (!m) return; // todo lo demás va directo a la red
  e.respondWith(stream(m[1], e.request, url.searchParams.get('t')));
});

async function stream(id, req, token) {
  const headers = { Authorization: 'Bearer ' + token };
  const range = req.headers.get('range');
  if (range) headers.Range = range;
  const r = await fetch(`https://www.googleapis.com/drive/v3/files/${id}?alt=media&supportsAllDrives=true`, { headers });
  return r;
}
