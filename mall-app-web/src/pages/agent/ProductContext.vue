<template>
  <view class="product-context" aria-label="当前咨询商品" role="status">
    <view class="context-copy">
      <text class="context-label">正在咨询当前商品 · 商品编号 {{ productId }}</text>
      <text v-if="name" class="context-name">{{ name }}</text>
      <text v-else class="context-message">{{ status }}</text>
    </view>
    <button v-if="name" size="mini" @click="open">查看商品</button>
  </view>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { agent } from '@/services/agent'
const props = defineProps<{ productId: string }>()
const name = ref(''), status = ref('正在核对商品…')
let revision = 0
watch(() => props.productId, async (id) => {
  const request = ++revision
  name.value = ''
  status.value = '正在核对商品…'
  try {
    if (!/^[1-9][0-9]{0,18}$/.test(id) || BigInt(id) > 9223372036854775807n) throw new Error()
    const product = await agent.product(id)
    if (request !== revision) return
    if (product.id !== id || !product.name) throw new Error()
    name.value = product.name
  } catch {
    if (request === revision) status.value = '当前商品暂不可读取，请刷新或联系人工核实。'
  }
}, { immediate: true })
onBeforeUnmount(() => { revision++ })
function open() {
  if (name.value) uni.navigateTo({ url: `/pages/product/product?id=${encodeURIComponent(props.productId)}` })
}
</script>

<style scoped>
.product-context { display: flex; align-items: center; gap: 12px; padding: 12px 18px; background: #fff7f3; border-bottom: 1px solid #f0e7e2; }
.context-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 4px; }
.context-label, .context-message { font-size: 12px; color: #76665e; }
.context-name { font-size: 13px; color: #382e29; overflow-wrap: anywhere; }
.product-context button { flex-shrink: 0; margin: 0; color: #b94b23; background: #fff; }
</style>
