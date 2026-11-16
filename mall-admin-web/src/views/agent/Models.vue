<template>
  <main class="models">
    <header>
      <h1>模型与提示词</h1>
      <button :disabled="busy" @click="act(load)">刷新配置</button>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <p>新请求使用当前活动配置；排队或生成中的请求保留提交时的模型与提示词版本。</p>
    <section v-if="can('agent:model:read')">
      <h2>模型配置</h2>
      <table>
        <thead>
          <tr>
            <th>能力</th>
            <th>配置与版本</th>
            <th>模型</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="profile in profiles" :key="profile.id">
            <td>{{ profile.capability }}</td>
            <td>{{ profile.name }} · v{{ profile.version }}</td>
            <td>{{ profile.model_name }}</td>
            <td>
              {{ profile.is_active ? '活动' : profile.enabled ? '已启用候选' : '候选' }} ·
              {{ profile.secret_configured ? '凭据已配置' : '缺少服务端凭据' }}
            </td>
            <td>
              <button :disabled="busy" @click="selectProfile(profile)">查看 / 编辑</button
              ><button v-if="can('agent:model:write')" :disabled="busy" @click="clone(profile)">
                复制为候选
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <button v-if="profileCursor" :disabled="busy" @click="act(moreProfiles)">更多模型配置</button>
      <button v-if="can('agent:model:write')" :disabled="busy" @click="newProfile">
        新增候选配置
      </button>
      <section v-if="editing" class="editor">
        <h3>{{ selectedProfile ? selectedProfile.name : '新增候选配置' }}</h3>
        <p v-if="selectedProfile?.is_active">活动配置保留原值。可复制为候选，验收后再切换。</p>
        <label>名称<input v-model="form.name" maxlength="100" :disabled="locked" /></label>
        <label
          >能力<select
            v-model="form.capability"
            :disabled="locked || !!selectedProfile"
            @change="useRegistry"
          >
            <option
              v-for="entry in registry?.entries || []"
              :key="entry.capability"
              :value="entry.capability"
            >
              {{ entry.capability }}
            </option>
          </select></label
        >
        <label
          >模型标识<input v-model="form.model_name" maxlength="160" :disabled="locked"
        /></label>
        <p>
          端点 {{ form.endpoint_ref }} · 供应商 {{ form.provider }} · 只使用服务端登记的凭据引用
        </p>
        <label>公开参数 JSON<textarea v-model="optionsText" :disabled="locked" rows="5" /></label>
        <p>
          对话：max_output_tokens、timeout_seconds；向量：dimension；各能力可设
          daily_limit_units。语音音色当前为 Cherry，图片精度为 auto。
        </p>
        <label v-if="selectedProfile && !selectedProfile.is_active"
          ><input type="checkbox" v-model="enabled" :disabled="locked" />启用候选</label
        >
        <button
          v-if="can('agent:model:write') && !selectedProfile?.is_active"
          :disabled="busy || !form.name.trim() || !form.model_name.trim()"
          @click="saveProfile"
        >
          保存候选版本
        </button>
        <template v-if="selectedProfile">
          <button
            v-if="can('agent:model:write')"
            :disabled="busy || !selectedProfile.secret_configured"
            @click="testProfile"
          >
            运行兼容测试（调用真实模型，计费用量）
          </button>
          <p v-if="currentJob">
            测试 {{ currentJob.id }} · {{ currentJob.state }} · {{ currentJob.error_code || ''
            }}<button
              v-if="['QUEUED', 'RUNNING'].includes(currentJob.state)"
              :disabled="busy"
              @click="act(refreshJob)"
            >
              刷新测试结果
            </button>
          </p>
          <button
            v-if="can('agent:model:activate') && !selectedProfile.is_active"
            :disabled="busy || currentJob?.state !== 'SUCCEEDED'"
            @click="activateProfile"
          >
            切换为活动配置
          </button>
          <h4>不可变历史版本</h4>
          <p v-for="version in versions" :key="version.version_no">
            v{{ version.version_no }} · {{ version.model_name }} · {{ version.compatibility_status
            }}<button
              v-if="can('agent:model:write')"
              :disabled="busy"
              @click="cloneVersion(version)"
            >
              从此版本创建候选
            </button>
          </p>
          <button v-if="versionCursor" :disabled="busy" @click="act(moreVersions)">
            更多历史版本
          </button>
        </template>
      </section>
    </section>
    <section v-if="can('agent:prompt:read')">
      <h2>提示词版本</h2>
      <select
        v-model="promptKey"
        aria-label="提示词职责"
        :disabled="busy"
        @change="act(loadPrompts)"
      >
        <option v-for="key in promptKeys" :key="key" :value="key">{{ key }}</option>
      </select>
      <button
        v-if="can('agent:prompt:write')"
        :disabled="busy"
        @click="editPrompt('')"
      >
        新建草稿
      </button>
      <div v-for="prompt in prompts" :key="prompt.id" class="prompt">
        <strong>{{ prompt.prompt_key }} · v{{ prompt.version_no }} · {{ prompt.state }}</strong>
        <p>{{ prompt.change_note }}</p>
        <details>
          <summary>查看完整内容</summary>
          <pre>{{ prompt.content }}</pre>
        </details>
        <button
          v-if="can('agent:prompt:write')"
          :disabled="busy"
          @click="editPrompt(prompt.content)"
        >
          复制为草稿
        </button>
        <button
          v-if="can('agent:prompt:publish') && prompt.state !== 'PUBLISHED'"
          :disabled="busy"
          @click="publishPrompt(prompt)"
        >
          {{ prompt.state === 'RETIRED' ? '恢复此版本' : '发布此草稿' }}
        </button>
      </div>
      <button v-if="promptCursor" :disabled="busy" @click="act(morePrompts)">更多提示词版本</button>
      <section v-if="promptEditor" class="editor">
        <h3>{{ promptKey }} 新草稿</h3>
        <textarea v-model="promptContent" maxlength="30000" rows="15" /><label
          >变更说明<input v-model="changeNote" maxlength="500" /></label
        ><button :disabled="busy || !promptContent.trim()" @click="savePrompt">保存草稿</button
        ><button :disabled="busy" @click="promptEditor = false">取消编辑</button>
      </section>
    </section>
  </main>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { modelApi as api, requestKey } from '@/apis/agent'
