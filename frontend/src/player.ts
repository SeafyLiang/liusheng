import { reactive } from 'vue'
import { api } from './api'

export type Song = { queueId: string; id: string; title: string; artists: string[]; album: string; cover: string; duration: number; assetId?: string; key?: string; libraryId?: string }
export const player = reactive({
  queue: [] as Song[], index: -1, playing: false, preparing: false, buffering: false,
  time: 0, total: 0, error: '', label: '', lyrics: '', lyricsNote: '', expanded: false,
  needsStart: false, sourceReady: false, compatible: false,
})
let audio: HTMLAudioElement | null = null
let generation = 0, pendingSeek = 0, lastSave = 0
let metadataId = ''
let storageKey = 'liusheng-player-v1'
export function setPlayerAccount(server:string,user:string) {storageKey='liusheng-player-v2:'+server+':'+user}
const queueLimit = 1000
let queueSerial=0
const newQueueId=()=>Date.now().toString(36)+'-'+(++queueSerial)
export const currentSong = () => player.queue[player.index]
export function song(a: any): Song {
  const assetId = a.asset_id || (a.path ? a.id : undefined)
  return { queueId:newQueueId(), id: assetId ? 'asset:' + assetId : a.key, assetId, key: a.key || a.track_key, libraryId: a.library_id,
    title: a.title, artists: a.artists || [], album: a.album || '', duration: a.duration || 0,
    cover: assetId && a.has_cover ? `/api/covers/${assetId}` : (a.cover || '') }
}
function save() {
  if (!currentSong()) return
  try { localStorage.setItem(storageKey, JSON.stringify({ queue: player.queue, index: player.index, time: player.time })) } catch { /* Storage can be disabled. */ }
}
function session() {
  const t = currentSong()
  if (!('mediaSession' in navigator) || !t) return
  try {
    if (metadataId !== t.id) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artists.join(' / '), album: t.album,
        artwork: t.cover ? [{ src: new URL(t.cover, location.href).href }] : [] })
      metadataId = t.id
    }
    navigator.mediaSession.playbackState = player.playing ? 'playing' : 'paused'
    if (audio && Number.isFinite(audio.duration) && audio.duration > 0) navigator.mediaSession.setPositionState?.({
      duration: audio.duration, playbackRate: audio.playbackRate, position: Math.min(audio.duration, Math.max(0, audio.currentTime)),
    })
  } catch { /* A subset of controls can be unavailable on this browser. */ }
}
function progress() {
  if (!audio) return
  player.time = audio.currentTime || 0
  if (!audio.paused && audio.readyState >= 3) player.buffering = false
  if (Number.isFinite(audio.duration)) player.total = audio.duration
  session()
  if (Date.now() - lastSave > 5000) { save(); lastSave = Date.now() }
}
async function startAudio(token: number) {
  if (!audio || token !== generation) return
  try {
    player.error = ''; player.needsStart = false
    await audio.play()
  } catch (e) {
    if (token !== generation || (e as DOMException).name === 'AbortError') return
    player.buffering = false
    if ((e as DOMException).name === 'NotAllowedError') {
      player.needsStart = true; player.error = '音频已准备好，请点播放继续'
    } else player.error = '播放未开始，请重试；格式不支持时可选择兼容播放'
  }
}
async function lyrics(t: Song, token: number) {
  player.lyrics = ''; player.lyricsNote = '正在读取歌词…'
  try {
    const result = t.assetId ? await api(`/library/${t.assetId}/detail`) : t.key ? await api(`/playback/lyrics?key=${encodeURIComponent(t.key)}`) : null
    if (token !== generation) return
    player.lyrics = result?.lyrics_text || ''; player.lyricsNote = player.lyrics ? '' : '这首歌暂时没有歌词'
  } catch { if (token === generation) player.lyricsNote = '歌词暂不可用，音频播放不受影响' }
}
async function prepare(t: Song, token: number, compatibility: boolean) {
  player.preparing = true; player.label = compatibility ? '正在准备兼容副本' : '正在准备试听'
  try {
    const personal = compatibility && !!t.libraryId
    let result = personal ? await api('/v1/downloads/prepare','POST',{song_id:t.libraryId,profile:'compatible'}) : await api('/playback/prepare', 'POST', compatibility ? { asset_id: t.assetId } : { key: t.key })
    const until = Date.now() + 240000
    while (['preparing','queued'].includes(result.state)) {
      if (token !== generation) return
      if (Date.now() > until) throw new Error('音频准备时间较长，请稍后重试')
      await new Promise(resolve => setTimeout(resolve, 1000))
      if (token !== generation) return
      result = await api(personal ? `/v1/downloads/${result.id}` : `/playback/preparations/${result.id}`)
    }
    if (token !== generation || !audio) return
    if (result.state !== 'ready') {
      const last = result.attempts?.at(-1)
      throw new Error((result.error || result.label || '准备失败') + (last?.detail ? '：' + last.detail : ''))
    }
    if (result.asset_id) { t.assetId = result.asset_id; void lyrics(t, token) }
    player.label = (personal ? '兼容版 MP3' : result.label) + (result.source === 'gd' ? ' · GD 音乐台' : result.source === 'netease_direct' ? ' · 网易云公开音频' : '')
    player.preparing = false; player.sourceReady = true; player.compatible = compatibility
    audio.src = personal ? result.audio_url : result.url; audio.load(); void startAudio(token)
  } catch (e) {
    if (token === generation) {
      player.preparing = false; player.buffering = false
      player.label = compatibility ? '兼容准备失败' : '试听暂不可用'
      player.error = (e as Error).message
    }
  }
}
export function select(index: number, position = 0) {
  if (!audio || !player.queue[index]) return
  const token = ++generation
  audio.pause(); audio.removeAttribute('src'); audio.load()
  player.index = index; player.time = position; pendingSeek = position
  const t = currentSong()!
  player.total = t.duration; player.error = ''; player.buffering = false; player.preparing = false
  player.needsStart = false; player.sourceReady = false; player.compatible = false
  session(); save(); void lyrics(t, token)
  if (t.assetId) {
    player.label = '音乐库'; player.sourceReady = true
    audio.src = `/api/library/${t.assetId}/audio`; audio.load(); void startAudio(token)
  } else void prepare(t, token, false)
}
export function playSongs(items: any[], index = 0) {
  const valid = items.filter(t => t.present !== false).map(song).filter(t => t.assetId || t.key)
  if (!valid.length) return
  player.queue = valid.slice(0, queueLimit); select(Math.min(index, player.queue.length - 1))
}
export function enqueueSongs(items: any[]) {
  const known = new Set(player.queue.map(t => t.id))
  const result = { added: 0, duplicates: 0, unavailable: 0, overflow: 0 }
  for (const item of items) {
    const t = song(item)
    if (item.present === false || !(t.assetId || t.key)) { result.unavailable++; continue }
    if (known.has(t.id)) { result.duplicates++; continue }
    if (player.queue.length >= queueLimit) { result.overflow++; continue }
    known.add(t.id); player.queue.push(t); result.added++
  }
  if (result.added) {
    if (player.index < 0) {
      player.index = 0; player.total = currentSong()!.duration; player.label = '待播放'; player.needsStart = true
    }
    save()
  }
  return result
}
export function enqueue(item: any) { return enqueueSongs([item]) }
export function toggle() {
  if (!audio || !currentSong()) return
  if (player.preparing) { ++generation; player.preparing = false; player.label = '已取消等待'; return }
  if (player.playing) { audio.pause(); return }
  if (!player.sourceReady || player.error && !player.needsStart) select(player.index, player.time)
  else { if (audio.ended) audio.currentTime = 0; void startAudio(generation) }
}
export function next() { if (player.index + 1 < player.queue.length) select(player.index + 1) }
export function previous() {
  if (player.time > 3 && player.sourceReady) seek(0)
  else if (player.index > 0) select(player.index - 1)
}
export function seek(value: number) {
  if (!audio || !player.sourceReady || !Number.isFinite(audio.duration)) return
  audio.currentTime = Math.max(0, Math.min(audio.duration, value)); progress(); save()
}
export function compatible() {
  const t = currentSong()
  if (!audio || !t?.assetId) return
  const token = ++generation; pendingSeek = player.time
  audio.pause(); audio.removeAttribute('src'); audio.load()
  player.sourceReady = false; player.error = ''; void prepare(t, token, true)
}
export function remove(index: number) {
  if (index === player.index) return
  player.queue.splice(index, 1); if (index < player.index) player.index--; save()
}
export function move(from: number, to: number) {
  if (from === to || from < 0 || to < 0 || from >= player.queue.length || to >= player.queue.length) return
  const current = currentSong()
  const [item] = player.queue.splice(from, 1)
  player.queue.splice(to, 0, item!)
  player.index = current ? player.queue.indexOf(current) : -1
  save()
}
export function playNext(index: number) {
  if (index === player.index || player.index < 0) return
  move(index, player.index + (index > player.index ? 1 : 0))
}
export function savePlayback() { save() }
export function stop(clearMemory = false) {
  ++generation
  audio?.pause(); audio?.removeAttribute('src'); audio?.load()
  player.playing = false; player.preparing = false; player.buffering = false; player.sourceReady = false
  player.queue = []; player.index = -1; player.error = ''; player.expanded = false
  metadataId = ''
  if ('mediaSession' in navigator) { navigator.mediaSession.metadata = null; navigator.mediaSession.playbackState = 'none' }
  if (clearMemory) try { localStorage.removeItem(storageKey) } catch { /* Private storage. */ }
}
const pause = () => { player.playing = false; player.buffering = false; session(); save() }
const playing = () => { player.playing = true; player.buffering = false; player.error = ''; session() }
const waiting = () => { if (player.sourceReady) player.buffering = true }
const metadata = () => {
  if (pendingSeek && audio && Number.isFinite(audio.duration)) { audio.currentTime = Math.min(pendingSeek, Math.max(0, audio.duration - .1)); pendingSeek = 0 }
  progress()
}
const ended = () => { pause(); if (player.index + 1 < player.queue.length) next() }
const failed = () => {
  if (!player.sourceReady || !audio?.error) return
  player.playing = false; player.buffering = false
  player.error = '音频加载失败，可重试；请检查网络、登录状态或尝试兼容播放'; session()
}
const events: Record<string, EventListener> = { timeupdate: progress, loadedmetadata: metadata, playing, pause, waiting, stalled: waiting, ended, error: failed }
export function mountPlayer(element: HTMLAudioElement) {
  audio = element
  Object.entries(events).forEach(([name, fn]) => audio!.addEventListener(name, fn))
  window.addEventListener('pagehide', save)
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || 'null')
    if (stored && Array.isArray(stored.queue)) {
      player.queue = stored.queue.filter((s: Song) => typeof s.title === 'string' && Array.isArray(s.artists) && (s.assetId || s.key)).slice(0, queueLimit).map((s:Song)=>({...s,queueId:s.queueId||newQueueId()}))
      player.index = Math.min(Math.max(0, Number(stored.index) || 0), player.queue.length - 1)
      player.time = Math.max(0, Number(stored.time) || 0); player.total = currentSong()?.duration || 0
      player.label = '上次播放 · 点击继续'; player.needsStart = true
    }
  } catch { /* Invalid or unavailable local storage must not block playback. */ }
  if ('mediaSession' in navigator) {
    const handlers: Partial<Record<MediaSessionAction, MediaSessionActionHandler>> = {
      play: () => { if (!player.playing) toggle() }, pause: () => audio?.pause(), previoustrack: previous, nexttrack: next,
      seekto: d => seek(d.seekTime ?? player.time), seekbackward: d => seek(player.time - (d.seekOffset || 10)),
      seekforward: d => seek(player.time + (d.seekOffset || 10)), stop: () => audio?.pause(),
    }
    for (const [name, handler] of Object.entries(handlers)) try { navigator.mediaSession.setActionHandler(name as MediaSessionAction, handler!) } catch { /* Optional action. */ }
  }
}
export function unmountPlayer() {
  save(); stop()
  Object.entries(events).forEach(([name, fn]) => audio?.removeEventListener(name, fn))
  window.removeEventListener('pagehide', save); audio = null
  if ('mediaSession' in navigator) for (const name of ['play','pause','previoustrack','nexttrack','seekto','seekbackward','seekforward','stop']) {
    try { navigator.mediaSession.setActionHandler(name as MediaSessionAction, null) } catch { /* Optional action. */ }
  }
}
export function lyricLines(raw: string) {
  const offset = Number(raw.match(/\[offset:([+-]?\d+)\]/i)?.[1] || 0) / 1000
  return raw.split('\n').flatMap(line => {
    const stamps = [...line.matchAll(/\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g)]
    const text = line.replace(/\[[^\]]*\]/g, '').trim()
    return text ? stamps.map(s => ({ time: Number(s[1]) * 60 + Number(s[2]) + Number('0.' + (s[3] || '0')) + offset, text })) : []
  }).sort((a, b) => a.time - b.time)
}
