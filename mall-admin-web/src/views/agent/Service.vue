<template>
  <main class="reception">
    <header>
      <div>
        <h1>客服工作台</h1>
        <p>排队按申请时间排序。在线状态失效后，已领取接待仍归原客服负责。</p>
      </div>
      <button :disabled="busy" @click="act(() => refresh())">刷新</button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section v-if="self" class="presence">
      <strong>{{ self.display_name }}</strong
      ><span
        >{{ presenceLabels[self.presence] }} · 接待 {{ self.active_count }}/{{
          self.capacity
        }}</span
      >
      <button
        v-for="mode in modes"
        :key="mode"
        :disabled="busy || !can('agent:presence:write')"
        @click="setPresence(mode)"
      >
        {{ presenceLabels[mode] }}
      </button>
      <template v-if="can('agent:staff:manage')"
        ><input v-model="profileName" aria-label="接待名称" maxlength="80" /><input
          v-model.number="profileCapacity"
          aria-label="接待容量"
          type="number"
          min="1"
          max="20"
        /><button :disabled="busy" @click="saveProfile">保存接待资料</button></template
      >
    </section>
    <nav aria-label="接待列表">
      <button
        v-for="tab in tabs"
        :key="tab.value"
        :class="{ selected: bucket === tab.value }"
        :disabled="busy"
        @click="changeBucket(tab.value)"
      >
        {{ tab.label }}</button
      ><input
        v-model="query"
        placeholder="搜索会话标题"
        aria-label="搜索会话标题"
        @keyup.enter="act(() => refresh())"
      /><button :disabled="busy" @click="act(() => refresh())">搜索</button>
    </nav>
    <div class="columns">
      <aside>
        <p v-if="!items.length">当前列表暂无会话</p>
        <button
          v-for="c in items"
          :key="c.id"
          class="conversation"
          :class="{ selected: selected === c.id }"
          @click="select(c.id)"
        >
          <strong>{{ c.title }}</strong
          ><span>{{ stateLabels[c.state] }} · {{ c.unread_count }} 条未读</span
          ><small>{{ new Date(c.updated_at).toLocaleString() }}</small>
        </button>
        <button v-if="nextCursor" :disabled="busy" @click="more">加载更多会话</button>
      </aside>
      <article v-if="snapshot">
        <header>
          <div>
            <h2>{{ snapshot.conversation.title }}</h2>
            <p>
              {{ stateLabels[snapshot.conversation.state]
              }}<span v-if="snapshot.conversation.assigned_staff">
                · {{ snapshot.conversation.assigned_staff.display_name }}</span
              >
            </p>
          </div>
          <button
            v-if="snapshot.conversation.state === 'WAITING_HUMAN' && can('agent:handoff:claim')"
            :disabled="busy || self?.presence !== 'ONLINE'"
            @click="claim"
          >
            领取接待
          </button>
        </header>
        <button v-if="historyCursor" :disabled="busy" @click="older">加载更早消息</button>
        <section class="messages" aria-live="polite">
          <div v-for="m in messages" :key="m.id" :class="['message', m.sender_kind.toLowerCase()]">
            <small
              >{{ m.sender_kind === 'MEMBER' ? '会员' : m.sender_display_name }} ·
              {{ new Date(m.created_at).toLocaleString() }} ·
              {{ messageStatus[m.status] || m.status }}</small
            >
            <p>{{ m.text }}</p>
            <StaffMessageContent
              :blocks="m.blocks"
              :can-read-images="!!can('agent:attachment:read')"
            />
          </div>
        </section>
        <template v-if="mine">
          <StaffComposer
            ref="composer"
            :key="selected"
            :conversation-id="selected"
            :disabled="busy || !can('agent:conversation:reply')"
            :business-allowed="!!can('agent:business:read')"
            :upload-allowed="!!can('agent:attachment:upload')"
            @submit="send"
          />
          <section v-if="can('agent:conversation:note')" class="notes">
            <h3>内部备注 · 仅员工可见</h3>
            <p v-for="n in notes" :key="n.id">
              {{ n.body }} <small>{{ new Date(n.created_at).toLocaleString() }}</small>
            </p>
            <button v-if="notesCursor" :disabled="busy" @click="olderNotes">加载更早备注</button
            ><textarea
              v-model="noteDraft"
              maxlength="5000"
              aria-label="内部备注"
              placeholder="备注不会发送给会员或模型"
            /><button :disabled="busy || !noteDraft.trim()" @click="addNote">保存内部备注</button>
          </section>
          <section class="finish">
            <label>转交 / 结束原因<input v-model="reason" maxlength="500" /></label
            ><template v-if="can('agent:handoff:transfer')"
              ><select v-model="target" aria-label="接收客服">
                <option value="">选择在线且有容量的客服</option>
                <option v-for="s in targets" :key="s.id" :value="s.id">
                  {{ s.display_name }}（{{ s.active_count }}/{{ s.capacity }}）
                </option></select
              ><button :disabled="busy || !target || !reason.trim()" @click="transfer">
                转交接待
              </button></template
            ><button
              v-if="can('agent:handoff:close')"
              :disabled="busy || !reason.trim()"
              @click="close"
            >
              结束接待与会话
            </button>
          </section>
        </template>
        <p v-else-if="snapshot.conversation.state === 'CLOSED'">会话已结束，历史消息仅供查看。</p>
      </article>
      <article v-else class="empty">从左侧选择会话；领取后可向会员回复。</article>
    </div>
    <section v-if="can('agent:audit:read')" class="notes">
      <h3>管理操作审计</h3>
      <input
        v-model="auditResource"
        maxlength="120"
        placeholder="资源编号（选填）"
        aria-label="审计资源编号"
      />
      <input
        v-model="auditRequest"
        maxlength="36"
        placeholder="请求UUID（选填）"
        aria-label="审计请求编号"
      />
      <button :disabled="busy" @click="act(() => loadAudits(false), false)">查询审计</button>
      <p v-for="record in audits" :key="record.id">
        {{ new Date(record.created_at).toLocaleString() }} · {{ record.actor_kind }}
        {{ record.actor_id }} · {{ record.summary }} · {{ record.resource_id }} · 请求
        {{ record.request_id }}
      </p>
      <button v-if="auditCursor" :disabled="busy" @click="act(() => loadAudits(true), false)">
        加载更多审计
      </button>
      <p>
        普通审计权限查看本人操作；同时拥有全量历史权限的主管可查看全部摘要。原文、令牌和核对凭据不展示。
      </p>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { receptionApi as api, requestKey, streamStaffEvents } from '@/apis/agent'
