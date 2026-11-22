<script setup lang="ts">
import { ref } from 'vue'
import { Upload, Search, RefreshCw, Check, CircleAlert, FileMusic } from 'lucide-vue-next'
import { api, date, duration } from './api'
defineProps<{ settings: any }>()
const emit = defineEmits<{ refresh: [] }>()
const link = ref(''), query = ref(''), platform = ref('netease'), samples = ref<any[]>([]), sample = ref('')
const busy = ref(''), message = ref(''), error = ref('')
const names: Record<string,string> = {wy:'网易云',tx:'QQ',kw:'酷我',kg:'酷狗',mg:'咪咕',local:'本地'}
const states: Record<string,string> = {imported:'待检查',ready:'待整曲验证',verified:'样本验证通过',failed:'验证未通过'}
async function act(label:string, work:()=>Promise<void>) {
  if(busy.value)return
  busy.value=label;error.value='';message.value=''
  try{await work();emit('refresh')}catch(e){error.value=(e as Error).message}finally{busy.value=''}
}
async function importLink(){await act('导入脚本',async()=>{const r=await api('/lx/import-url','POST',{url:link.value});link.value='';message.value=r.duplicate?'该脚本已在音源列表中':'已保存私有脚本，请检查兼容性后验证歌曲'})}
async function importFile(event:Event){const input=event.target as HTMLInputElement;const file=input.files?.[0];if(!file)return;await act('导入脚本',async()=>{const form=new FormData();form.append('file',file);const r=await api('/lx/import-file','POST',form);message.value=r.duplicate?'该脚本已在音源列表中':'已保存私有脚本，请检查兼容性后验证歌曲'});input.value=''}
async function inspect(source:any){await act('检查 '+source.name,async()=>{const r=await api(`/lx/${source.id}/inspect`,'POST');if(!r.ok)throw new Error(r.detail);message.value='初始化通过。选择歌曲后可验证整曲，尚未自动启用'})}
async function search(){await act('查找验证歌曲',async()=>{samples.value=(await api(`/search?q=${encodeURIComponent(query.value)}&platform=${platform.value}`)).items;sample.value='';message.value=samples.value.length?'请选择正确版本作为样本':'没有搜索结果，可更换关键词或平台'})}
async function probe(source:any){await act('验证 '+source.name+'，请稍候',async()=>{const r=await api(`/lx/${source.id}/probe`,'POST',{key:sample.value});emit('refresh');if(!r.ok)throw new Error(r.detail);message.value=`完整音频验证通过（${duration(r.evidence.duration)}），样本已清理，未写入曲库`})}
async function toggleLyrics(id:string, settings:any){await act('保存歌词来源',async()=>{const list=settings.lyric_providers.includes(id)?settings.lyric_providers.filter((x:string)=>x!==id):[...settings.lyric_providers,id];await api('/settings','PATCH',{lyric_providers:list});message.value='歌词来源已保存'})}
</script>

