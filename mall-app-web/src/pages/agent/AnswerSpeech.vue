<template>
  <view class="answer-speech">
    <button v-if="!active" size="mini" @click="start">播报回答</button>
    <button v-if="active" size="mini" @click="stop">停止播报</button>
    <button v-if="resumeNeeded" size="mini" @click="resume">点击继续播放</button>
    <text v-if="status" role="status">{{ status }}</text>
  </view>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import { agent, agentId } from '@/services/agent'
import { claimPlayback, releasePlayback } from '@/services/agentAudio'
const props = defineProps<{ messageId: string }>()
const active = ref(false),
  resumeNeeded = ref(false),
  status = ref('')
let controller: AbortController | undefined,
  audio: HTMLAudioElement | undefined,
  objectUrl = '',
  part = 0,
  generation = 0
function clearAudio() {
  if (audio) {
    audio.onended = audio.onerror = null
    audio.pause()
    audio.removeAttribute('src')
    audio.load()
    audio = undefined
  }
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
}
function stop() {
  generation++
  controller?.abort()
  controller = undefined
  clearAudio()
  active.value = false
  resumeNeeded.value = false
  status.value = ''
  releasePlayback(stop)
}
async function start() {
  claimPlayback(stop)
  generation++
  part = 0
  active.value = true
  controller = new AbortController()
  await next(generation)
}
async function next(current: number) {
  if (current !== generation || !controller) return
  clearAudio()
  status.value = `正在准备第${part + 1}段播报…`
  const signal = controller.signal
  const timeout = setTimeout(() => controller?.abort(), 95000)
  try {
    const result = await agent.speech(props.messageId, part, agentId(), signal)
    const content = await agent.image(result.attachment.id, signal)
    clearTimeout(timeout)
    if (current !== generation) return
    objectUrl = URL.createObjectURL(content)
    audio = new Audio(objectUrl)
    audio.onended = () => {
      if (current !== generation) return
      if (++part < result.part_count) void next(current)
      else {
        stop()
        status.value = '播报结束'
      }
    }
    audio.onerror = () => {
      if (current === generation) {
        stop()
        status.value = '音频播放失败，可重新播报'
      }
    }
    status.value = `正在播报 ${part + 1} / ${result.part_count}`
    await resume()
  } catch (error) {
    if (current === generation) {
      stop()
      status.value = error instanceof Error ? error.message : '播报失败，可重试'
    }
  } finally {
    clearTimeout(timeout)
  }
}
async function resume() {
  const playing = audio
  if (!playing) return
  try {
    await playing.play()
    if (audio === playing) resumeNeeded.value = false
  } catch {
    if (audio === playing) {
      resumeNeeded.value = true
      status.value = '浏览器暂停了音频，请点击继续播放'
    }
  }
}
onBeforeUnmount(stop)
</script>

<style scoped>
.answer-speech {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  color: #606266;
  margin-top: 8px;
}
</style>
