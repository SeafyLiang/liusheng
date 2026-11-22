<script setup lang="ts">
import {onMounted, ref} from 'vue'
import {api} from './api'
const emit=defineEmits<{select:[url:string]}>()
const items=ref<any[]>([]), error=ref('')
onMounted(async()=>{try{items.value=(await api('/chart-sources')).items}catch(e){error.value=(e as Error).message}})
</script>
<template>
<section class="chart-subscriptions">
  <h3>热门榜单订阅</h3><p class="fine">首次获取缺少的歌曲，之后每周一 04:00 更新；下榜歌曲仍保留。</p>
  <p v-if="error" class="inline-error">{{error}}</p>
  <div v-for="item in items" :key="item.platform" class="chart-option">
    <div><strong>{{item.name}}</strong><small>{{item.blocked || item.last_error || (item.source_kind==='editorial'?'官方精选歌单':'官方榜单')}}</small></div>
    <button class="secondary" :disabled="!!item.blocked || item.subscribed" @click="emit('select',item.url)">{{item.subscribed?'已订阅':item.blocked?'待接入':'预览'}}</button>
  </div>
</section>
</template>
<style scoped>
.chart-subscriptions{margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #8883}.chart-option{display:flex;align-items:center;gap:12px;justify-content:space-between;margin:12px 0}.chart-option div{min-width:0}.chart-option small{display:block;opacity:.65;margin-top:4px;font-size:12px}.chart-option button{flex-shrink:0}
</style>