<template>
  <article class="panel settings-panel full-width source-manager">
    <div class="section-title"><h2><FileMusic :size="20"/>LX 音源管理</h2><span class="badge" :class="settings.lx_runner_configured?'succeeded':'failed'">{{settings.lx_runner_configured?'执行服务已配置':'执行服务未配置'}}</span></div>
    <p class="muted">导入你的 JS 音源，检查支持的平台，再用一首歌验证。脚本保存在私有目录，整曲验证通过后才可加入上方音频来源。</p>
    <form class="source-import" @submit.prevent="importLink"><input v-model="link" type="url" required aria-label="LX 脚本链接" placeholder="粘贴 https://…/source.js"><button class="secondary" :disabled="!!busy">从链接导入</button><label class="secondary upload-button"><Upload :size="16"/>导入 JS 文件<input type="file" accept=".js" :disabled="!!busy" @change="importFile"></label></form>
    <p class="fine">支持网易云、QQ 和酷我歌曲信息；其他平台能力会显示，但暂不自动跨平台匹配。更新脚本请重新导入并验证。单文件上限 256 KiB。</p>
    <div class="source-sample">
      <h3>选择验证样本</h3><form class="source-import" @submit.prevent="search"><select v-model="platform" aria-label="样本搜索平台"><option value="netease">网易云</option><option value="tencent">QQ 音乐</option><option value="kuwo">酷我音乐</option></select><input v-model="query" required minlength="2" aria-label="验证歌曲关键词" placeholder="歌名和歌手"><button class="secondary" :disabled="!!busy"><Search :size="16"/>搜索样本</button></form>
      <select v-if="samples.length" v-model="sample" aria-label="选择验证歌曲"><option value="">选择正确的歌曲版本</option><option v-for="t in samples" :key="t.key" :value="t.key">{{t.title}} · {{t.artists.join(' / ')}} · {{duration(t.duration)}}</option></select>
      <p class="fine">验证会临时获取一首音频并完整解码，完成后清理，不会自动入库。一次样本通过不保证所有歌曲可用。</p>
    </div>
    <p v-if="busy" role="status" class="muted">{{busy}}</p><p v-if="message" role="status" class="source-feedback"><Check :size="16"/>{{message}}</p><p v-if="error" role="alert" class="inline-error">{{error}}</p>
    <p v-if="!settings.lx_sources.length" class="inbox-empty">还没有导入 LX 音源。</p>
    <div v-for="s in settings.lx_sources" :key="s.id" class="source-card">
      <div class="section-title"><h3>{{s.name}} <small>{{s.version||'未标注版本'}}</small></h3><span class="badge" :class="s.state==='verified'?'succeeded':s.state==='failed'?'failed':'queued'">{{states[s.state]}}</span></div>
      <p v-if="s.description" class="fine">{{s.description}}</p>
      <p v-if="s.batch_restricted" class="inline-error"><CircleAlert :size="15"/>脚本注明限制批量下载，仅供单曲验证，不加入自动任务。</p>
      <div class="source-capabilities"><span v-for="(cap,key) in s.capabilities" :key="key" class="badge">{{names[String(key)]||key}} · {{cap.qualitys?.join(' / ')||'未声明音质'}}</span></div>
      <p v-if="s.verified_platforms.length" class="fine">已通过整曲验证的平台：{{s.verified_platforms.map((p:string)=>names[p]||p).join('、')}}。上方音质为脚本声明，实际音质以获取结果为准。</p>
      <p>{{s.detail}}</p><p v-if="s.checked" class="fine">最近检查 {{date(s.checked)}}<span v-if="s.evidence?.title"> · 样本：{{s.evidence.title}}</span><span v-if="s.evidence?.full_decode"> · 完整解码 {{duration(s.evidence.duration)}}</span></p>
      <div class="button-row"><button class="secondary compact" :disabled="!!busy" @click="inspect(s)"><RefreshCw :size="15"/>检查兼容性</button><button class="secondary compact" :disabled="!!busy||!sample" @click="probe(s)">验证整曲</button></div>
      <p class="fine source-fingerprint">文件指纹 {{s.id.slice(0,16)}} · {{s.enabled?'已加入任务来源':'未启用自动获取'}}</p>
    </div>
  </article>
  <article class="panel settings-panel full-width"><h2>歌词补充来源</h2><p class="muted">按显示顺序补齐缺失歌词，已有歌词与人工锁定内容优先保留。</p><div class="button-row"><button v-for="p in [{id:'platform',name:'歌曲原平台'},{id:'lrclib',name:'LRCLIB 公共歌词库'}]" :key="p.id" class="secondary" :aria-pressed="settings.lyric_providers.includes(p.id)" :disabled="!!busy" @click="toggleLyrics(p.id,settings)">{{settings.lyric_providers.includes(p.id)?'已启用':'已停用'}} · {{p.name}}</button></div><p class="fine">当前顺序：{{settings.lyric_providers.map((x:string)=>x==='platform'?'原平台':'LRCLIB').join(' → ')||'全部停用'}}。LRCLIB 需歌名、歌手、专辑和时长匹配；只有纯文本时会明确提示缺少同步歌词。</p></article>
</template>

<style scoped>
.source-import{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}.source-import input[type=url],.source-import input[type=text],.source-import input:not([type]){flex:1;min-width:160px}.source-manager input,.source-manager select{max-width:100%;border:1px solid var(--line,#e2e5eb);border-radius:10px;padding:11px;background:var(--paper,#fff);font:inherit;min-width:0}.source-sample{padding:18px 0;border-top:1px solid var(--line,#e2e5eb)}.source-sample>select{width:100%}.source-card{border-top:1px solid var(--line,#e2e5eb);padding:20px 0 4px}.source-card h3{margin:0}.source-card small{font-weight:400;color:var(--muted,#657080)}.source-capabilities{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.source-feedback{display:flex;gap:8px;align-items:center}.source-fingerprint{overflow-wrap:anywhere}@media(max-width:600px){.source-import input[type=url]{flex-basis:100%}.source-card .section-title{align-items:flex-start;gap:12px}.source-card .section-title h3{font-size:17px}.source-card .badge{white-space:normal}}
</style>
