<template>
  <view class="voice-compose" aria-label="语音转写">
    <button
      v-if="!recording && !starting && !blob"
      size="mini"
      :disabled="disabled || busy"
      @click="start"
      >语音输入</button
    >
    <template v-if="recording">
      <text role="status">录音中 {{ seconds }} / 60秒</text>
      <button size="mini" @click="finish">结束录音</button>
    </template>
    <text v-if="starting" role="status">正在请求麦克风权限…</text>
    <template v-if="blob">
      <text>录音已就绪，转写后可编辑再发送</text>
      <button size="mini" :disabled="busy || disabled" @click="transcribe">{{
        busy ? '正在转写…' : '转写为文字'
      }}</button>
    </template>
    <button v-if="starting || recording || blob" size="mini" @click="cancel">取消语音</button>
    <text v-if="status" role="status">{{ status }}</text>
  </view>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import { agent, agentId, AgentError } from '@/services/agent'
import { startRecording, type Recording } from '@/services/agentAudio'
const props = defineProps<{ disabled: boolean }>()
const emit = defineEmits<{ (e: 'transcribed', text: string): void }>()
const recording = ref(false),
  starting = ref(false),
  busy = ref(false),
  seconds = ref(0),
  status = ref(''),
  blob = ref<Blob | null>(null)
let capture: Recording | undefined, timer: ReturnType<typeof setInterval> | undefined
let generation = 0,
  uploadKey = agentId(),
  transcriptionKey = agentId(),
  attachmentId = ''
async function cancel() {
  generation++
  const old = capture
  capture = undefined
  clearInterval(timer)
  recording.value = starting.value = busy.value = false
  blob.value = null
  attachmentId = ''
  status.value = ''
  uploadKey = agentId()
  transcriptionKey = agentId()
  await old?.cancel()
}
async function start() {
  if (props.disabled || starting.value) return
  const current = generation + 1
  await cancel()
  if (current !== generation) return
  starting.value = true
  try {
    const session = await startRecording(() => {
      void finish()
    })
    if (current !== generation) {
      await session.cancel()
      return
    }
    capture = session
    recording.value = true
    seconds.value = 0
    timer = setInterval(() => {
      seconds.value = Math.min(60, seconds.value + 1)
    }, 1000)
  } catch (error) {
    if (current === generation)
      status.value = error instanceof Error ? error.message : '无法录音，可继续输入文字'
  } finally {
    if (current === generation) starting.value = false
  }
}
async function finish() {
  const old = capture
  if (!old) return
  capture = undefined
  recording.value = false
  clearInterval(timer)
  const current = generation
  try {
    const result = await old.finish()
    if (current === generation) blob.value = result
  } catch (error) {
    if (current === generation)
      status.value = error instanceof Error ? error.message : '录音失败，请重录'
  }
}
async function transcribe() {
  if (!blob.value || busy.value || props.disabled) return
  const current = generation
  busy.value = true
  status.value = ''
  try {
    if (!attachmentId) {
      const attachment = await agent.upload(blob.value, '录音.wav', uploadKey, 'RECORDING')
      if (current !== generation) return
      attachmentId = attachment.id
    }
    const result = await agent.transcribe(attachmentId, transcriptionKey)
    if (current !== generation) return
    emit('transcribed', result.text)
    await cancel()
    status.value = '转写已放入输入框，请核对、编辑后手动发送。'
  } catch (error) {
    if (current === generation) {
      status.value = error instanceof Error ? error.message : '转写失败，可重试或直接输入'
      if (error instanceof AgentError && error.code && error.code !== 'MEDIA_REQUEST_IN_PROGRESS')
        transcriptionKey = agentId()
    }
  } finally {
    if (current === generation) busy.value = false
  }
}
onBeforeUnmount(() => {
  void cancel()
})
</script>

<style scoped>
.voice-compose {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
  color: #606266;
  font-size: 13px;
}
</style>
