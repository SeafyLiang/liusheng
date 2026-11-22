<script setup lang="ts">
import { computed, ref, watch } from 'vue'
const props = withDefaults(defineProps<{items: any[]; rowHeight?: number; label?: string}>(), {rowHeight: 72, label: '歌曲'})
const scroll = ref(0), host = ref<HTMLElement>()
const start = computed(() => Math.max(0, Math.floor(scroll.value / props.rowHeight) - 5))
const rows = computed(() => props.items.slice(start.value, start.value + 30))
watch(() => props.items.length, () => {
  if (scroll.value > Math.max(0, props.items.length * props.rowHeight - 300)) {
    scroll.value = 0; host.value?.scrollTo({top: 0})
  }
})
defineExpose({reset: () => {scroll.value = 0; host.value?.scrollTo({top: 0})}})
</script>
<template>
  <div ref="host" class="virtual-list" role="list" tabindex="0" :aria-label="label" @scroll="scroll=($event.target as HTMLElement).scrollTop">
    <div :style="{height:items.length*rowHeight+'px',position:'relative'}">
      <div :style="{position:'absolute',left:0,right:0,top:start*rowHeight+'px'}">
        <div v-for="(item,i) in rows" :key="item.id" role="listitem" :aria-posinset="start+i+1" :aria-setsize="items.length" :style="{height:rowHeight+'px'}">
          <slot :item="item" :index="start+i" />
        </div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.virtual-list{height:calc(100dvh - 290px);min-height:260px;max-height:900px;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
.virtual-list:focus-visible{outline:2px solid #77a99b;outline-offset:3px}
</style>
