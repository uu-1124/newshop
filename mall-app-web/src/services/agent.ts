import type {
  Action,
  Product,
  Sku,
  CheckoutOptions,
  CreateOrderParameters,
  ProductPage,
  StaffOrderSummary,
  OrderSummary,
  OrderPage,
  Logistics,
  ReturnPage,
  ReturnEligibility,
  ActionParameters,
  SelectedItem,
  ReplyFailure,
} from './agent-contracts'
export type {
  Action,
  Product,
  Sku,
  CheckoutOptions,
  OrderSummary,
  Logistics,
  ReturnSummary,
  ReturnEligibility,
  SelectedItem,
} from './agent-contracts'
const base = `${String(
  import.meta.env.VITE_AGENT_GATEWAY_URL || (import.meta.env.DEV ? '' : 'http://127.0.0.1:8201'),
).replace(/\/$/, '')}/mall-agent/v1/member`

export type CartItem = { id: number | string; productName: string; quantity: number }
export type Feedback = { id: string; message_id: string; vote: -1 | 1; reason: string | null }
export type ProductFilters = {
  query?: string
  min_price?: string
  max_price?: string
  category_id?: string
  brand_id?: string
  specification?: string
  in_stock?: boolean
  sort?: string
  cursor?: string
  limit?: number
}
export type Conversation = {
  id: string
  entry_context: { kind: 'GENERAL' } | { kind: 'PRODUCT'; product_id: string } | { kind: 'ORDER'; order_id: string }
  title: string
  last_message_preview: string
  unread_count: number
  state: 'AI_ACTIVE' | 'WAITING_HUMAN' | 'HUMAN_ACTIVE' | 'CLOSED'
  waiting_ai_enabled: boolean
  version: number
  control_epoch: number
  assigned_staff: { id: string; display_name: string; presence: string } | null
}
export type Handoff = { id: string; state: string; version: number }
export type Attachment = {
  id: string
  name: string
  mime_type: string
  byte_size: number
  purpose: string
  state: string
}
export type Source = {
  document_id: string
  version_id: string
  chunk_id: string
  title: string
  section: string
  available: boolean
}
export type ChatMessage = {
  id: string
  seq: string
  text: string
  sender_kind: string
  sender_display_name: string
  run_id: string | null
  status: string
  reply_failure?: ReplyFailure | null
  blocks: {
    type: string
    attachment?: Attachment
    source?: Source
    product?: Product
    action?: Action
    staff_order?: StaffOrderSummary
    order?: OrderSummary
  }[]
}
export type Run = {
  id: string
  state: string
  version: number
  attempt: number
  error_code: string | null
}
export type ReadPosition = { last_seq: string; unread_count: number }
export type ConversationPage = {
  items: Conversation[]
  next_cursor: string | null
  has_more: boolean
}
export type Snapshot = {
  conversation: Conversation
  pending_actions: Action[]
  handoff: Handoff | null
  active_run: Run | null
  event_cursor: string
  latest_seq: string
  read_position: ReadPosition
  recent_messages: ChatMessage[]
  history_cursor: string | null
}
export type AgentEvent = {
  event_id: string
  type: string
  conversation_id: string
  conversation_version: number
  control_epoch: number
  payload: {
    message_id?: string
    run_id?: string
    text?: string
    offset?: number
    message?: ChatMessage
  }
}

export class AgentError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message)
  }
}

function auth() {
  const token = uni.getStorageSync('token')
  if (!token) throw new AgentError('请先登录商城', 401)
  return String(token)
}