import type {
  Conversation,
  Snapshot,
  Staff,
  Message,
  Note,
  Audit,
  StaffMessageInput,
} from '@/apis/agent-contracts'
import StaffComposer from './StaffComposer.vue'
import StaffMessageContent from './StaffMessageContent.vue'

const modes = ['ONLINE', 'BUSY', 'OFFLINE'] as const
const presenceLabels = { ONLINE: '在线', BUSY: '忙碌', OFFLINE: '离线' }
const messageStatus: Record<string, string> = {
  ACCEPTED: '已接收',
  GENERATING: '回复中',
  COMPLETED: '已发送',
  STOPPED: '已停止',
  FAILED: '失败',
  INTERRUPTED: '已中断',
}
const stateLabels = {
  AI_ACTIVE: 'AI 咨询',
  WAITING_HUMAN: '等待人工',
  HUMAN_ACTIVE: '人工接待中',
  CLOSED: '已结束',
}
const tabs = [
  { value: 'WAITING', label: '等待接待' },
  { value: 'MINE', label: '我的接待' },
  { value: 'HISTORY', label: '历史接待' },
]
const bucket = ref('WAITING'),
  query = ref(''),
  selected = ref(''),
  error = ref('')
const items = ref<Conversation[]>([]),
  nextCursor = ref<string | null>(null)
const snapshot = ref<Snapshot | null>(null),
  messages = ref<Message[]>([]),
  historyCursor = ref<string | null>(null)
const staff = ref<Staff[]>([]),
  notes = ref<Note[]>([]),
  notesCursor = ref<string | null>(null)