import type {
  Job,
  ModelProfile,
  ModelProfileCreate,
  ModelProfileVersion,
  ModelRegistry,
  Prompt,
} from '@/apis/agent-contracts'
const identity = ref<{ id: string; permissions: string[] }>(),
  busy = ref(false),
  error = ref('')
const can = (permission: string) => !!identity.value?.permissions.includes(permission)
const profiles = ref<ModelProfile[]>([]),
  profileCursor = ref<string | null>(null),
  registry = ref<ModelRegistry>()
const selectedProfile = ref<ModelProfile>(),
  editing = ref(false),
  versions = ref<ModelProfileVersion[]>([]),
  versionCursor = ref<string | null>(null)
const form = ref<ModelProfileCreate>({
  name: '',
  capability: 'DIALOG',
  provider: 'gpt',
  model_name: '',
  endpoint_ref: 'gpt.responses.v1',
  secret_ref: 'local-env:dialog.v1',
  public_options: {},
})
const optionsText = ref('{}'),
  enabled = ref(false),
  currentJob = ref<Job>()
const locked = computed(
  () => busy.value || !can('agent:model:write') || !!selectedProfile.value?.is_active,
)
const promptKeys = ['readonly-dialog', 'workflow-router', 'knowledge-answer', 'fact-selector'],
  promptKey = ref('readonly-dialog')
const prompts = ref<Prompt[]>([]),
  promptCursor = ref<string | null>(null),
  promptEditor = ref(false),
  promptContent = ref(''),
  changeNote = ref('')
