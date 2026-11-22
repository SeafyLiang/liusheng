let csrf = ''
export function setCsrf(value: string) { csrf = value }
export async function api(path: string, method = 'GET', body?: unknown) {
  const form = body instanceof FormData
  const res = await fetch('/api' + path, {method, credentials: 'same-origin', cache: 'no-store',
    headers: {...(form ? {} : {'Content-Type': 'application/json'}), ...(method !== 'GET' ? {'X-CSRF-Token': csrf} : {})},
    body: body === undefined ? undefined : form ? body : JSON.stringify(body)})
  const json = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401 && path !== '/login') window.dispatchEvent(new Event('session-expired'))
    throw new Error(typeof json.detail === 'string' ? json.detail : '请求未完成，请稍后重试')
  }
  return json
}
export const bytes = (n: number) => !n ? '0 B' : n >= 2**30 ? (n/2**30).toFixed(2)+' GB' : n >= 2**20 ? (n/2**20).toFixed(1)+' MB' : (n/1024).toFixed(1)+' KB'
export const date = (n?: number) => n ? new Date(n*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : '尚未检查'
export const duration = (n: number) => `${Math.floor((n||0)/60)}:${String(Math.floor((n||0)%60)).padStart(2,'0')}`

export const taskDate = (n?:number) => n ? new Date(n*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : '—'
export const elapsed = (n:number|null|undefined) => n==null ? '—' : n<60 ? Math.floor(n)+' 秒' : n<3600 ? Math.floor(n/60)+' 分 '+Math.floor(n%60)+' 秒' : Math.floor(n/3600)+' 小时 '+Math.floor(n%3600/60)+' 分'