const identity = ref<{ id: string; permissions: string[] }>(),
  self = computed(() => staff.value.find(s => s.id === identity.value?.id))
const profileName = ref(''),
  profileCapacity = ref(3),
  busy = ref(false)
const composer = ref<InstanceType<typeof StaffComposer>>()
const noteDraft = ref(''),
  reason = ref(''),
  target = ref('')
const can = (permission: string) => identity.value?.permissions.includes(permission)
const mine = computed(
  () =>
    snapshot.value?.conversation.state === 'HUMAN_ACTIVE' &&
    snapshot.value.conversation.assigned_staff?.id === identity.value?.id,
)
const targets = computed(() =>
  staff.value.filter(
    s => s.id !== identity.value?.id && s.presence === 'ONLINE' && s.active_count < s.capacity,
  ),
)
const pending = new Map<string, string>()
const audits = ref<Audit[]>([]),
  auditResource = ref(''),
  auditRequest = ref(''),
  auditCursor = ref<string | null>(null)
let streamController: AbortController | undefined,
  streamCursor = '0',
  eventRefreshing = false
const key = (operation: string, body: unknown) => {
  const fingerprint = operation + ':' + JSON.stringify(body)
  if (!pending.has(fingerprint)) pending.set(fingerprint, requestKey())
  return pending.get(fingerprint)!
}
let expanded = false
let live = true,
  polling = false,
  timer: ReturnType<typeof setInterval> | undefined,
  heartbeatTimer: ReturnType<typeof setTimeout> | undefined

