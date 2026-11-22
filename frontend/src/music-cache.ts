// Namespace is obtained from the authenticated session, never from a remembered username.
let database: Promise<IDBDatabase> | undefined
function open() {
  if (!database) database = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('liusheng-music-v1', 1)
    request.onupgradeneeded = () => {
      const db = request.result
      db.createObjectStore('songs', {keyPath: ['ns', 'id']}).createIndex('ns', 'ns')
      db.createObjectStore('meta')
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
    request.onblocked = () => reject(new Error('请关闭旧页面后重试'))
  }).catch(error => { database = undefined; throw error })
  return database
}
const done = (tx: IDBTransaction) => new Promise<void>((resolve, reject) => {
  tx.oncomplete = () => resolve()
  tx.onerror = tx.onabort = () => reject(tx.error || new Error('本地缓存不可用'))
})
const result = <T>(req: IDBRequest<T>) => new Promise<T>((resolve, reject) => {
  req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error)
})
export async function cachedValue<T>(ns: string, key: string): Promise<T | undefined> {
  const db = await open()
  return result(db.transaction('meta').objectStore('meta').get(ns + ':' + key))
}
export async function cacheValue(ns: string, key: string, value: unknown) {
  const db = await open(), tx = db.transaction('meta', 'readwrite')
  tx.objectStore('meta').put(value, ns + ':' + key)
  await done(tx)
}
export async function cachedLibrary(ns: string) {
  const db = await open(), tx = db.transaction(['songs', 'meta'])
  const [cursor, rows] = await Promise.all([
    result(tx.objectStore('meta').get(ns + ':cursor')),
    result<any[]>(tx.objectStore('songs').index('ns').getAll(ns)),
  ])
  return typeof cursor === 'number' ? {cursor, items: rows.map(({ns: _ns, ...song}) => song)} : null
}
export async function cacheLibrary(ns: string, items: any[], cursor: number, replace = false) {
  const db = await open(), tx = db.transaction(['songs', 'meta'], 'readwrite')
  const songs = tx.objectStore('songs')
  const completed = done(tx)
  const write = () => {
    try {
      items.forEach(song => songs.put({...song, ns}))
      tx.objectStore('meta').put(cursor, ns + ':cursor')
    } catch { tx.abort() }
  }
  if (replace) {
    const keys = songs.index('ns').getAllKeys(ns)
    keys.onsuccess = () => { keys.result.forEach(key => songs.delete(key)); write() }
  } else write()
  await completed
}
