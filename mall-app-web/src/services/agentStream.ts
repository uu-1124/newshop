import type { AgentEvent, ChatMessage, Conversation, Run, Snapshot } from './agent'

export function compareEventIds(left: string, right: string): number {
  if (!/^\d+$/.test(left) || !/^\d+$/.test(right)) throw new Error('实时消息游标无效，请重新同步')
  const a = BigInt(left),
    b = BigInt(right)
  return a < b ? -1 : a > b ? 1 : 0
}

export function snapshotIsCurrent(
  snapshot: Snapshot,
  conversation: Conversation | null,
  appliedCursor: string,
): boolean {
  if (!conversation || conversation.id !== snapshot.conversation.id) return true
  return (
    snapshot.conversation.version >= conversation.version &&
    snapshot.conversation.control_epoch >= conversation.control_epoch &&
    compareEventIds(snapshot.event_cursor, appliedCursor) >= 0
  )
}

export function mergeSnapshotMessages(
  snapshot: Snapshot,
  messages: ChatMessage[],
  conversation: Conversation | null,
  preserveHistory: boolean,
  blockedRuns: ReadonlySet<string> = new Set(),
): ChatMessage[] {
  const keep =
    preserveHistory &&
    conversation?.id === snapshot.conversation.id &&
    conversation.control_epoch === snapshot.conversation.control_epoch
  const merged = new Map((keep ? messages : []).map((message) => [message.id, message]))
  snapshot.recent_messages.forEach((message) => {
    const prior = messages.find((item) => item.id === message.id)
    merged.set(
      message.id,
      prior &&
        message.run_id &&
        blockedRuns.has(message.run_id) &&
        message.text.startsWith(prior.text)
        ? { ...message, text: prior.text }
        : message,
    )
  })
  return [...merged.values()].sort((a, b) => compareEventIds(a.seq, b.seq))
}

export type DeltaResult = { kind: 'ignored' | 'resync' } | { kind: 'applied'; message: ChatMessage }

export function applyMessageDelta(
  event: AgentEvent,
  conversation: Conversation | null,
  activeRun: Run | null,
  messages: ChatMessage[],
  blockedRuns: ReadonlySet<string>,
): DeltaResult {
  if (!conversation || event.conversation_id !== conversation.id) return { kind: 'ignored' }
  if (event.control_epoch < conversation.control_epoch) return { kind: 'ignored' }
  if (
    event.control_epoch > conversation.control_epoch ||
    event.conversation_version > conversation.version
  )
    return { kind: 'resync' }
  if (
    !activeRun ||
    activeRun.state !== 'RUNNING' ||
    event.payload.run_id !== activeRun.id ||
    blockedRuns.has(activeRun.id)
  )
    return { kind: 'ignored' }
  const message = messages.find((item) => item.id === event.payload.message_id)
  if (!message) return { kind: 'resync' }
  if (message.run_id !== activeRun.id || message.status !== 'GENERATING') return { kind: 'ignored' }
  const offset = event.payload.offset,
    text = event.payload.text
  if (!Number.isSafeInteger(offset) || offset! < 0 || typeof text !== 'string')
    return { kind: 'resync' }
  const existing = Array.from(message.text),
    chunk = Array.from(text)
  if (offset! > existing.length) return { kind: 'resync' }
  const overlap = existing.slice(offset!, offset! + chunk.length)
  if (overlap.some((character, index) => character !== chunk[index])) return { kind: 'resync' }
  return {
    kind: 'applied',
    message: { ...message, text: message.text + chunk.slice(overlap.length).join('') },
  }
}
