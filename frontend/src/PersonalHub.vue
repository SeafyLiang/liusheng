<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Heart, Library, ListMusic, Play, Plus, ArrowUp, ArrowDown, X, RefreshCw, LogOut, Download, Check, Music2, MoreHorizontal } from 'lucide-vue-next'
import { api, duration } from './api'
import { playSongs, enqueueSongs } from './player'
import VirtualList from './VirtualList.vue'
import { cachedLibrary, cacheLibrary, cachedValue, cacheValue } from './music-cache'

const newId=()=>crypto.randomUUID()
const props = defineProps<{user:{id:string;username:string;role:string;server_id:string}}>()
const emit = defineEmits<{manage:[];logout:[]}>()
const tab=ref('library'), query=ref(''), songs=ref<any[]>([]), playlists=ref<any[]>([]), favorites=ref<string[]>([]), publics=ref<any[]>([])
const busy=ref(false), error=ref(''), notice=ref(''), editor=ref<any>(null), publicDetail=ref<any>(null), users=ref<any[]>([]), release=ref<any>(null)
const newUsername=ref(''), newPassword=ref(''), oldPassword=ref(''), ownPassword=ref(''), resetId=ref(''), resetPassword=ref('')
const selected=ref<string[]>([])
const ns=props.user.server_id+':'+props.user.id
const refreshing=ref(false), syncText=ref(''), lastSync=ref(''), cacheWarning=ref(''), more=ref(false)
const filter=ref(''), sort=ref('title'), search=ref(''), songList=ref<InstanceType<typeof VirtualList>>()
let cursor:number|null=null, active=true, personalRequest=0, cacheNeedsRepair=false, searchTimer:ReturnType<typeof setTimeout>
const liked=computed(()=>new Set(favorites.value))
watch(query, value=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{search.value=value.trim().toLocaleLowerCase();songList.value?.reset()},150)})
watch([tab,filter,sort],()=>songList.value?.reset())
const visible=computed(()=>songs.value.filter(s=>(s.available||tab.value==='favorites') && (tab.value==='favorites'||!s.alias_of) && (tab.value!=='favorites'||favorites.value.includes(s.id)) &&
  (!filter.value||s.folders?.includes(filter.value)) && (!search.value||[s.title,...s.artists,s.album].join(' ').toLocaleLowerCase().includes(search.value)))
  .sort((a,b)=>sort.value==='recent'?b.revision-a.revision:(sort.value==='artist'?a.artists.join(' '):sort.value==='album'?a.album:a.title).localeCompare(sort.value==='artist'?b.artists.join(' '):sort.value==='album'?b.album:b.title,'zh-CN')))