export function agentId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(
    16,
    20,
  )}-${hex.slice(20)}`
}

async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  key?: string,
  options: { timeout?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const response = await fetch(base + path, {
    method,
    signal: options.signal || AbortSignal.timeout(options.timeout || 35000),
    headers: {
      Authorization: auth(),
      ...(key ? { 'Idempotency-Key': key, 'Content-Type': 'application/json' } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const json = await response.json().catch(() => null)
  if (!response.ok || json?.code !== 'OK') {
    throw new AgentError(json?.message || '咨询服务暂不可用', response.status, json?.code)
  }
  return json.data as T
}

export const agent = {
  transcribe: (attachmentId: string, key: string) =>
    request<{ text: string; duration_ms: number; editable: true }>(
      '/audio/transcriptions',
      'POST',
      { attachment_id: attachmentId },
      key,
      { timeout: 95000 },
    ),
  speech: (messageId: string, part: number, key: string, signal: AbortSignal) =>
    request<{ attachment: Attachment; cache_hit: boolean; part_index: number; part_count: number }>(
      `/messages/${messageId}/speech`,
      'POST',
      { part },
      key,
      { signal },
    ),
  feedback: (id: string) => request<Feedback | null>(`/messages/${id}/feedback`),
  submitFeedback: (id: string, vote: -1 | 1, reason: string, key: string) =>
    request<Feedback>(`/messages/${id}/feedback`, 'POST', { vote, reason }, key),
  createOrderConversation: (key: string, orderId: string) =>
    request<Conversation>(
      '/conversations',
      'POST',
      { entry_context: { kind: 'ORDER', order_id: orderId } },
      key,
    ),
  orders: (filters: { order_sn?: string; status?: number; cursor?: string }) =>
    request<OrderPage>(
      `/orders?${new URLSearchParams(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== '')
          .map(([k, v]) => [k, String(v)]),
      )}`,
    ),
  order: (id: string) => request<OrderSummary>(`/orders/${id}`),
  logistics: (id: string) => request<Logistics>(`/orders/${id}/logistics`),
  returns: (id: string) => request<ReturnPage>(`/orders/${id}/returns`),
  returnEligibility: (order: string, item: string) =>
    request<ReturnEligibility>(`/orders/${order}/items/${item}/return-eligibility`),
  prepareBusiness: (conversation: Conversation, parameters: ActionParameters, key: string) =>
    request<Action>(
      `/conversations/${conversation.id}/actions`,
      'POST',
      { expected_conversation_version: conversation.version, parameters },
      key,
    ),
  directOptions: (items: SelectedItem[], key: string) =>
    request<CheckoutOptions>(
      '/checkout-options',
      'POST',
      { action_type: 'CREATE_ORDER', source: 'DIRECT', items },
      key,
    ),
  products: (filters: ProductFilters) => {
    const query = Object.entries(filters)
      .filter(([, value]) => value !== undefined && value !== '')
      .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
      .join('&')
    return request<ProductPage>(`/products?${query}`)
  },
  product: (id: string) => request<Product>(`/products/${id}`),
  cart: async (): Promise<CartItem[]> => {
    const response = await fetch(base.replace('/mall-agent/v1/member', '/mall-portal/cart/list'), {
      headers: { Authorization: auth() },
      signal: AbortSignal.timeout(15000),
      cache: 'no-store',
    })
    const body = await response.json().catch(() => null)
    if (!response.ok || body?.code !== 200)
      throw new AgentError('读取购物车失败，请刷新或重新登录', response.status)
    return body.data
  },
  checkoutOptions: (ids: string[], key: string) =>
    request<CheckoutOptions>(
      '/checkout-options',
      'POST',
      { action_type: 'CREATE_ORDER', source: 'CART', cart_item_ids: ids },
      key,
    ),
  prepareOrder: (conversation: Conversation, parameters: CreateOrderParameters, key: string) =>
    request<Action>(
      `/conversations/${conversation.id}/actions`,
      'POST',
      { expected_conversation_version: conversation.version, parameters },
      key,
    ),
  prepareCart: (
    conversation: Conversation,
    product: Product,
    sku: Sku,
    quantity: number,
    key: string,
  ) =>
    request<Action>(
      `/conversations/${conversation.id}/actions`,
      'POST',
      {
        expected_conversation_version: conversation.version,
        parameters: {
          action_type: 'ADD_CART',
          items: [{ product_id: product.id, sku_id: sku.id, quantity }],
        },
      },
      key,
    ),
  action: (id: string) => request<Action>(`/actions/${id}`),
  confirmAction: (action: Action, key: string) =>
    request<Action>(
      `/actions/${action.id}/confirm`,
      'POST',
      { expected_version: action.version },
      key,
    ),
  cancelAction: (action: Action, key: string) =>
    request<Action>(
      `/actions/${action.id}/cancel`,
      'POST',
      { expected_version: action.version },
      key,
    ),
  citation: (source: Source) =>
    request<{ source: Source; excerpt: string }>(
      `/citations/${source.version_id}/chunks/${source.chunk_id}`,
    ),
  list: (query = '', cursor?: string) =>
    request<ConversationPage>(
      `/conversations?limit=20&query=${encodeURIComponent(query)}${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
      }`,
    ),
  rename: (c: Conversation, title: string, key: string) =>
    request<Conversation>(
      `/conversations/${c.id}`,
      'PATCH',
      { title, expected_version: c.version },
      key,
    ),
  deleteConversation: (c: Conversation, key: string) =>
    request<Record<string, never>>(
      `/conversations/${c.id}`,
      'DELETE',
      { expected_version: c.version },
      key,
    ),
  markRead: (id: string, lastSeq: string, key: string) =>
    request<ReadPosition>(`/conversations/${id}/read`, 'POST', { last_seq: lastSeq }, key),
  create: (key: string, productId?: string) =>
    request<Conversation>(
      '/conversations',
      'POST',
      {
        entry_context: productId ? { kind: 'PRODUCT', product_id: productId } : { kind: 'GENERAL' },
      },
      key,
    ),
  snapshot: (id: string) => request<Snapshot>(`/conversations/${id}`),
  history: (id: string, cursor: string) =>
    request<{ items: ChatMessage[]; next_cursor: string | null }>(
      `/conversations/${id}/messages?cursor=${encodeURIComponent(cursor)}`,
    ),
  handoff: (c: Conversation, key: string) =>
    request<Handoff>(
      `/conversations/${c.id}/handoff`,
      'POST',
      { expected_conversation_version: c.version },
      key,
    ),
  withdraw: (c: Conversation, h: Handoff, key: string) =>
    request<Handoff>(
      `/conversations/${c.id}/handoff`,
      'DELETE',
      { expected_version: h.version },
      key,
    ),
  waitingAI: (c: Conversation, enabled: boolean, key: string) =>
    request<Conversation>(
      `/conversations/${c.id}/waiting-ai-mode`,
      'POST',
      { expected_version: c.version, enabled },
      key,
    ),
  send: (id: string, text: string, messageId: string, attachmentIds: string[] = []) =>
    request<{ message: ChatMessage; run: Run | null }>(
      `/conversations/${id}/messages`,
      'POST',
      { client_message_id: messageId, text, attachment_ids: attachmentIds },
      messageId,
    ),
  upload: async (
    file: Blob,
    name: string,
    key: string,
    purpose: 'CHAT_IMAGE' | 'RECORDING' = 'CHAT_IMAGE',
  ): Promise<Attachment> => {
    const form = new FormData()
    form.append('purpose', purpose)
    form.append('file', file, name)
    const response = await fetch(base + '/attachments', {
      method: 'POST',
      headers: { Authorization: auth(), 'Idempotency-Key': key },
      body: form,
      signal: AbortSignal.timeout(45000),
    })
    const json = await response.json().catch(() => null)
    if (!response.ok || json?.code !== 'OK')
      throw new AgentError(json?.message || '图片上传失败，请重试', response.status)
    return json.data as Attachment
  },
  image: async (id: string, signal: AbortSignal): Promise<Blob> => {
    const response = await fetch(base + `/attachments/${id}/content`, {
      headers: { Authorization: auth() },
      signal,
      cache: 'no-store',
    })
    if (!response.ok)
      throw new AgentError(response.status === 404 ? '图片已失效' : '图片加载失败', response.status)
    return response.blob()
  },
  run: (id: string) => request<Run>(`/runs/${id}`),
  retry: (run: Run, key: string) =>
    request<Run>(`/runs/${run.id}/retry`, 'POST', { expected_version: run.version }, key),
  stop: (run: Run) =>
    request<Run>(`/runs/${run.id}/stop`, 'POST', { expected_version: run.version }, agentId()),
}

export async function streamAgent(
  id: string,
  cursor: string,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => Promise<void>,
  onConnected?: () => void,
) {
  const response = await fetch(`${base}/conversations/${id}/events?after=${cursor}`, {
    headers: { Authorization: auth(), Accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok || !response.body) throw new AgentError('实时连接暂不可用', response.status)
  onConnected?.()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
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
        const data = frame.split('\n').find((line) => line.startsWith('data: '))
        if (data) await onEvent(JSON.parse(data.slice(6)) as AgentEvent)
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
