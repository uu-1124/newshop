<template>
  <view class="answer-feedback" aria-label="评价此回答">
    <text>这条回答有帮助吗？</text>
    <button size="mini" :disabled="busy || loading" :aria-pressed="vote === 1" @click="choose(1)">{{
      vote === 1 ? '已选有帮助' : '有帮助'
    }}</button>
    <button
      size="mini"
      :disabled="busy || loading"
      :aria-pressed="vote === -1"
      @click="choose(-1)"
      >{{ vote === -1 ? '已选需改进' : '需改进' }}</button
    >
    <template v-if="vote">
      <textarea
        v-model="reason"
        :maxlength="500"
        :disabled="busy"
        aria-label="反馈原因"
        placeholder="补充原因（选填，最多500字）"
      />
      <button size="mini" :disabled="busy || loading || loadFailed" @click="save">{{
        busy ? '正在保存…' : '保存评价'
      }}</button>
    </template>
    <text v-if="status" role="status">{{ status }}</text>
    <button v-if="loadFailed" size="mini" :disabled="loading" @click="load">重新读取评价</button>
  </view>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { agent, agentId } from '@/services/agent'
const props = defineProps<{ messageId: string }>()
const vote = ref<-1 | 1 | null>(null),
  reason = ref(''),
  busy = ref(false),
  loading = ref(true),
  loadFailed = ref(false),
  status = ref('')
let alive = true
let pending: { value: string; key: string } | undefined
onBeforeUnmount(() => {
  alive = false
})
async function load() {
  loading.value = true
  loadFailed.value = false
  try {
    const saved = await agent.feedback(props.messageId)
    if (!alive) return
    vote.value = saved?.vote ?? null
    reason.value = saved?.reason ?? ''
    status.value = saved ? '评价已保存，可修改后再次保存。' : ''
  } catch (error) {
    if (alive) {
      loadFailed.value = true
      status.value = error instanceof Error ? error.message : '评价读取失败'
    }
  } finally {
    if (alive) loading.value = false
  }
}
function choose(value: -1 | 1) {
  vote.value = value
  status.value = '选择尚未保存'
}
async function save() {
  if (!vote.value || busy.value || loadFailed.value) return
  busy.value = true
  const value = JSON.stringify([vote.value, reason.value])
  if (pending?.value !== value) pending = { value, key: agentId() }
  try {
    await agent.submitFeedback(props.messageId, vote.value, reason.value, pending.key)
    if (alive) {
      status.value = '评价已保存，感谢反馈。'
      pending = undefined
    }
  } catch (error) {
    if (alive) status.value = error instanceof Error ? error.message : '保存失败，请重试'
  } finally {
    if (alive) busy.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.answer-feedback {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  color: #606266;
  font-size: 13px;
}
textarea {
  width: 100%;
  min-height: 55px;
  padding: 8px;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
}
button[aria-pressed='true'] {
  color: #175b9b;
  background: #e8f2ff;
}
</style>
