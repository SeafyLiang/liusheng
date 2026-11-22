<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { Play, Pause, SkipBack, SkipForward, Music2, ListMusic, X, LoaderCircle, ChevronDown, RefreshCw, ArrowUp, ArrowDown, ListPlus } from 'lucide-vue-next'
import { player as p, currentSong, mountPlayer, unmountPlayer, toggle, next, previous, seek, select, remove, move, playNext, compatible, stop, lyricLines } from './player'
import { duration } from './api'
const audio = ref<HTMLAudioElement>(), panel = ref<HTMLDialogElement>(), lyricBox = ref<HTMLElement>()
const track = computed(currentSong), lines = computed(() => lyricLines(p.lyrics))
const active = computed(() => { let at = -1; for (let i=0; i<lines.value.length; i++) if (lines.value[i]!.time <= p.time) at=i; return at })
const view = ref('lyrics')
onMounted(() => mountPlayer(audio.value!))
let resize: ResizeObserver | undefined
function centerLyric(smooth = true) {
  const box = lyricBox.value
  if (!p.expanded || view.value !== 'lyrics' || !box) return
  box.style.setProperty('--lyric-height', box.clientHeight + 'px')
  const el = box.querySelector('.current-line') as HTMLElement | null
  if (!el) { box.scrollTop=0; return }
  const b=box.getBoundingClientRect(), r=el.getBoundingClientRect()
  const top=box.scrollTop+r.top-b.top-box.clientTop+r.height/2-box.clientHeight/2
  box.scrollTo({top:Math.max(0,top),behavior:smooth&&!matchMedia('(prefers-reduced-motion: reduce)').matches?'smooth':'auto'})
}
onUnmounted(() => { resize?.disconnect(); unmountPlayer() })
watch(lyricBox, box => { resize?.disconnect(); if(box){resize=new ResizeObserver(()=>centerLyric(false));resize.observe(box)} },{flush:'post'})
watch(() => p.expanded, async opened => { await nextTick(); if (opened) {panel.value?.showModal();centerLyric(false)} else panel.value?.close() })
watch([active, view, () => p.lyrics], async()=>{await nextTick();centerLyric()})
</script>
<template>
  <audio ref="audio" preload="metadata" aria-hidden="true"></audio>
  <section v-if="track" class="player-bar" aria-label="音乐播放器">
    <button class="player-track" aria-label="打开歌词" @click="view='lyrics';p.expanded=true"><div class="player-cover"><img v-if="track.cover" :src="track.cover" alt="" referrerpolicy="no-referrer"><Music2 v-else :size="24"/></div><span><strong>{{track.title}}</strong><small>{{track.artists.join(' / ')}} · {{p.label}}</small></span></button>
    <div class="player-controls"><button class="icon-button player-prev" aria-label="上一首" :disabled="p.index===0&&p.time<3" @click="previous"><SkipBack :size="20"/></button><button class="player-toggle" :aria-label="p.preparing?'取消等待':p.playing?'暂停播放':'播放音乐'" @click="toggle"><LoaderCircle v-if="p.preparing||p.buffering" class="spin" :size="22"/><Pause v-else-if="p.playing" :size="23"/><Play v-else :size="23"/></button><button class="icon-button" aria-label="下一首" :disabled="p.index+1>=p.queue.length" @click="next"><SkipForward :size="20"/></button></div>
    <div class="player-seek"><span>{{duration(p.time)}}</span><input aria-label="播放进度" type="range" min="0" :max="p.total||1" step="0.1" :value="p.time" :disabled="!p.sourceReady" @change="seek(Number(($event.target as HTMLInputElement).value))"><span>{{duration(p.total)}}</span></div>
    <button class="icon-button player-queue-button" aria-label="播放队列" @click="view='queue';p.expanded=true"><ListMusic :size="23"/></button>
    <p v-if="p.error" class="player-error" role="alert">{{p.error}} <button @click="p.expanded=true">查看</button></p>
  </section>
  <dialog ref="panel" class="modal player-modal" @cancel="p.expanded=false" @click="($event.target===panel)&&(p.expanded=false)">
    <div v-if="track" class="modal-content">
      <button class="modal-close icon-button" aria-label="收起播放器" @click="p.expanded=false"><ChevronDown :size="23"/></button>
      <p class="eyebrow">正在聆听 · {{p.label}}</p><h2>{{track.title}}</h2><p class="muted">{{track.artists.join(' / ')}} · {{track.album}}</p>
      <div class="full-player-controls"><button class="icon-button" aria-label="上一首" :disabled="p.index===0&&p.time<3" @click="previous"><SkipBack/></button><button class="player-toggle" :aria-label="p.preparing?'取消等待':p.playing?'暂停播放':'播放音乐'" @click="toggle"><LoaderCircle v-if="p.preparing||p.buffering" class="spin"/><Pause v-else-if="p.playing"/><Play v-else/></button><button class="icon-button" aria-label="下一首" :disabled="p.index+1>=p.queue.length" @click="next"><SkipForward/></button><span>{{duration(p.time)}} / {{duration(p.total)}}</span></div>
      <input class="full-seek" aria-label="展开播放进度" type="range" min="0" :max="p.total||1" step="0.1" :value="p.time" :disabled="!p.sourceReady" @change="seek(Number(($event.target as HTMLInputElement).value))">
      <p v-if="p.error" class="inline-error" role="alert">{{p.error}}</p>
      <div class="button-row"><button v-if="p.error" class="secondary compact" @click="toggle"><RefreshCw :size="15"/>重试播放</button><button v-if="track.assetId&&!p.compatible" class="text-button" :disabled="p.preparing" @click="compatible">格式不支持？兼容播放</button></div>
      <div class="filter-bar"><button :class="{chosen:view==='lyrics'}" @click="view='lyrics'">歌词</button><button :class="{chosen:view==='queue'}" @click="view='queue'">播放队列 · {{p.queue.length}}</button></div>
      <div v-if="view==='lyrics'" ref="lyricBox" class="player-lyrics"><template v-if="lines.length"><div class="lyric-spacer" aria-hidden="true"></div><button v-for="(line,i) in lines" :key="i" :class="{'current-line':i===active}" :aria-current="i===active?'true':undefined" @click="seek(line.time)">{{line.text}}</button><div class="lyric-spacer" aria-hidden="true"></div></template><pre v-else-if="p.lyrics">{{p.lyrics}}</pre><p v-else class="muted">{{p.lyricsNote||'开始播放后读取歌词'}}</p></div>
      <div v-else class="player-queue" aria-label="当前播放列表"><p class="fine">调整顺序不会打断当前播放，顺序会保存在这台设备。</p><div v-for="(s,i) in p.queue" :key="s.queueId" :class="{current:i===p.index}"><span>{{i+1}}</span><button @click="select(i)"><strong>{{s.title}}</strong><small>{{s.artists.join(' / ')}}</small></button><div class="queue-order-controls"><button class="icon-button" :aria-label="'上移 '+s.title" :disabled="i===0" @click="move(i,i-1)"><ArrowUp :size="17"/></button><button class="icon-button" :aria-label="'下移 '+s.title" :disabled="i===p.queue.length-1" @click="move(i,i+1)"><ArrowDown :size="17"/></button><button class="icon-button" :aria-label="'下一首播放 '+s.title" :disabled="i===p.index||i===p.index+1" @click="playNext(i)"><ListPlus :size="17"/></button></div><span v-if="i===p.index" class="badge">{{p.playing?'播放中':'已选'}}</span><button v-else class="icon-button" :aria-label="'移出队列 '+s.title" @click="remove(i)"><X :size="16"/></button></div><button class="text-button" @click="stop(true)">停止并清空队列</button></div>
      <p class="fine">锁屏可通过系统播放器控制。手机后台连续播放需以设备实际表现为准。</p>
    </div>
  </dialog>
</template>