const folders=computed(()=>[...new Set<string>(songs.value.flatMap(s=>s.folders||[]))].sort())
const byId=computed(()=>Object.fromEntries(songs.value.map(s=>[s.id,s])))
const playerSong=(s:any)=>({...s,cover:s.cover_url,asset_id:s.asset_id,key:s.id,library_id:s.id})
async function act(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function remember(key:string,value:any){try{await cacheValue(ns,key,JSON.parse(JSON.stringify(value)))}catch{cacheWarning.value='本地缓存不可用，下次打开需重新加载'}}
async function personal(){const request=++personalRequest;const [p,f]=await Promise.all([api('/v1/me/playlists'),api('/v1/me/favorites')]);if(!active||request!==personalRequest)return;playlists.value=p.items;favorites.value=f.song_ids;await remember('personal',{playlists:p.items,favorites:f.song_ids})}
const pendingKey='liusheng-web-op:'+props.user.server_id+':'+props.user.id
const hasPending=ref(!!localStorage.getItem(pendingKey))
async function flushPending(){
  const raw=localStorage.getItem(pendingKey);if(!raw)return null
  const saved=JSON.parse(raw);const r=await api('/v1/me/sync','POST',{cursor:0,operations:[saved]});if(localStorage.getItem(pendingKey)===raw)localStorage.removeItem(pendingKey)
  hasPending.value=!!localStorage.getItem(pendingKey)
  const result=r.results[0];notice.value=result.message||'已同步';if(editor.value?.id===saved.entity_id&&result.playlist){editor.value.id=result.playlist.id;editor.value.revision=result.playlist.revision}
  return result
}
async function library(){
  const map=new Map<string,any>(songs.value.map(s=>[s.id,s]))
  if(cursor===null){
    let after='', watermark:number|undefined, hasMore=true
    map.clear()
    while(hasMore&&active){
      const d=await api('/v1/library/snapshot?limit='+(after?500:100)+'&after='+after+(watermark===undefined?'':'&cursor='+watermark))
      if(!active)return
      watermark=d.cursor;after=d.after;hasMore=d.has_more;d.items.forEach((s:any)=>map.set(s.id,s))
      songs.value=[...map.values()];syncText.value=`已载入 ${map.size} 首`
    }
    if(!active)return
    cursor=watermark??0
    try{await cacheLibrary(ns,[...map.values()],cursor,true)}catch{cacheNeedsRepair=true;cacheWarning.value='本地缓存不可用，下次打开需重新加载'}
  }
  let hasMore=true
  while(hasMore&&active){
    const d=await api('/v1/library/changes?cursor='+cursor)
    if(!active)return
    if(d.items.length||cacheNeedsRepair){
      // Persist records and their cursor together. Failed storage can safely replay later.
      d.items.forEach((s:any)=>map.set(s.id,s));songs.value=[...map.values()]
      try{await cacheLibrary(ns,cacheNeedsRepair?JSON.parse(JSON.stringify([...map.values()])):d.items,d.cursor,cacheNeedsRepair);cacheNeedsRepair=false;cacheWarning.value=''}catch{cacheNeedsRepair=true;cacheWarning.value='本地缓存未更新，下次联网会重新校对'}
    }
    cursor=d.cursor;hasMore=d.has_more
  }
}
async function reload(){
  if(refreshing.value||!active)return
  refreshing.value=true;error.value='';syncText.value=songs.value.length?'后台更新中':'加载曲库'
  const results=await Promise.allSettled([
    library(),
    (async()=>{await flushPending();await personal()})(),
    (async()=>{const d=await api('/v1/library/playlists');if(active){publics.value=d.items;await remember('publics',d.items)}})(),
  ])
  if(!active)return
  const failures=results.filter((r):r is PromiseRejectedResult=>r.status==='rejected')
  error.value=failures.map(r=>r.reason.message).join('；')
  syncText.value=failures.length?'部分内容未更新':'已更新'
  if(!failures.length)lastSync.value=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})
  refreshing.value=false
}
async function operation(op:any){
  if(localStorage.getItem(pendingKey))throw new Error('上次修改仍待同步，请先点击刷新重试；本地记录已保留')
  const saved={id:crypto.randomUUID(),device_name:'网页',...op};localStorage.setItem(pendingKey,JSON.stringify(saved))
  hasPending.value=true
  const result=await flushPending();await personal();return result
}
async function like(s:any){const desired=!liked.value.has(s.id);await act(async()=>{await operation({kind:'favorite.set',payload:{song_id:s.id,liked:desired}})})}
function edit(p?:any){editor.value=p?JSON.parse(JSON.stringify(p)):{id:crypto.randomUUID(),revision:0,name:'',song_ids:[...selected.value]}}
async function save(){await act(async()=>{await operation({kind:'playlist.save',entity_id:editor.value.id,base_revision:editor.value.revision,payload:{name:editor.value.name,song_ids:editor.value.song_ids}});editor.value=null;selected.value=[]})}
async function removePlaylist(p:any){if(!confirm('删除这张个人歌单？NAS 音乐和手机下载会保留。'))return;await act(async()=>{await operation({kind:'playlist.delete',entity_id:p.id,base_revision:p.revision})})}
function move(index:number,delta:number){const ids=editor.value.song_ids;const to=index+delta;if(to<0||to>=ids.length)return;[ids[index],ids[to]]=[ids[to],ids[index]]}
function play(ids:string[]){const items=ids.map(id=>byId.value[id]).filter(s=>s?.available).map(playerSong);if(items.length)playSongs(items);else notice.value='这些歌曲当前不在曲库中'}
function enqueue(ids:string[]){const result=enqueueSongs(ids.map(id=>byId.value[id]).filter(s=>s?.available).map(playerSong));notice.value=`已加入 ${result.added} 首；跳过 ${result.duplicates} 首重复歌曲`}
async function showPublic(p:any){
  more.value=false;error.value=''
  try{const saved=await cachedValue<any>(ns,'public:'+p.id);if(active)publicDetail.value=saved||{...p,song_ids:[],loading:true}}catch{publicDetail.value={...p,song_ids:[],loading:true}}
  try{const d=await api('/v1/library/playlists/'+encodeURIComponent(p.id));if(active&&publicDetail.value?.id===p.id)publicDetail.value=d;await remember('public:'+p.id,d)}
  catch(e){error.value=(e as Error).message;if(publicDetail.value)publicDetail.value.loading=false}
}
async function showUsers(){more.value=false;tab.value='account';await act(async()=>{const tasks=[api('/v1/releases/android').then(d=>release.value=d)];if(props.user.role==='admin')tasks.push(api('/v1/admin/users').then(d=>users.value=d.items));await Promise.all(tasks)})}
async function createUser(){await act(async()=>{await api('/v1/admin/users','POST',{username:newUsername.value,password:newPassword.value});newUsername.value='';newPassword.value='';users.value=(await api('/v1/admin/users')).items;notice.value='账号已创建'})}
async function toggleUser(u:any){await act(async()=>{await api('/v1/admin/users/'+u.id,'PATCH',{enabled:!u.enabled});users.value=(await api('/v1/admin/users')).items})}
async function resetUser(){await act(async()=>{await api('/v1/admin/users/'+resetId.value,'PATCH',{password:resetPassword.value});resetId.value='';resetPassword.value='';notice.value='密码已重置，该账号的设备需要重新登录'})}
async function changePassword(){await act(async()=>{await api('/v1/auth/password','POST',{current:oldPassword.value,password:ownPassword.value});location.reload()})}
let priorFocus:HTMLElement|null=null
watch(()=>!!editor.value||!!publicDetail.value,async(open)=>{
  if(open){priorFocus=document.activeElement as HTMLElement;await nextTick();document.querySelector<HTMLElement>('.editor-overlay input,.editor-overlay button')?.focus()}
  else if(priorFocus?.isConnected)priorFocus.focus()
})
function dialogKeys(event:KeyboardEvent){
  if(event.key==='Escape'){editor.value=null;publicDetail.value=null;event.stopPropagation()}
  if(event.key!=='Tab')return
  const items=[...(event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('input,button,select,textarea,a[href]')].filter(e=>!e.hasAttribute('disabled')&&e.getClientRects().length)
  const first=items[0],last=items[items.length-1]
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
}
const online=()=>{void reload()}
onMounted(async()=>{
  try{
    const [library,p,lists]=await Promise.all([cachedLibrary(ns),cachedValue<any>(ns,'personal'),cachedValue<any[]>(ns,'publics')])
    if(!active)return
    if(library){songs.value=library.items;cursor=library.cursor;syncText.value='已显示上次内容'}
    if(p){playlists.value=p.playlists;favorites.value=p.favorites}
    if(lists)publics.value=lists
  }catch{cacheWarning.value='本地缓存不可用，下次打开需重新加载'}
  window.addEventListener('online',online);void reload()
})
onUnmounted(()=>{active=false;clearTimeout(searchTimer);window.removeEventListener('online',online)})
</script>

<template>
  <div class="personal-hub">
    <header class="personal-header"><a class="wordmark" href="/">留声</a><div class="header-actions">
      <button class="icon-button" aria-label="刷新曲库和个人数据" :disabled="refreshing" @click="reload"><RefreshCw :size="19" :class="{spin:refreshing}"/></button>
      <div class="more-wrap"><button class="icon-button" aria-label="更多" :aria-expanded="more" @click="more=!more"><MoreHorizontal :size="22"/></button>
        <div v-if="more" class="more-menu" @keydown.esc="more=false">
          <span class="muted">{{user.username}}</span>
          <button @click="showUsers">账号设置</button>
          <button v-if="user.role==='admin'" @click="emit('manage')">曲库管理</button>
          <details><summary>同步与帮助</summary><p>{{syncText}}<span v-if="lastSync"> · {{lastSync}}</span></p><p v-if="cacheWarning">{{cacheWarning}}</p><p>曲库所有人可见，喜欢和个人歌单只属于当前账号。</p><p>刷新会保留当前播放；歌单只记录歌曲，不复制音频。</p></details>
          <button @click="emit('logout')">退出登录</button>
        </div>
      </div>
    </div></header>
    <nav class="personal-tabs" aria-label="个人音乐导航"><button :class="{active:tab==='library'}" @click="tab='library'"><Library :size="18"/>曲库</button><button :class="{active:tab==='publics'}" @click="tab='publics'"><ListMusic :size="18"/>歌单</button><button :class="{active:tab==='favorites'}" @click="tab='favorites'"><Heart :size="18"/>喜欢</button><button :class="{active:tab==='playlists'}" @click="tab='playlists'"><ListMusic :size="18"/>我的</button></nav>
    <p v-if="error" class="inline-error" role="alert">{{error}} <button class="text-button" @click="reload">重试</button></p>
    <p v-if="notice" class="personal-notice" role="status"><Check :size="16"/>{{notice}}<button class="icon-button" aria-label="关闭提示" @click="notice=''"><X :size="16"/></button></p>
    <p v-if="!songs.length&&refreshing" class="muted" role="status">{{syncText}}…</p>
    <p v-if="hasPending" class="personal-notice" role="status">1 项修改待上传 <button class="text-button" :disabled="refreshing" @click="reload">重试</button></p>
    <template v-if="tab==='library'||tab==='favorites'">
      <div class="personal-heading"><h1>{{tab==='library'?'曲库':'喜欢'}} <small>{{visible.length}}</small></h1></div>
      <div class="personal-toolbar"><input v-model="query" type="search" placeholder="搜索歌名、歌手、专辑" aria-label="搜索共享曲库"><select v-model="filter" aria-label="文件夹筛选"><option value="">全部文件夹</option><option v-for="f in folders" :key="f" :value="f">{{f}}</option></select><select v-model="sort" aria-label="歌曲排序"><option value="title">歌名</option><option value="artist">歌手</option><option value="album">专辑</option><option value="recent">最近更新</option></select><button class="secondary" :disabled="!visible.length" @click="play(visible.map(s=>s.id))"><Play :size="16"/>播放</button><button v-if="selected.length" class="secondary" @click="edit()"><Plus :size="16"/>选中 {{selected.length}} 首建歌单</button><select v-if="selected.length&&playlists.length" aria-label="添加选中歌曲到已有歌单" @change="const p=playlists.find(p=>p.id===($event.target as HTMLSelectElement).value);if(p){edit(p);editor.song_ids=[...new Set([...editor.song_ids,...selected])]} "><option value="">加入已有歌单…</option><option v-for="p in playlists" :value="p.id">{{p.name}}</option></select></div>
      <VirtualList v-if="visible.length" ref="songList" :items="visible" label="歌曲列表"><template #default="{item:s}"><div class="personal-song"><input v-model="selected" type="checkbox" :value="s.id" :aria-label="'选择 '+s.title"><button class="song-main" :aria-label="'播放 '+s.title" @click="play([s.id])"><img v-if="s.cover_url" :src="s.cover_url" alt="" loading="lazy"><span v-else class="song-art"><Music2 :size="22"/></span><span><strong>{{s.title}}</strong><small>{{s.artists.join(' / ')}} · {{s.album||'未知专辑'}}{{s.available?'':' · 文件不可用'}}</small></span></button><span class="song-duration">{{duration(s.duration)}}</span><button class="icon-button" :aria-label="(liked.has(s.id)?'取消喜欢 ':'喜欢 ')+s.title" :aria-pressed="liked.has(s.id)" :disabled="busy" @click="like(s)"><Heart :size="20" :fill="liked.has(s.id)?'currentColor':'none'"/></button><button class="icon-button" :aria-label="'把 '+s.title+' 加入播放队列'" @click="enqueue([s.id])"><Plus :size="19"/></button></div></template></VirtualList>
      <div v-if="!visible.length&&!busy" class="empty"><Music2 :size="38"/><h3>{{tab==='favorites'?'喜欢，从一首歌开始':'还没有找到音乐'}}</h3><p>{{tab==='favorites'?'在歌曲旁点一下爱心，就会出现在这里。':'试试其他关键词，或等待管理员添加音乐。'}}</p></div>
    </template>
    <template v-if="tab==='publics'"><div class="personal-heading"><h1>公共歌单 <small>{{publics.length}}</small></h1></div><div class="personal-playlist-grid"><button v-for="p in publics" :key="p.id" class="personal-playlist" @click="showPublic(p)"><ListMusic :size="26"/><strong>{{p.name}}</strong></button></div><p v-if="!publics.length&&!refreshing" class="muted">暂无公共歌单</p></template>
    <template v-if="tab==='playlists'"><div class="personal-heading"><div><h1>我的歌单</h1></div><button class="primary" @click="edit()"><Plus :size="18"/>新建歌单</button></div><div class="personal-playlist-grid"><article v-for="p in playlists" :key="p.id" class="personal-playlist"><ListMusic :size="30"/><h2>{{p.name}}</h2><p>{{p.song_ids.length}} 首<span v-if="p.conflict_of"> · 冲突副本</span></p><div class="button-row"><button class="secondary compact" @click="play(p.song_ids)">播放</button><button class="secondary compact" @click="enqueue(p.song_ids)">加入队列</button><button class="secondary compact" @click="edit(p)">编辑</button><button class="text-button" :disabled="busy" @click="removePlaylist(p)">删除歌单</button></div></article></div><p v-if="!playlists.length" class="muted">新建一张歌单，再去曲库选择想加入的歌曲。</p></template>
    <section v-if="tab==='account'" class="personal-account"><h1>账号</h1><p>{{user.username}}</p><button class="secondary" @click="emit('logout')"><LogOut :size="18"/>退出登录</button><article class="panel"><h2>更改我的密码</h2><form @submit.prevent="changePassword"><label>当前密码<input v-model="oldPassword" type="password" autocomplete="current-password" required></label><label>新密码<input v-model="ownPassword" type="password" minlength="12" maxlength="256" autocomplete="new-password" required></label><button class="primary" :disabled="busy">修改密码并重新登录</button></form></article><article class="panel"><h2>安卓 App</h2><a v-if="release?.available" class="primary" :href="release.apk_url"><Download :size="18"/>下载 v{{release.version_name}}</a><p v-else class="muted">还没有发布可下载的安装包。</p></article><template v-if="user.role==='admin'"><article class="panel"><h2>创建普通账号</h2><form @submit.prevent="createUser"><label>用户名<input v-model="newUsername" required pattern="[A-Za-z0-9_.-]{3,40}" autocomplete="off" placeholder="3 至 40 位字母、数字或 ._-"></label><label>初始密码<input v-model="newPassword" type="password" required minlength="12" maxlength="256" autocomplete="new-password"></label><button class="primary" :disabled="busy">创建账号</button></form></article><article class="panel"><h2>账号管理</h2><div v-for="u in users" class="user-row"><span><strong>{{u.username}}</strong><small>{{u.role==='admin'?'管理员':u.enabled?'可用':'已停用'}}</small></span><button v-if="u.role!=='admin'" class="secondary compact" :disabled="busy" @click="toggleUser(u)">{{u.enabled?'停用':'启用'}}</button><button class="text-button" @click="resetId=u.id">重置密码</button></div><form v-if="resetId" @submit.prevent="resetUser"><label>为 {{users.find(u=>u.id===resetId)?.username}} 设置新密码<input v-model="resetPassword" type="password" required minlength="12" autocomplete="new-password"></label><button class="primary" :disabled="busy">保存新密码</button></form></article></template></section>
    <section v-if="editor" class="editor-overlay" @keydown="dialogKeys" role="dialog" aria-modal="true" aria-label="编辑个人歌单"><form class="editor-card" @submit.prevent="save"><div class="button-row"><h2>编辑歌单</h2><button type="button" class="icon-button" aria-label="关闭歌单编辑" @click="editor=null"><X/></button></div><label>歌单名称<input v-model="editor.name" maxlength="160" required autofocus></label><p v-if="error" class="inline-error" role="alert">{{error}}</p><VirtualList :items="editor.song_ids.map((id:string)=>({id}))" :row-height="52" label="编辑歌单歌曲"><template #default="{item,index:i}"><div class="editor-track"><span>{{byId[item.id]?.title||'暂不可用的歌曲'}}{{byId[item.id]?.available?'':' · 文件不可用'}}</span><button type="button" class="icon-button" :disabled="Number(i)===0" :aria-label="'上移 '+(byId[item.id]?.title||'歌曲')" @click="move(Number(i),-1)"><ArrowUp :size="17"/></button><button type="button" class="icon-button" :disabled="Number(i)===editor.song_ids.length-1" :aria-label="'下移 '+(byId[item.id]?.title||'歌曲')" @click="move(Number(i),1)"><ArrowDown :size="17"/></button><button type="button" class="icon-button" :aria-label="'移出 '+(byId[item.id]?.title||'歌曲')" @click="editor.song_ids.splice(Number(i),1)"><X :size="17"/></button></div></template></VirtualList><button class="primary wide" :disabled="busy">保存并同步</button></form></section>
    <section v-if="publicDetail" class="editor-overlay" @keydown="dialogKeys" role="dialog" aria-modal="true" aria-label="公共歌单"><div class="editor-card"><div class="button-row"><h2>{{publicDetail.name}}</h2><button class="icon-button" aria-label="关闭公共歌单" @click="publicDetail=null"><X/></button></div><p class="muted">{{publicDetail.loading?'加载中…':publicDetail.song_ids.length+' 首'}}</p><div class="button-row"><button class="primary" @click="play(publicDetail.song_ids)">播放</button><button class="secondary" @click="enqueue(publicDetail.song_ids)">加入队列</button><button class="secondary" @click="editor={id:newId(),revision:0,name:publicDetail.name,song_ids:[...publicDetail.song_ids]};publicDetail=null">复制为个人歌单</button></div><VirtualList :items="publicDetail.song_ids.map((id:string)=>byId[id]||{id,title:'歌曲加载中',artists:[]})" :row-height="52" label="歌单歌曲"><template #default="{item:s}"><div class="public-track"><strong>{{s.title}}</strong><small>{{s.artists.join(' / ')}}</small></div></template></VirtualList></div></section>
  </div>
</template>

<style scoped>
.more-wrap{position:relative}.more-menu{position:absolute;right:0;top:42px;z-index:20;width:260px;padding:16px;background:#fff;border:1px solid #dce5e7;border-radius:14px;box-shadow:0 12px 36px #2636451c;display:grid;gap:6px}.more-menu>button{border:0;background:transparent;text-align:left;padding:10px;border-radius:8px;cursor:pointer}.more-menu>button:hover{background:#e1eeea}.more-menu details{padding:10px;font-size:13px}.more-menu summary{cursor:pointer}.more-menu p{line-height:1.6;color:#657784}.personal-heading h1 small{font:400 14px system-ui;color:#657784;margin-left:8px}.public-track{display:flex;gap:12px;align-items:center;height:52px;border-bottom:1px solid #e3e9eb}.public-track strong{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}.public-track small{max-width:35%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#657784}

.personal-hub{max-width:1100px;margin:auto;padding:28px 32px 150px;color:#263645}.personal-header,.header-actions,.personal-tabs,.personal-heading,.personal-toolbar,.personal-song,.user-row,.editor-track{display:flex;align-items:center;gap:14px}.personal-header{justify-content:space-between;margin-bottom:30px}.wordmark{font-family:ui-serif,serif;font-size:30px;letter-spacing:.1em;color:inherit;text-decoration:none}.wordmark span{display:block;font-family:system-ui;font-size:11px;letter-spacing:.1em;color:#657784;margin-top:6px}.header-actions{font-size:13px}.personal-tabs{border-bottom:1px solid #d9e2e5;padding-bottom:12px}.personal-tabs button{display:flex;align-items:center;gap:8px;padding:12px 18px;border:0;background:transparent;border-radius:12px;color:#657784;cursor:pointer}.personal-tabs .active{background:#e1eeea;color:#216a67;font-weight:650}.personal-heading{justify-content:space-between;margin:34px 0 24px}.personal-heading h1{font-size:clamp(26px,5vw,38px);font-weight:650;letter-spacing:-.03em;margin:8px 0}.heading-note{color:#a8c5be;transform:rotate(-12deg)}.personal-toolbar{flex-wrap:wrap;margin-bottom:18px}.personal-toolbar>input{flex:1;min-width:180px}.personal-song{border-bottom:1px solid #e3e9eb;padding:12px 0;gap:10px}.personal-song>input{width:18px;height:18px;flex:none}.song-main{min-width:0;flex:1;display:flex;align-items:center;gap:14px;text-align:left;border:0;background:transparent;cursor:pointer;color:inherit}.song-main>img,.song-art{width:48px;height:48px;border-radius:8px;object-fit:cover;flex:none}.song-art{background:#dfeae7;display:grid;place-items:center;color:#437c70}.song-main>span:last-child{min-width:0}.song-main strong,.song-main small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.song-main strong{font-size:15px;font-weight:600}.song-main small{font-size:12px;color:#6a7c86;margin-top:6px}.song-duration{font-size:12px;color:#77868c}.personal-song button[aria-pressed=true]{color:#b55e48}.personal-playlist-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin:20px 0}.personal-playlist{display:flex;flex-direction:column;gap:12px;border:1px solid #dce5e7;border-radius:16px;padding:22px;background:#fff;text-align:left;color:inherit}.personal-playlist h2{font-size:18px;margin:0}.personal-playlist small{color:#687a85}.personal-notice{display:flex;align-items:center;gap:8px;padding:12px;background:#e5f1eb;border-radius:12px;color:#216a67}.personal-notice button{margin-left:auto}.personal-account{max-width:720px;margin:32px auto}.personal-account .panel{padding:24px;margin:20px 0}.personal-account form{display:grid;gap:16px}.personal-account label,.editor-card label{display:grid;gap:8px}.user-row{padding:15px 0;border-bottom:1px solid #e4e9eb}.user-row>span{flex:1}.user-row small{display:block;color:#77848c}.editor-overlay{position:fixed;inset:0;z-index:100;background:#172f3c66;display:grid;place-items:center;padding:20px}.editor-card{width:min(620px,100%);max-height:85vh;overflow:auto;background:#f8faf9;padding:26px;border-radius:22px;box-shadow:0 20px 70px #142c3430}.editor-card>.button-row{justify-content:space-between}.editor-tracks{max-height:45vh;overflow:auto;margin:18px 0}.editor-track{padding:10px 0;border-bottom:1px solid #dfe7e8;gap:5px}.editor-track>span{flex:1;min-width:0}.editor-card .primary{background:#216a67}.personal-hub button:focus-visible,.personal-hub a:focus-visible{outline:3px solid #77a99b;outline-offset:3px}@media(max-width:600px){.personal-hub{padding:18px 16px 140px}.personal-header{align-items:flex-start}.header-actions{gap:4px;flex-wrap:wrap;justify-content:flex-end;max-width:60%}.header-actions>span{width:100%;text-align:right}.wordmark span{font-size:10px}.personal-tabs{gap:2px;justify-content:space-between}.personal-tabs button{padding:10px;font-size:13px}.song-duration{display:none}.song-main{gap:10px}.song-main>img,.song-art{width:40px;height:40px}.personal-song{gap:3px}.personal-song .icon-button{padding:8px}.personal-playlist-grid{grid-template-columns:1fr}.editor-card{padding:18px}.personal-toolbar>input{flex-basis:100%}.heading-note{display:none}}
</style>

<style scoped>
.personal-header{margin-bottom:20px}.personal-heading{margin:18px 0 16px}.personal-heading h1{font-size:26px}.personal-song{height:72px;box-sizing:border-box;padding:10px 0}.personal-tabs button{padding:10px 16px}.personal-toolbar select{max-width:155px}.personal-toolbar input{min-width:200px}.editor-card .virtual-list{height:45dvh;min-height:120px}.personal-account .panel{margin:16px 0}.personal-account h1{font-size:26px}
@media(max-width:600px){.personal-tabs button{padding:9px;font-size:13px}.personal-tabs{gap:0}.personal-toolbar{gap:8px}.personal-toolbar select{flex:1;min-width:0;max-width:40%}.personal-toolbar button{padding:9px}.personal-hub{padding-bottom:100px}.personal-heading{margin:14px 0}.virtual-list{height:calc(100dvh - 350px)}}
@media(prefers-reduced-motion:reduce){.spin{animation:none}}
</style>

<style scoped>
.personal-toolbar input,.personal-toolbar select,.personal-account input,.editor-card input{border:1px solid #dce5e7;border-radius:10px;background:#fff;color:#263645;padding:11px 12px;font:inherit;box-sizing:border-box;min-height:42px}.personal-toolbar input:focus,.personal-toolbar select:focus{outline:2px solid #77a99b;outline-offset:1px}.personal-toolbar input{min-width:220px}.personal-heading h1{margin:0}.editor-track{height:52px;box-sizing:border-box}.virtual-list{height:calc(100dvh - 380px)}
@media(max-width:600px){.personal-toolbar input{width:100%}.personal-toolbar select{max-width:none}.virtual-list{height:calc(100dvh - 390px)}}
</style>