async function loadSnapshot(id: string, reset = false) {
  const result = await api.snapshot(id)
  if (
    !live ||
    selected.value !== id ||
    (!reset && snapshot.value && result.conversation.version < snapshot.value.conversation.version)
  )
    return
  reset = reset || (!!snapshot.value && result.conversation.control_epoch !== snapshot.value.conversation.control_epoch)
  snapshot.value = result
  items.value = items.value.map(item => (item.id === id ? result.conversation : item))
  if (reset) {
    messages.value = result.recent_messages
    historyCursor.value = result.history_cursor
  } else {
    const merged = new Map(messages.value.map(m => [m.id, m]))
    result.recent_messages.forEach(m => merged.set(m.id, m))
    messages.value = [...merged.values()].sort(
      (a, b) => a.seq.length - b.seq.length || a.seq.localeCompare(b.seq),
    )
  }
  if (can('agent:conversation:note')) {
    const page = await api.notes(id)
    if (live && selected.value === id) {
      if (reset || !notes.value.length) {
        notes.value = page.items
        notesCursor.value = page.next_cursor
      } else {
        const cutoff = page.retention_cutoff ? Date.parse(page.retention_cutoff) : 0
        const merged = new Map(notes.value.filter(n => Date.parse(n.created_at) > cutoff && (!n.retention_until || Date.parse(n.retention_until) > Date.now())).map(n => [n.id, n]))
        page.items.forEach(n => merged.set(n.id, n))
        notes.value = [...merged.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
      }
    }
  }
  const completed = result.recent_messages.filter(message => message.status !== 'GENERATING')
  const lastSeq = completed[completed.length - 1]?.seq || '0'
  if (BigInt(lastSeq) > BigInt(result.read_position.last_seq)) {
    const position = await api.read(id, lastSeq, key('read', { id, lastSeq }))
    if (live && selected.value === id) {
      items.value = items.value.map(item =>
        item.id === id ? { ...item, unread_count: position.unread_count } : item,
      )
    }
  }
}

async function loadAudits(more: boolean) {
  const page = await api.audits(
    auditResource.value.trim(),
    auditRequest.value.trim(),
    more ? auditCursor.value || '' : '',
  )
  audits.value = more ? [...audits.value, ...page.items] : page.items
  auditCursor.value = page.next_cursor
}

async function followEvents() {
  const controller = new AbortController()
  streamController?.abort()
  streamController = controller
  while (live && !controller.signal.aborted) {
    try {
      await streamStaffEvents(streamCursor, controller.signal, async event => {
        streamCursor = event.event_id
        if (!live || controller.signal.aborted || eventRefreshing || busy.value) return
        if (!['read.updated', 'message.delta'].includes(event.type)) {
          eventRefreshing = true
          try {
            await refresh(true)
          } finally {
            eventRefreshing = false
          }
        }
      })
    } catch (reason) {
      if (controller.signal.aborted || !live) return
      if (reason instanceof Error && reason.message === 'EVENT_CURSOR_EXPIRED')
        await refresh(true, true)
    }
    if (!controller.signal.aborted) await new Promise(resolve => setTimeout(resolve, 1500))
  }
}
async function refresh(preservePages = false, resetEventCursor = false) {
  if (!identity.value || !live) return
  const tab = bucket.value,
    search = query.value
  {
    const page = await api.list(tab, search)
    if (!live || bucket.value !== tab || query.value !== search) return
    if (streamCursor === '0' || resetEventCursor) streamCursor = page.event_cursor
    if (preservePages && expanded) {
      const merged = new Map(items.value.map(item => [item.id, item]))
      page.items.forEach(item => merged.set(item.id, item))
      items.value = [...merged.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    } else {
      items.value = page.items
      nextCursor.value = page.next_cursor
    }
    if (!preservePages) expanded = false
  }
  if (can('agent:staff:read')) {
    const all: Staff[] = []
    let cursor = ''
    do {
      const page = await api.staff(cursor)
      all.push(...page.items)
      cursor = page.next_cursor || ''
    } while (cursor && live)
    if (live) staff.value = all
  }
  if (selected.value) await loadSnapshot(selected.value)
}
async function act(operation: () => Promise<unknown>, refreshAfter = true) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await operation()
    if (refreshAfter) await refresh()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作失败'
    await refresh().catch(() => {})
  } finally {
    busy.value = false
  }
}
async function select(id: string) {
  if (busy.value) return
  selected.value = id
  snapshot.value = null
  messages.value = []
  notes.value = []
  noteDraft.value = ''
  reason.value = ''
  await act(() => loadSnapshot(id, true))
}
async function changeBucket(value: string) {
  if (busy.value) return
  bucket.value = value
  selected.value = ''
  snapshot.value = null
  await act(refresh)
}
async function more() {
  if (!nextCursor.value) return
  const cursor = nextCursor.value
  await act(async () => {
    const page = await api.list(bucket.value, query.value, cursor)
    const merged = new Map(items.value.map(c => [c.id, c]))
    page.items.forEach(c => merged.set(c.id, c))
    items.value = [...merged.values()]
    nextCursor.value = page.next_cursor
    expanded = true
  }, false)
}
async function older() {
  if (!selected.value || !historyCursor.value) return
  const id = selected.value,
    cursor = historyCursor.value
  await act(async () => {
    const page = await api.messages(id, cursor)
    if (selected.value === id) {
      const known = new Set(messages.value.map(m => m.id))
      messages.value = [...page.items.filter(m => !known.has(m.id)), ...messages.value]
      historyCursor.value = page.next_cursor
    }
  }, false)
}
async function olderNotes() {
  if (!selected.value || !notesCursor.value) return
  const id = selected.value
  const page = await api.notes(id, notesCursor.value)
  if (selected.value === id) {
    notes.value.push(...page.items)
    notesCursor.value = page.next_cursor
  }
}
async function claim() {
  const h = snapshot.value?.handoff
  if (h) await act(() => api.claim(h, key('claim', h)))
}
async function send(body: Omit<StaffMessageInput, 'client_message_id'>) {
  const id = selected.value
  await act(async () => {
    await api.send(id, body, key('send', { id, body }))
    pending.delete('send:' + JSON.stringify({ id, body }))
    if (selected.value === id) composer.value?.reset()
  })
}
async function addNote() {
  const id = selected.value,
    body = noteDraft.value.trim()
  await act(async () => {
    await api.note(id, body, key('note', { id, body }))
    pending.delete('note:' + JSON.stringify({ id, body }))
    if (selected.value === id) noteDraft.value = ''
  })
}
async function transfer() {
  const h = snapshot.value?.handoff
  if (h)
    await act(() =>
      api.transfer(
        h,
        target.value,
        reason.value.trim(),
        key('transfer', { h, target: target.value, reason: reason.value.trim() }),
      ),
    )
}
async function close() {
  const h = snapshot.value?.handoff
  if (h)
    await act(() =>
      api.close(h, reason.value.trim(), key('close', { h, reason: reason.value.trim() })),
    )
}
async function setPresence(mode: Staff['presence']) {
  const s = self.value
  if (s)
    await act(() => api.presence(s, mode, key('presence', { id: s.id, version: s.version, mode })))
}
async function saveProfile() {
  const s = self.value
  if (s)
    await act(() =>
      api.profile(
        s,
        profileName.value.trim(),
        profileCapacity.value,
        key('profile', {
          version: s.version,
          name: profileName.value.trim(),
          capacity: profileCapacity.value,
        }),
      ),
    )
}
async function heartbeat() {
  if (!live) return
  if (!busy.value && can('agent:presence:write')) {
    try {
      const result = await api.heartbeat()
      if (live) staff.value = [...staff.value.filter(s => s.id !== result.id), result]
    } catch (e) {
      error.value = e instanceof Error ? e.message : '在线续约失败，请刷新'
    }
  }
  if (live) heartbeatTimer = setTimeout(heartbeat, 20000)
}
onMounted(async () => {
  await act(async () => {
    identity.value = await api.me()
    if (!can('agent:handoff:claim')) bucket.value = 'MINE'
    await refresh()
    profileName.value = self.value?.display_name || ''
    profileCapacity.value = self.value?.capacity || 3
  })
  if (!live) return
  void followEvents()
  timer = setInterval(async () => {
    if (!live || busy.value || polling) return
    polling = true
    try {
      await refresh(true)
    } catch (e) {
      error.value = e instanceof Error ? e.message : '同步失败'
    } finally {
      polling = false
    }
  }, 15000)
  heartbeatTimer = setTimeout(heartbeat, 20000)
})
onUnmounted(() => {
  live = false
  streamController?.abort()
  clearInterval(timer)
  clearTimeout(heartbeatTimer)
})
</script>