const pending = new Map<string, string>()
function key(operation: string, body: unknown) {
  const id = operation + JSON.stringify(body)
  if (!pending.has(id)) pending.set(id, requestKey())
  return pending.get(id)!
}
let live = true,
  timer: ReturnType<typeof setTimeout> | undefined
async function act(work: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await work()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '操作失败'
  } finally {
    busy.value = false
  }
}
function editPrompt(content: string) {
  promptEditor.value = true
  promptContent.value = content
  changeNote.value = ''
}
function useRegistry() {
  const entry = registry.value?.entries.find(entry => entry.capability === form.value.capability)
  if (!entry) return
  form.value.provider = entry.provider
  form.value.endpoint_ref = entry.endpoint_ref
  form.value.secret_ref = entry.secret_ref
  if (!selectedProfile.value)
    optionsText.value = JSON.stringify(
      entry.capability === 'EMBEDDING'
        ? { dimension: 1024 }
        : entry.capability === 'DIALOG'
          ? { max_output_tokens: 1536 }
          : entry.capability === 'TTS'
            ? { voice: 'Cherry' }
            : entry.capability === 'VISION'
              ? { detail: 'auto' }
              : {},
      null,
      2,
    )
}
function newProfile() {
  clearTimeout(timer)
  currentJob.value = undefined
  selectedProfile.value = undefined
  versions.value = []
  editing.value = true
  form.value = {
    name: '',
    capability: 'DIALOG',
    provider: 'gpt',
    model_name: '',
    endpoint_ref: '',
    secret_ref: '',
    public_options: {},
  }
  useRegistry()
}
function clone(profile: ModelProfile) {
  newProfile()
  form.value = {
    name: profile.name + ' 候选',
    capability: profile.capability,
    provider: profile.provider,
    model_name: profile.model_name,
    endpoint_ref: profile.endpoint_ref,
    secret_ref: '',
    public_options: profile.public_options,
  }
  useRegistry()
  optionsText.value = JSON.stringify(profile.public_options, null, 2)
}
function cloneVersion(version: ModelProfileVersion) {
  newProfile()
  form.value = {
    name: version.name + ' 恢复候选',
    capability: version.capability as ModelProfileCreate['capability'],
    provider: version.provider,
    model_name: version.model_name,
    endpoint_ref: version.endpoint_ref,
    secret_ref: '',
    public_options: version.public_options,
  }
  useRegistry()
  optionsText.value = JSON.stringify(version.public_options, null, 2)
}
async function selectProfile(profile: ModelProfile) {
  await act(async () => {
    clearTimeout(timer)
    currentJob.value = undefined
    selectedProfile.value = profile
    editing.value = true
    form.value = {
      name: profile.name,
      capability: profile.capability,
      provider: profile.provider,
      model_name: profile.model_name,
      endpoint_ref: profile.endpoint_ref,
      secret_ref: '',
      public_options: profile.public_options,
    }
    enabled.value = profile.enabled
    const entry = registry.value?.entries.find(entry => entry.capability === profile.capability)
    form.value.secret_ref = entry?.secret_ref || ''
    optionsText.value = JSON.stringify(profile.public_options, null, 2)
    const page = await api.versions(profile.id)
    versions.value = page.items
    versionCursor.value = page.next_cursor
  })
}
async function load() {
  if (can('agent:model:read')) {
    const page = await api.list()
    profiles.value = page.items
    profileCursor.value = page.next_cursor
    registry.value = await api.registry()
  }
  if (can('agent:prompt:read')) await loadPrompts()
}
async function moreProfiles() {
  const page = await api.list(profileCursor.value || '')
  profiles.value.push(...page.items)
  profileCursor.value = page.next_cursor
}
async function moreVersions() {
  if (!selectedProfile.value) return
  const page = await api.versions(selectedProfile.value.id, versionCursor.value || '')
  versions.value.push(...page.items)
  versionCursor.value = page.next_cursor
}
async function saveProfile() {
  await act(async () => {
    const body = { ...form.value, public_options: JSON.parse(optionsText.value) }
    const profile = selectedProfile.value
      ? await api.update(
          selectedProfile.value.id,
          {
            expected_version: selectedProfile.value.version,
            name: body.name,
            model_name: body.model_name,
            endpoint_ref: body.endpoint_ref,
            secret_ref: body.secret_ref,
            public_options: body.public_options,
            enabled: enabled.value,
          },
          key('update', body),
        )
      : await api.create(body, key('create', body))
    selectedProfile.value = profile
    currentJob.value = undefined
    await load()
    const page = await api.versions(profile.id)
    versions.value = page.items
    versionCursor.value = page.next_cursor
  })
}
async function testProfile() {
  const profile = selectedProfile.value
  if (!profile || !window.confirm('将向真实供应商发送隔离测试请求并计入用量，是否开始？')) return
  await act(async () => {
    currentJob.value = await api.test(
      profile,
      key('test', { id: profile.id, version: profile.version, attempt: currentJob.value?.id }),
    )
    scheduleJob()
  })
}
function scheduleJob() {
  clearTimeout(timer)
  if (live && currentJob.value && ['QUEUED', 'RUNNING'].includes(currentJob.value.state))
    timer = setTimeout(async () => {
      if (!busy.value) await act(refreshJob)
      else scheduleJob()
    }, 2500)
}
async function refreshJob() {
  if (!currentJob.value) return
  currentJob.value = await api.job(currentJob.value.id)
  scheduleJob()
}
async function activateProfile() {
  const profile = selectedProfile.value,
    job = currentJob.value
  if (
    !profile ||
    !job ||
    !window.confirm(`将 ${profile.name} 切换为 ${profile.capability} 的活动配置？`)
  )
    return
  await act(async () => {
    selectedProfile.value = await api.activate(
      profile,
      job,
      key('activate', { profile, job: job.id }),
    )
    await load()
    const page = await api.versions(profile.id)
    versions.value = page.items
    versionCursor.value = page.next_cursor
  })
}
async function loadPrompts() {
  const page = await api.prompts(promptKey.value)
  prompts.value = page.items
  promptCursor.value = page.next_cursor
}
async function morePrompts() {
  const page = await api.prompts(promptKey.value, promptCursor.value || '')
  prompts.value.push(...page.items)
  promptCursor.value = page.next_cursor
}
async function savePrompt() {
  await act(async () => {
    const body = {
      prompt_key: promptKey.value,
      content: promptContent.value,
      change_note: changeNote.value,
    }
    await api.createPrompt(body, key('prompt', body))
    promptEditor.value = false
    await loadPrompts()
  })
}
async function publishPrompt(prompt: Prompt) {
  if (!window.confirm(`发布 ${prompt.prompt_key} v${prompt.version_no}？新请求将使用此版本。`))
    return
  await act(async () => {
    await api.publish(prompt, key('publish', prompt))
    await loadPrompts()
  })
}
onMounted(() =>
  act(async () => {
    identity.value = await api.me()
    await load()
  }),
)
onUnmounted(() => {
  live = false
  clearTimeout(timer)
})
</script>
<style scoped>
.models {
  padding: 24px;
  color: #253044;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
section {
  margin: 24px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  padding: 12px;
  border-bottom: 1px solid #d9e2ef;
  text-align: left;
}
button,
input,
select,
textarea {
  padding: 8px 12px;
  margin: 5px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
}
button {
  cursor: pointer;
  background: white;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
label {
  display: block;
  margin: 10px 0;
}
textarea {
  width: 95%;
  box-sizing: border-box;
  font: 14px/1.6 monospace;
}
.editor,
.prompt {
  padding: 18px;
  border: 1px solid #d9e2ef;
  border-radius: 8px;
  margin: 12px 0;
}
.editor {
  background: #f5f8fd;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
}
[role='alert'] {
  background: #fff0f3;
  color: #b42342;
  padding: 12px;
}
</style>
