<template>
  <section class="composer">
    <section v-if="businessAllowed" class="context">
      <h3>业务上下文</h3>
      <button :disabled="disabled || loading" @click="loadContext()">刷新会员与最近订单</button>
      <p v-if="context">
        {{ context.member_display_name }} · {{ context.phone_masked }} · 最近
        {{ context.orders.length }} 笔订单
      </p>
      <label>商品编号 <input v-model="productId" inputmode="numeric" maxlength="19" /></label>
      <button
        :disabled="disabled || loading || !validId(productId)"
        @click="loadContext(productId)"
      >
        读取商品卡片
      </button>
      <label>订单编号 <input v-model="orderId" inputmode="numeric" maxlength="19" /></label>
      <button
        :disabled="disabled || loading || !validId(orderId)"
        @click="loadContext('', orderId)"
      >
        查询该会员订单
      </button>
      <div v-for="product in context?.products || []" :key="'p' + product.id">
        <strong>{{ product.name }} · ¥{{ product.price_from }} 起</strong>
        <button
          :disabled="disabled || cards.length >= 5"
          @click="addCard('PRODUCT', product.id, product.name)"
        >
          附加商品卡片
        </button>
      </div>
      <div v-for="order in context?.orders || []" :key="'o' + order.id">
        <span
          >{{ order.order_sn }} · {{ order.status_label }} · ¥{{
            order.pay_amount ?? '未提供'
          }}</span
        >
        <button
          :disabled="disabled || cards.length >= 5"
          @click="addCard('ORDER', order.id, order.order_sn)"
        >
          附加订单卡片
        </button>
      </div>
    </section>
    <label
      >回复会员<textarea
        v-model="draft"
        maxlength="10000"
        :disabled="disabled"
        placeholder="此内容会发送给会员"
      />
    </label>
    <template v-if="uploadAllowed">
      <label
        >附加图片（最多两张，每张5MB）<input
          type="file"
          accept="image/jpeg,image/png,image/webp"
          :disabled="disabled || uploading || pictures.length >= 2"
          @change="chooseImage"
      /></label>
      <div v-for="picture in pictures" :key="picture.key">
        <span
          >{{ picture.file.name }} ·
          {{ picture.attachment ? '已上传' : picture.error || '上传中…' }}</span
        ><button v-if="picture.error" :disabled="disabled || uploading" @click="upload(picture)">
          重试上传</button
        ><button
          :disabled="disabled || uploading"
          @click="pictures = pictures.filter(p => p !== picture)"
        >
          移除图片
        </button>
      </div>
    </template>
    <p v-for="card in cards" :key="card.kind + card.id">
      {{ card.kind === 'PRODUCT' ? '商品' : '订单' }} · {{ card.label }}
      <button :disabled="disabled" @click="cards = cards.filter(c => c !== card)">移除卡片</button>
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button
      :disabled="
        disabled ||
        uploading ||
        pictures.some(p => !p.attachment) ||
        (!draft.trim() && !pictures.length && !cards.length)
      "
      @click="submit"
    >
      发送回复
    </button>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { receptionApi, requestKey } from '@/apis/agent'
import type { Attachment, StaffBusinessContext, StaffMessageInput } from '@/apis/agent-contracts'
const props = defineProps<{
  conversationId: string
  disabled: boolean
  businessAllowed: boolean
  uploadAllowed: boolean
}>()
const emit = defineEmits<{ submit: [body: Omit<StaffMessageInput, 'client_message_id'>] }>()
const draft = ref(''),
  productId = ref(''),
  orderId = ref(''),
  error = ref(''),
  loading = ref(false)
const context = ref<StaffBusinessContext>()
const cards = ref<{ kind: 'PRODUCT' | 'ORDER'; id: string; label: string }[]>([])
type Picture = {
  key: string
  file: File
  attachment?: Attachment
  error: string
  uploading: boolean
}
const pictures = ref<Picture[]>([]),
  uploading = computed(() => pictures.value.some(p => p.uploading))
let live = true
const validId = (value: string) =>
  /^[1-9][0-9]{0,18}$/.test(value) && BigInt(value) <= BigInt('9223372036854775807')
async function loadContext(product = '', order = '') {
  loading.value = true
  error.value = ''
  try {
    const value = await receptionApi.context(props.conversationId, product, order)
    if (live) context.value = value
  } catch (reason) {
    if (live) error.value = reason instanceof Error ? reason.message : '业务摘要加载失败'
  } finally {
    if (live) loading.value = false
  }
}
function addCard(kind: 'PRODUCT' | 'ORDER', id: string, label: string) {
  if (cards.value.length < 5 && !cards.value.some(card => card.kind === kind && card.id === id))
    cards.value.push({ kind, id, label })
}
async function upload(picture: Picture) {
  picture.uploading = true
  picture.error = ''
  try {
    picture.attachment = await receptionApi.upload(picture.file, picture.key)
  } catch (reason) {
    picture.error = reason instanceof Error ? reason.message : '上传失败'
  } finally {
    picture.uploading = false
  }
}
function chooseImage(event: Event) {
  const input = event.target as HTMLInputElement,
    file = input.files?.[0]
  input.value = ''
  if (!file || pictures.value.length >= 2) return
  if (file.size > 5 * 1024 * 1024) {
    error.value = '图片不能超过5MB'
    return
  }
  const picture: Picture = { key: requestKey(), file, error: '', uploading: true }
  pictures.value.push(picture)
  void upload(pictures.value[pictures.value.length - 1]!)
}
function submit() {
  emit('submit', {
    text: draft.value.trim(),
    attachment_ids: pictures.value.map(p => p.attachment!.id),
    cards: cards.value.map(({ kind, id }) => ({ kind, id })),
  })
}
function reset() {
  draft.value = ''
  pictures.value = []
  cards.value = []
  error.value = ''
}
defineExpose({ reset })
onMounted(() => {
  if (props.businessAllowed) void loadContext()
})
onUnmounted(() => {
  live = false
})
</script>
<style scoped>
.context {
  padding: 16px;
  background: #f1f6ff;
  border-radius: 8px;
  max-height: 360px;
  overflow: auto;
  margin: 12px 0;
}
.context div {
  padding: 8px 0;
  border-bottom: 1px solid #dce5f2;
}
label {
  display: block;
  margin: 12px 0;
}
textarea {
  display: block;
  box-sizing: border-box;
  width: 100%;
  min-height: 90px;
  padding: 12px;
}
button,
input {
  padding: 8px;
  margin: 4px;
}
[role='alert'] {
  color: #b42342;
}
</style>
