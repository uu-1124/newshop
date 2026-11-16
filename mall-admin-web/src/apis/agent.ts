import { useUserStore } from '@/stores/user'
import type {
  KBDocument,
  KBVersion,
  KBPreview,
  Job,
  Conversation,
  Snapshot,
  Handoff,
  Staff,
  Message,
  Note,
  ReadPosition,
  Audit,
  Attachment,
  StaffBusinessContext,
  StaffMessageInput,
  ModelProfile,
  ModelProfileVersion,
  ModelProfileCreate,
  ModelProfileUpdate,
  ModelRegistry,
  Prompt,
  ReturnSummary,
  ReturnAddress,
  StaffReturnPage,
  Logistics,
  LogisticsNode,
  ReturnReviewInput,
  LogisticsNodeUpdate,
  Settings,
  SettingsInput,
  Statistics,
  UnknownModelUsagePage,
  UsageSettlement,
  UsageSettlementResult,
  ActionRecovery,
  ActionRecoveryInput,
  ActionRecoveryResult,
  BackupPage,
  BackupPolicyState,
  BackupPolicyInput,
  ReturnRule,
  ReturnRulePage,
  ReturnRuleInput,
} from './agent-contracts'

type CursorPage<T> = { items: T[]; next_cursor: string | null; has_more: boolean }
type Principal = { id: string; permissions: string[] }

const gateway = String(
  import.meta.env.VITE_AGENT_GATEWAY_URL ||
    new URL(
      String(import.meta.env.VITE_BASE_SERVER_URL || 'http://127.0.0.1:8201'),
      location.origin,
    ).origin,
).replace(/\/$/, '')
const base = (import.meta.env.DEV ? '' : gateway) + '/mall-agent/v1/admin'
export const requestKey = () => crypto.randomUUID()

export const governanceApi = {
  backups: (before = '') => call<BackupPage>(`/backups${before ? '?before=' + encodeURIComponent(before) : ''}`),
  backupPolicy: (body: BackupPolicyInput, key: string) => call<BackupPolicyState>('/backups/policy', 'PUT', body, key),
  createBackup: (version: number, note: string, key: string) => call<Job>('/backups', 'POST', { expected_policy_version: version, maintenance_confirmed: true, note }, key),
  verifyBackup: (id: string, note: string, key: string) => call<Job>('/backups/verify', 'POST', { backup_record_id: id, note }, key),
  actions: (before = '') => call<{items: ActionRecovery[];has_more:boolean;next_before:string|null}>(`/actions/unresolved${before ? '?before='+encodeURIComponent(before) : ''}`),
  resume: (id:string,body:ActionRecoveryInput,key:string) => call<ActionRecoveryResult>(`/actions/${id}/reconcile`,'POST',body,key),
  settings: () => call<Settings>('/settings'),
  save: (body: SettingsInput, key: string) => call<Settings>('/settings', 'PUT', body, key),
  statistics: (from: string, to: string, capability = '') =>
    call<Statistics>(`/statistics?${new URLSearchParams({ from, to, ...(capability ? { capability } : {}) })}`),
  unknown: (before = '') => call<UnknownModelUsagePage>(`/model-usage/unknown${before ? '?before=' + encodeURIComponent(before) : ''}`),
  reconcile: (id: string, body: UsageSettlement, key: string) =>
    call<UsageSettlementResult>(`/model-usage/${id}/reconcile`, 'POST', body, key),
  jobs: (cursor = '') => call<CursorPage<Job>>(`/jobs${cursor ? '?cursor=' + encodeURIComponent(cursor) : ''}`),
  previewRetention: (proposed: Settings['value'], key: string) =>
    call<Job>('/retention/preview', 'POST', { proposed_settings: proposed }, key),
  executeRetention: (preview: string, version: number, key: string) =>
    call<Job>('/retention/execute', 'POST', { preview_job_id: preview, expected_settings_version: version }, key),
}

export const operationsApi = {
  rules: () => call<ReturnRulePage>('/return-rules'),
  publishRule: (body: ReturnRuleInput, key: string) => call<ReturnRule>('/return-rules', 'POST', body, key),
  returns: (stage = '', before = '') =>
    call<StaffReturnPage>(
      `/returns?${new URLSearchParams({ ...(stage ? { stage } : {}), ...(before ? { before } : {}) })}`,
    ),
  detail: (id: string) => call<ReturnSummary>(`/returns/${id}`),
  addresses: () => call<{ items: ReturnAddress[] }>('/return-addresses'),
  review: (id: string, body: ReturnReviewInput, key: string) =>
    call<ReturnSummary>(`/returns/${id}/review`, 'POST', body, key),
  receive: (id: string, version: number, note: string, key: string) =>
    call<ReturnSummary>(`/returns/${id}/receive`, 'POST', { expected_version: version, note }, key),
  refund: (id: string, version: number, note: string, key: string) =>
    call<ReturnSummary>(
      `/returns/${id}/simulate-refund`,
      'POST',
      { expected_version: version, note },
      key,
    ),
  logistics: (id: string) => call<Logistics>(`/orders/${id}/logistics`),
  updateLogistics: (id: string, version: number, carrier: string, tracking: string, key: string) =>
    call<Logistics>(
      `/orders/${id}/logistics`,
      'PUT',
      { expected_version: version, carrier, tracking_no: tracking },
      key,
    ),
  node: (
    id: string,
    version: number,
    occurred: string,
    location: string,
    description: string,
    key: string,
  ) =>
    call<LogisticsNode>(
      `/shipments/${id}/events`,
      'POST',
      { expected_shipment_version: version, occurred_at: occurred, location, description },
      key,
    ),
  editNode: (shipment: string, id: string, body: LogisticsNodeUpdate, key: string) =>
    call<LogisticsNode>(`/shipments/${shipment}/events/${id}`, 'PATCH', body, key),
}

