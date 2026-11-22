// 仅刷新留声自己的界面缓存；不触碰 Cookie、IndexedDB 和播放队列。
export async function refreshPwaCache() {
  if (!navigator.onLine) throw new Error('当前离线，请联网后更新缓存')
  const health = await fetch('/api/health', {cache:'no-store',credentials:'same-origin'})
  if (!health.ok) throw new Error('服务暂不可用，已保留现有缓存')
  if (!window.isSecureContext || !('serviceWorker' in navigator)) {
    const response=await fetch('/',{cache:'reload'})
    if(!response.ok) throw new Error('页面更新失败，请稍后重试')
    return
  }
  const registration=await navigator.serviceWorker.getRegistration('/') || await navigator.serviceWorker.register('/sw.js')
  await registration.update()
  const deadline=Date.now()+25000
  while(registration.installing && Date.now()<deadline) await new Promise(r=>setTimeout(r,150))
  if(registration.installing) throw new Error('新版本仍在下载，请稍后再试；现有缓存已保留')
  if(registration.waiting) registration.waiting.postMessage({type:'ACTIVATE_UPDATE'})
  while((registration.waiting || !registration.active || registration.active.state!=='activated') && Date.now()<deadline) await new Promise(r=>setTimeout(r,150))
  if(!registration.active || registration.active.state!=='activated' || registration.waiting) throw new Error('更新尚未激活，请稍后再试')
  await new Promise<void>((resolve,reject)=>{
    const channel=new MessageChannel()
    const timer=setTimeout(()=>{channel.port1.close();reject(new Error('缓存更新未确认，请稍后重试'))},20000)
    channel.port1.onmessage=event=>{clearTimeout(timer);channel.port1.close();event.data?.ok?resolve():reject(new Error('缓存下载失败，已保留原缓存'))}
    registration.active!.postMessage({type:'REFRESH_SHELL'},[channel.port2])
  })
}