<style scoped>
.reception {
  padding: 24px;
  color: #253044;
}
header,
nav,
.presence,
.finish {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
header {
  justify-content: space-between;
}
h1 {
  font-size: 24px;
  margin: 0;
}
h2 {
  font-size: 18px;
}
p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
}
header p {
  color: #64748b;
}
button,
input,
select {
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  background: white;
  padding: 9px 12px;
  color: inherit;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
.presence,
nav {
  margin: 18px 0;
}
.selected {
  background: #e8f1ff;
  border-color: #668fd6;
}
.columns {
  display: grid;
  grid-template-columns: 280px 1fr;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  min-height: 520px;
}
aside {
  padding: 14px;
  border-right: 1px solid #e2e8f0;
}
.conversation {
  width: 100%;
  text-align: left;
  display: grid;
  gap: 6px;
  margin-bottom: 10px;
}
article {
  padding: 22px;
  min-width: 0;
}
.messages {
  max-height: 530px;
  overflow-y: auto;
  margin: 16px 0;
}
.message {
  padding: 12px;
  background: #f1f5f9;
  border-radius: 8px;
  margin: 10px 0;
}
.message.staff {
  background: #e8f1ff;
}
small,
.block {
  color: #64748b;
  font-size: 12px;
}
label {
  display: block;
  margin: 12px 0;
}
textarea {
  display: block;
  width: 100%;
  box-sizing: border-box;
  min-height: 80px;
  padding: 12px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  margin-top: 8px;
  font: inherit;
}
.notes {
  margin-top: 24px;
  padding: 16px;
  background: #fff9eb;
}
.finish {
  margin-top: 20px;
  border-top: 1px solid #e2e8f0;
}
.error {
  padding: 12px;
  color: #b42342;
  background: #fff0f3;
}
.empty {
  color: #64748b;
}
@media (max-width: 900px) {
  .columns {
    grid-template-columns: 1fr;
  }
  aside {
    border-right: 0;
    border-bottom: 1px solid #e2e8f0;
  }
}
</style>