async function call<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  const form = body instanceof FormData
  const response = await fetch(base + path, {
    method,
    cache: 'no-store',
    signal: AbortSignal.timeout(form ? 60000 : 20000),
    headers: {
      Authorization: useUserStore().userInfo.token,
      ...(key ? { 'Idempotency-Key': key } : {}),
      ...(!form && body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  })
  const json = await response.json().catch(() => null)
  if (!response.ok || json?.code !== 'OK') throw new Error(json?.message || '客服服务暂不可用')
  return json.data as T
}

export const receptionApi = {
  context: (id: string, product = '', order = '') =>
    call<StaffBusinessContext>(
      `/conversations/${id}/context?${new URLSearchParams({ ...(product ? { product_id: product } : {}), ...(order ? { order_id: order } : {}) })}`,
    ),
  upload: (file: File, key: string) => {
    const form = new FormData()
    form.append('purpose', 'CHAT_IMAGE')
    form.append('file', file, file.name)
    return call<Attachment>('/attachments', 'POST', form, key)
  },
  image: async (id: string, signal: AbortSignal) => {
    const response = await fetch(`${base}/attachments/${id}/content`, {
      headers: { Authorization: useUserStore().userInfo.token },
      signal,
      cache: 'no-store',
    })
    if (!response.ok) throw new Error('图片已失效或当前无权查看')
    return response.blob()
  },
  read: (id: string, lastSeq: string, key: string) =>
    call<ReadPosition>(`/conversations/${id}/read`, 'POST', { last_seq: lastSeq }, key),
  audits: (resource = '', request = '', cursor = '') =>
    call<CursorPage<Audit>>(
      `/audit-logs?resource_id=${encodeURIComponent(resource)}${request ? '&request_id=' + encodeURIComponent(request) : ''}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  me: () => call<Principal>('/me'),
  list: (bucket: string, query = '', cursor = '') =>
    call<CursorPage<Conversation> & { event_cursor: string }>(
      `/conversations?bucket=${bucket}&query=${encodeURIComponent(query)}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  snapshot: (id: string) => call<Snapshot>(`/conversations/${id}`),
  messages: (id: string, cursor: string) =>
    call<CursorPage<Message>>(`/conversations/${id}/messages?cursor=${encodeURIComponent(cursor)}`),
  staff: (cursor = '') =>
    call<CursorPage<Staff>>(
      `/staff?limit=100${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  notes: (id: string, cursor = '') =>
    call<CursorPage<Note> & { retention_cutoff?: string }>(
      `/conversations/${id}/notes${cursor ? '?cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  claim: (h: Handoff, key: string) =>
    call<Handoff>(`/handoffs/${h.id}/claim`, 'POST', { expected_version: h.version }, key),
  transfer: (h: Handoff, target: string, reason: string, key: string) =>
    call<Handoff>(
      `/handoffs/${h.id}/transfer`,
      'POST',
      { expected_version: h.version, target_staff_id: target, reason },
      key,
    ),
  close: (h: Handoff, reason: string, key: string) =>
    call<Handoff>(`/handoffs/${h.id}/close`, 'POST', { expected_version: h.version, reason }, key),
  send: (id: string, body: Omit<StaffMessageInput, 'client_message_id'>, key: string) =>
    call<Message>(
      `/conversations/${id}/messages`,
      'POST',
      { client_message_id: key, ...body },
      key,
    ),
  note: (id: string, body: string, key: string) =>
    call<Note>(`/conversations/${id}/notes`, 'POST', { client_note_id: key, body }, key),
  presence: (s: Staff, presence: Staff['presence'], key: string) =>
    call<Staff>('/presence', 'PUT', { expected_version: s.version, presence }, key),
  heartbeat: () => call<Staff>('/presence/heartbeat', 'POST', {}, requestKey()),
  profile: (s: Staff, display_name: string, capacity: number, key: string) =>
    call<Staff>(
      `/staff/${s.id}`,
      'PATCH',
      { expected_version: s.version, display_name, capacity },
      key,
    ),
}

export async function streamStaffEvents(
  cursor: string,
  signal: AbortSignal,
  consume: (event: {
    event_id: string
    type: string
    conversation_id: string | null
  }) => Promise<void>,
) {
  const response = await fetch(`${base}/events?after=${encodeURIComponent(cursor)}`, {
    headers: { Authorization: useUserStore().userInfo.token, Accept: 'text/event-stream' },
    signal,
    cache: 'no-store',
  })
  if (!response.ok || !response.body)
    throw new Error(response.status === 410 ? 'EVENT_CURSOR_EXPIRED' : '员工实时连接暂不可用')
  const reader = response.body.getReader(),
    decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      let split: number
      while ((split = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, split)
        buffer = buffer.slice(split + 2)
        const line = frame.split('\n').find(value => value.startsWith('data: '))
        if (line) await consume(JSON.parse(line.slice(6)))
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export const knowledgeApi = {
  me: () => call<Principal>('/me'),
  list: (query = '', cursor = '') =>
    call<CursorPage<KBDocument>>(
      `/kb/documents?query=${encodeURIComponent(query)}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  get: (id: string) => call<KBDocument>(`/kb/documents/${id}`),
  create: (body: unknown, key: string) => call<KBDocument>('/kb/documents', 'POST', body, key),
  update: (id: string, body: unknown, key: string) =>
    call<KBDocument>(`/kb/documents/${id}`, 'PATCH', body, key),
  versions: (id: string, cursor = '') =>
    call<CursorPage<KBVersion>>(
      `/kb/documents/${id}/versions${cursor ? '?cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  newVersion: (id: string, body: unknown, key: string) =>
    call<KBVersion>(`/kb/documents/${id}/versions`, 'POST', body, key),
  version: (id: string) => call<KBVersion>(`/kb/versions/${id}`),
  preview: (id: string) => call<KBPreview>(`/kb/versions/${id}/preview`),
  edit: (version: KBVersion, text: string, key: string, pages?: { page: number; text: string }[]) =>
    call<KBVersion>(
      `/kb/versions/${version.id}/preview`,
      'PATCH',
      { expected_version: version.version, parsed_text: text, ...(pages ? { pages } : {}) },
      key,
    ),
  task: (version: KBVersion, kind: 'parse' | 'build', key: string) =>
    call<Job>(
      `/kb/versions/${version.id}/${kind}`,
      'POST',
      { expected_version: version.version },
      key,
    ),
  job: (id: string) => call<Job>(`/jobs/${id}`),
  retry: (job: Job, key: string) =>
    call<Job>(`/jobs/${job.id}/retry`, 'POST', { expected_version: job.version }, key),
  publish: (doc: KBDocument, version: KBVersion, key: string) =>
    call<KBDocument>(
      `/kb/documents/${doc.id}/publish`,
      'POST',
      { expected_document_version: doc.version, version_id: version.id },
      key,
    ),
  enabled: (doc: KBDocument, enabled: boolean, key: string) =>
    call<KBDocument>(
      `/kb/documents/${doc.id}/enabled`,
      'PUT',
      { expected_version: doc.version, enabled },
      key,
    ),
  upload: (file: File, key: string) => {
    const form = new FormData()
    form.append('purpose', 'KNOWLEDGE')
    form.append('file', file, file.name)
    return call<{ id: string }>('/attachments', 'POST', form, key)
  },
}

export const modelApi = {
  me: () => call<Principal>('/me'),
  registry: () => call<ModelRegistry>('/model-registry'),
  list: (cursor = '') =>
    call<CursorPage<ModelProfile>>(
      `/model-profiles?limit=20${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  versions: (id: string, cursor = '') =>
    call<CursorPage<ModelProfileVersion>>(
      `/model-profiles/${id}/versions?limit=20${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  create: (body: ModelProfileCreate, key: string) =>
    call<ModelProfile>('/model-profiles', 'POST', body, key),
  update: (id: string, body: ModelProfileUpdate, key: string) =>
    call<ModelProfile>(`/model-profiles/${id}`, 'PATCH', body, key),
  test: (profile: ModelProfile, key: string) =>
    call<Job>(
      `/model-profiles/${profile.id}/test`,
      'POST',
      { expected_version: profile.version },
      key,
    ),
  activate: (profile: ModelProfile, job: Job, key: string) =>
    call<ModelProfile>(
      `/model-profiles/${profile.id}/activate`,
      'POST',
      { expected_version: profile.version, compatibility_job_id: job.id },
      key,
    ),
  job: (id: string) => call<Job>(`/jobs/${id}`),
  prompts: (promptKey = '', cursor = '') =>
    call<CursorPage<Prompt>>(
      `/prompt-versions?prompt_key=${encodeURIComponent(promptKey)}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
    ),
  createPrompt: (body: { prompt_key: string; content: string; change_note: string }, key: string) =>
    call<Prompt>('/prompt-versions', 'POST', body, key),
  publish: (prompt: Prompt, key: string) =>
    call<Prompt>(
      `/prompt-versions/${prompt.id}/publish`,
      'POST',
      { expected_version: prompt.version },
      key,
    ),
}
