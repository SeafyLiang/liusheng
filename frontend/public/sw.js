const CACHE = 'liusheng-shell-__BUILD_ID__';
const SHELL = ['/','/manifest.webmanifest','/icon.svg','/icon-192.png','/icon-512.png'];
async function refreshShell(){const cache=await caches.open(CACHE);await cache.addAll(SHELL.map(url=>new Request(url,{cache:'reload'})));}
self.addEventListener('install', event => event.waitUntil(refreshShell()));
self.addEventListener('message',event=>{
 if(event.data?.type==='ACTIVATE_UPDATE') event.waitUntil(self.skipWaiting());
 if(event.data?.type==='REFRESH_SHELL') event.waitUntil(refreshShell().then(()=>event.ports[0]?.postMessage({ok:true})).catch(()=>event.ports[0]?.postMessage({ok:false})));
});
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k=>k.startsWith('liusheng-shell-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch', event => {
  const u = new URL(event.request.url);
  if(event.request.method!=='GET'||u.origin!==self.location.origin||u.pathname.startsWith('/api/')) return;
  if(event.request.mode==='navigate') {
    event.respondWith(fetch(event.request).catch(()=>caches.match('/')));return;
  }
  if(u.pathname.startsWith('/assets/')||u.pathname.startsWith('/icon')) event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request).then(response=>{if(response.ok){const clone=response.clone();caches.open(CACHE).then(c=>c.put(event.request,clone));}return response;})));
});
