<template>
  <div v-for="(block, index) in blocks" :key="index" class="message-block">
    <template v-if="block.type === 'attachment'">
      <a
        v-if="urls[block.attachment.id]"
        :href="urls[block.attachment.id]"
        target="_blank"
        rel="noopener"
        ><img :src="urls[block.attachment.id]" :alt="block.attachment.name"
      /></a>
      <span v-else>{{
        errors[block.attachment.id] || (canReadImages ? '图片加载中…' : '查看图片需要附件读取权限')
      }}</span>
    </template>
    <template v-else-if="block.type === 'product'">
      <strong>商品 · {{ block.product.name }}</strong>
      <p>
        {{ block.product.brand }} · ¥{{ block.product.price_from }} 起 · 编号 {{ block.product.id }}
      </p>
      <p v-for="fact in block.product.facts" :key="fact.name">{{ fact.name }}：{{ fact.value }}</p>
      <small>价格与库存以商城当前详情为准</small>
    </template>
    <template v-else-if="block.type === 'staff_order'">
      <strong>订单 {{ block.staff_order.order_sn }}</strong>
      <p>
        {{ block.staff_order.status_label }} · 实付 ¥{{ block.staff_order.pay_amount ?? '未提供' }}
      </p>
      <p v-for="(line, n) in block.staff_order.lines" :key="n">
        {{ line.name }} × {{ line.quantity }} · {{ line.specification }}
      </p>
      <small
        >发送时的订单摘要 · {{ new Date(block.staff_order.created_at).toLocaleString() }}</small
      >
    </template>
    <small v-else-if="block.type === 'source'">AI 知识来源 · {{ block.source.title }}</small>
  </div>
</template>
<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import type { MessageBlock } from '@/apis/agent-contracts'
import { receptionApi } from '@/apis/agent'
const props = defineProps<{ blocks: MessageBlock[]; canReadImages: boolean }>()
const urls = ref<Record<string, string>>({}),
  errors = ref<Record<string, string>>({})
let controller: AbortController | undefined
watch(
  () => [props.blocks, props.canReadImages],
  async () => {
    controller?.abort()
    const current = new AbortController()
    controller = current
    if (!props.canReadImages) {
      Object.values(urls.value).forEach(URL.revokeObjectURL)
      urls.value = {}
      return
    }
    for (const block of props.blocks) {
      if (block.type !== 'attachment' || urls.value[block.attachment.id]) continue
      const id = block.attachment.id
      try {
        const blob = await receptionApi.image(id, current.signal)
        if (!current.signal.aborted) urls.value[id] = URL.createObjectURL(blob)
      } catch (reason) {
        if (!current.signal.aborted)
          errors.value[id] = reason instanceof Error ? reason.message : '图片读取失败'
      }
    }
  },
  { immediate: true, deep: true },
)
onUnmounted(() => {
  controller?.abort()
  Object.values(urls.value).forEach(URL.revokeObjectURL)
})
</script>
<style scoped>
.message-block {
  margin: 10px 0;
  padding: 12px;
  border: 1px solid #d8e1ed;
  border-radius: 8px;
}
img {
  max-width: 240px;
  max-height: 200px;
  border-radius: 6px;
}
p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
small {
  color: #64748b;
}
</style>
