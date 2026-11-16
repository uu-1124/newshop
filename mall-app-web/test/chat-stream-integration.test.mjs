import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { nextTick } from 'vue'

const nodeRequire = createRequire(import.meta.url)
const source = await readFile(new URL('../src/pages/agent/chat.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'chat-stream-integration' })
const javascript = ts.transpileModule(
  compiled.content.replaceAll('import.meta', '({hot:undefined})'),
  {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  },
).outputText
const streamSource = await readFile(
  new URL('../src/services/agentStream.ts', import.meta.url),
  'utf8',
)
const streamJavascript = ts.transpileModule(streamSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const streamModule = { exports: {} }
vm.runInNewContext(streamJavascript, { module: streamModule, exports: streamModule.exports })

class AgentError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}
const copy = (value) => JSON.parse(JSON.stringify(value))
const message = {
  id: 'message',
  seq: '2',
  text: '你好😀',
  run_id: 'run',
  status: 'GENERATING',
  sender_kind: 'ASSISTANT',
  blocks: [],
}
function snapshot(cursor = '10', epoch = 1, text = message.text) {
  return {
    conversation: {
      id: 'conversation',
      title: 'synthetic stream',
      state: 'AI_ACTIVE',
      version: 3,
      control_epoch: epoch,
      waiting_ai_enabled: false,
    },
    active_run: { id: 'run', state: 'RUNNING', version: 2 },
    handoff: null,
    event_cursor: cursor,
    recent_messages: [{ ...message, text }],
    history_cursor: 'older',
    read_position: { last_seq: '2' },
  }
}
function delta(id, offset, text, epoch = 1) {
  return {
    event_id: String(id),
    type: 'message.delta',
    conversation_id: 'conversation',
    conversation_version: 3,
    control_epoch: epoch,
    payload: { message_id: 'message', run_id: 'run', offset, text },
  }
}
const deferred = () => {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

async function harness(entry = {}) {
  const hooks = {},
    subscriptions = [],
    pendingSnapshots = []
  const initial = snapshot()
  let currentSnapshot = initial
  let resolveSubscribed
  const subscribed = () =>
    new Promise((resolve) => {
      resolveSubscribed = resolve
    })
  const agent = {
    create: async (key, productId) => {
      creations.push({ key, productId })
      return copy(initial.conversation)
    },
    createOrderConversation: async (key, orderId) => {
      creations.push({ key, orderId })
      return copy(initial.conversation)
    },
    list: async () => ({ items: [copy(initial.conversation)], next_cursor: null }),
    snapshot: async () =>
      pendingSnapshots.length ? pendingSnapshots.shift() : copy(currentSnapshot),
    history: async () => ({
      items: [{ ...message, id: 'old-message', seq: '1', text: 'old history' }],
      next_cursor: null,
    }),
    run: async () => copy(initial.active_run),
    stop: async () => {},
    markRead: async (_id, lastSeq) => ({ last_seq: lastSeq, unread_count: 0 }),
  }
  const creations = []
  const module = { exports: {} }
  vm.runInNewContext(javascript, {
    module,
    exports: module.exports,
    console,
    AbortController,
    BigInt,
    URL: { revokeObjectURL() {} },
    uni: { getStorageSync: () => 'synthetic-only' },
    setInterval: () => 0,
    clearInterval() {},
    setTimeout,
    require(name) {
      if (name === '@dcloudio/uni-app')
        return {
          onShow: (fn) => {
            hooks.show = fn
          },
          onHide: (fn) => {
            hooks.hide = fn
          },
          onUnload: (fn) => {
            hooks.unload = fn
          },
          onLoad: (fn) => { hooks.load = fn },
        }
      if (name === '@/services/agentStream') return streamModule.exports
      if (name === '@/services/agentAudio') return { stopAllSpeech() {} }
      if (name === '@/services/agent')
        return {
          agent,
          AgentError,
          agentId: () => 'synthetic-id',
          streamAgent: async (id, cursor, signal, event, connected) => {
            connected?.()
            const disconnected = deferred()
            subscriptions.push({ id, cursor, signal, event, disconnect: disconnected.resolve })
            resolveSubscribed?.()
            await Promise.race([
              disconnected.promise,
              new Promise((resolve) => signal.addEventListener('abort', resolve, { once: true })),
            ])
          },
        }
      if (name.endsWith('.vue')) return {}
      return nodeRequire(name)
    },
  })
  const state = module.exports.default.setup({}, { expose() {} })
  hooks.load(entry)
  const ready = subscribed()
  hooks.show()
  await ready
  return {
    state,
    agent,
    subscriptions,
    pendingSnapshots,
    hooks,
    creations,
    setSnapshot: (value) => {
      currentSnapshot = value
    },
    nextSubscription: subscribed,
    close: () => hooks.unload(),
  }
}

test('product entry creates only one scoped conversation before subscribing to its stream', async () => {
  const h = await harness({ productId: '9007199254740993' })
  try {
    assert.equal(h.creations.length, 1)
    assert.equal(h.creations[0].productId, '9007199254740993')
    assert.equal(h.state.entryProductId.value, '')
    await h.state.load()
    assert.equal(h.creations.length, 1)
  } finally { h.close() }
})

test('order entry sets its target and clears the entry while the stream remains open', async () => {
  const h = await harness({ orderId: '40', productId: '45' })
  try {
    assert.equal(h.creations[0].orderId, '40')
    assert.equal(h.state.orderPanelId.value, '40')
    assert.equal(h.state.entryOrderId.value, '')
    assert.equal(h.state.entryProductId.value, '')
  } finally { h.close() }
})

test('malformed and overflowing product IDs cannot create scoped conversations', async () => {
  for (const productId of ['9223372036854775808', '45&orderId=40', '-1', '0045']) {
    const h = await harness({ productId })
    try { assert.equal(h.creations.length, 0) } finally { h.close() }
  }
})

test('the actual chat callback repairs a missing fragment before advancing to later text', async () => {
  const h = await harness()
  try {
    await h.subscriptions[0].event(delta(11, 3, '世界'))
    assert.equal(h.state.messages.value[0].text, '你好😀世界')
    h.setSnapshot(snapshot('13', 1, '你好😀世界补齐'))
    await h.subscriptions[0].event(delta(12, 99, '不能直接拼接'))
    assert.equal(h.state.messages.value[0].text, '你好😀世界补齐')
    await h.subscriptions[0].event(delta(14, 7, '。'))
    assert.equal(h.state.messages.value[0].text, '你好😀世界补齐。')
    await h.subscriptions[0].event(delta(11, 3, '世界'))
    assert.equal(h.state.messages.value[0].text, '你好😀世界补齐。')
  } finally {
    h.close()
  }
})

test('quota recovery keeps the partial reply and prevents retries before its actual UTC window', async () => {
  const h = await harness()
  try {
    const failed = snapshot('12', 1, '已核验的部分')
    failed.active_run = null
    const retryAfter = Date.now() + 60000
    failed.recent_messages[0].status = 'FAILED'
    failed.recent_messages[0].reply_failure = {
      code: 'MODEL_QUOTA_EXCEEDED',
      message: '今日额度已用完，原问题已保留。',
      retry_after: new Date(retryAfter).toISOString(),
    }
    h.setSnapshot(failed)
    await h.state.refreshCurrent()
    let retries = 0
    h.agent.run = async () => ({ id: 'run', version: 3, state: 'FAILED' })
    h.agent.retry = async () => {
      retries++
      return { id: 'new-run', state: 'QUEUED' }
    }
    const current = h.state.messages.value[0]
    assert.equal(current.text, '已核验的部分')
    assert.equal(h.state.canRetry(current), true)
    assert.equal(h.state.retryDelayed(current), true)
    await h.state.retryReply(current)
    assert.equal(retries, 0)
    h.state.pageTime.value = retryAfter + 1
    assert.equal(h.state.retryDelayed(current), false)
    await h.state.retryReply(current)
    assert.equal(retries, 1)
  } finally {
    h.close()
  }
})

test('historical failed replies cannot be retried after a newer member context supersedes them', async () => {
  const h = await harness()
  try {
    const changed = snapshot('12')
    changed.active_run = null
    changed.recent_messages[0].status = 'FAILED'
    changed.recent_messages.push({
      ...message,
      id: 'new-member',
      run_id: null,
      status: 'COMPLETED',
      sender_kind: 'MEMBER',
      seq: '3',
      text: '按新的选择',
    })
    h.setSnapshot(changed)
    await h.state.refreshCurrent()
    let queried = false
    h.agent.run = async () => {
      queried = true
      return {}
    }
    assert.equal(h.state.canRetry(h.state.messages.value[0]), false)
    await h.state.retryReply(h.state.messages.value[0])
    assert.equal(queried, false)
  } finally {
    h.close()
  }
})

test('a delayed refresh cannot replace newer streamed text even when conversation version is unchanged', async () => {
  const h = await harness()
  try {
    const slow = deferred()
    h.pendingSnapshots.push(slow.promise)
    const refreshing = h.state.refreshCurrent()
    await h.subscriptions[0].event(delta(11, 3, '世界'))
    slow.resolve(snapshot('10'))
    await refreshing
    assert.equal(h.state.messages.value[0].text, '你好😀世界')
  } finally {
    h.close()
  }
})

test('clicking stop fences already buffered deltas while the stop request is still pending', async () => {
  const h = await harness()
  try {
    const stopRequest = deferred()
    h.agent.stop = () => stopRequest.promise
    const stopping = h.state.stop()
    await Promise.resolve()
    await h.subscriptions[0].event(delta(11, 3, '不得追加'))
    assert.equal(h.state.messages.value[0].text, '你好😀')
    const terminal = snapshot('12', 2, '你好😀停止请求期间晚到的正文')
    terminal.active_run = null
    terminal.conversation.version = 4
    terminal.recent_messages[0].status = 'STOPPED'
    h.setSnapshot(terminal)
    const reconnected = h.nextSubscription()
    stopRequest.resolve()
    await stopping
    await reconnected
    assert.equal(h.state.activeRun.value, null)
    await h.subscriptions[0].event(delta(13, 3, '旧连接缓冲'))
    assert.equal(h.state.messages.value[0].text, '你好😀')
  } finally {
    h.close()
  }
})

test('takeover snapshot removes cached old pages and ignores the old generation', async () => {
  const h = await harness()
  try {
    await h.state.older()
    assert.equal(h.state.messages.value.length, 2)
    const human = snapshot('15', 2)
    human.active_run = null
    human.conversation = { ...human.conversation, version: 4, state: 'HUMAN_ACTIVE' }
    human.recent_messages = [{ ...message, status: 'STOPPED', text: '接管前的部分' }]
    h.setSnapshot(human)
    await h.state.refreshCurrent()
    assert.equal(h.state.messages.value.length, 1)
    assert.equal(h.state.current.value.state, 'HUMAN_ACTIVE')
    await h.subscriptions[0].event(delta(16, 3, '旧生成', 1))
    assert.equal(h.state.messages.value[0].text, '接管前的部分')
  } finally {
    h.close()
  }
})

for (const command of ['stop', 'handoff']) {
  test(`a rejected ${command} resumes valid text from an authoritative snapshot`, async () => {
    const h = await harness()
    try {
      h.agent[command] = async () => {
        throw new AgentError('synthetic rejected command', 503)
      }
      h.setSnapshot(snapshot('11', 1, '你好😀请求期间已保存'))
      if (command === 'stop') await h.state.stop()
      else await h.state.handoffCommand('request')
      assert.equal(h.state.activeRun.value.state, 'RUNNING')
      assert.equal(h.state.messages.value[0].text, '你好😀请求期间已保存')
      await h.subscriptions[0].event(delta(12, 10, '继续'))
      assert.equal(h.state.messages.value[0].text, '你好😀请求期间已保存继续')
    } finally {
      h.close()
    }
  })

  test(`a lost ${command} response preserves the fence when the server actually advanced control`, async () => {
    const h = await harness()
    try {
      h.agent[command] = async () => {
        throw new AgentError('synthetic lost response', 503)
      }
      const changed = snapshot('12', 2, '你好😀不得迟到显示')
      changed.conversation.version = 4
      if (command === 'handoff') changed.conversation.state = 'WAITING_HUMAN'
      changed.active_run = null
      changed.recent_messages[0].status = 'STOPPED'
      h.setSnapshot(changed)
      if (command === 'stop') await h.state.stop()
      else await h.state.handoffCommand('request')
      assert.equal(h.state.current.value.control_epoch, 2)
      assert.equal(h.state.messages.value[0].text, '你好😀')
      await h.subscriptions[0].event(delta(13, 3, '旧缓冲'))
      assert.equal(h.state.messages.value[0].text, '你好😀')
    } finally {
      h.close()
    }
  })
}

test('a failed control with an unavailable snapshot stays fenced until a later authoritative recovery', async () => {
  const h = await harness()
  try {
    h.agent.stop = async () => {
      throw new AgentError('synthetic command failure', 503)
    }
    const original = h.agent.snapshot
    h.agent.snapshot = async () => {
      throw new AgentError('synthetic snapshot failure', 503)
    }
    await h.state.stop()
    await h.subscriptions[0].event(delta(11, 3, '暂不显示'))
    assert.equal(h.state.messages.value[0].text, '你好😀')
    h.agent.snapshot = original
    h.setSnapshot(snapshot('12', 1, '你好😀恢复后正文'))
    await h.state.refreshCurrent()
    assert.equal(h.state.messages.value[0].text, '你好😀恢复后正文')
    await h.subscriptions[0].event(delta(13, 8, '继续'))
    assert.equal(h.state.messages.value[0].text, '你好😀恢复后正文继续')
  } finally {
    h.close()
  }
})

test('reopening a stopped conversation reads persisted history instead of retaining a local empty clamp', async () => {
  const h = await harness()
  try {
    h.agent.stop = async () => {}
    const stopped = snapshot('12', 2, '你好😀停止前已保存')
    stopped.active_run = null
    stopped.conversation.version = 4
    stopped.recent_messages[0].status = 'STOPPED'
    h.setSnapshot(stopped)
    let ready = h.nextSubscription()
    await h.state.stop()
    await ready
    assert.equal(h.state.messages.value[0].text, '你好😀')
    const other = snapshot('13')
    other.conversation.id = 'other'
    other.recent_messages = []
    other.active_run = null
    h.setSnapshot(other)
    ready = h.nextSubscription()
    void h.state.select('other')
    await ready
    h.setSnapshot(stopped)
    ready = h.nextSubscription()
    void h.state.select('conversation')
    await ready
    assert.equal(h.state.messages.value[0].text, '你好😀停止前已保存')
  } finally {
    h.close()
  }
})

test('disconnection resnapshots a completed run without repeating text or creating a new generation', async () => {
  const h = await harness()
  try {
    await h.subscriptions[0].event(delta(11, 3, '在线'))
    const completed = snapshot('15', 1, '你好😀在线离线期间补齐')
    completed.active_run = null
    completed.recent_messages[0].status = 'COMPLETED'
    h.setSnapshot(completed)
    const ready = h.nextSubscription()
    h.subscriptions[0].disconnect()
    await ready
    assert.equal(h.state.activeRun.value, null)
    assert.equal(h.state.messages.value.length, 1)
    assert.equal(h.state.messages.value[0].text, '你好😀在线离线期间补齐')
    assert.equal(h.subscriptions[1].cursor, '15')
    await h.subscriptions[1].event(delta(11, 3, '在线'))
    assert.equal(h.state.messages.value[0].text, '你好😀在线离线期间补齐')
  } finally {
    h.close()
  }
})

for (const [kind, method] of [
  ['toggle', 'waitingAI'],
  ['withdraw', 'withdraw'],
]) {
  test(`${kind} fences waiting AI immediately and restores it only after a rejected command is reconciled`, async () => {
    const h = await harness()
    try {
      const waiting = snapshot()
      waiting.conversation.state = 'WAITING_HUMAN'
      waiting.conversation.waiting_ai_enabled = true
      waiting.handoff = { id: 'handoff', state: 'WAITING', version: 1 }
      h.setSnapshot(waiting)
      await h.state.refreshCurrent()
      const pending = deferred()
      h.agent[method] = async () => {
        await pending.promise
        throw new AgentError('synthetic rejected waiting command', 503)
      }
      const command = h.state.handoffCommand(kind)
      await h.subscriptions[0].event(delta(11, 3, '缓冲正文'))
      assert.equal(h.state.messages.value[0].text, '你好😀')
      const latest = {
        ...waiting,
        event_cursor: '12',
        recent_messages: [{ ...message, text: '你好😀缓冲正文' }],
      }
      h.setSnapshot(latest)
      await h.state.refreshCurrent()
      assert.equal(h.state.messages.value[0].text, '你好😀')
      pending.resolve()
      await command
      assert.equal(h.state.messages.value[0].text, '你好😀缓冲正文')
      await h.subscriptions[0].event(delta(13, 7, '继续'))
      assert.equal(h.state.messages.value[0].text, '你好😀缓冲正文继续')
    } finally {
      h.close()
    }
  })
}

test('the redesigned feed follows streaming replies while the reader is at the bottom', async () => {
  const h = await harness()
  try {
    const feed = { scrollHeight: 900, clientHeight: 400, scrollTop: 500 }
    h.state.chatFeed.value = feed
    h.state.trackChatScroll()
    await h.subscriptions[0].event(delta(11, 3, '继续回复'))
    await nextTick()
    assert.equal(feed.scrollTop, 900)
    assert.equal(h.state.messages.value[0].text, '你好😀继续回复')
  } finally {
    h.close()
  }
})

test('the redesigned feed keeps the reading position until the reader returns to the bottom', async () => {
  const h = await harness()
  try {
    const feed = { scrollHeight: 900, clientHeight: 400, scrollTop: 100 }
    h.state.chatFeed.value = feed
    h.state.trackChatScroll()
    await h.subscriptions[0].event(delta(11, 3, '新的内容'))
    await nextTick()
    assert.equal(feed.scrollTop, 100)
    feed.scrollTop = 500
    h.state.trackChatScroll()
    await h.subscriptions[0].event(delta(12, 7, '继续'))
    await nextTick()
    assert.equal(feed.scrollTop, 900)
  } finally {
    h.close()
  }
})

test('an empty consultation shows the welcome screen from its top', async () => {
  const h = await harness()
  try {
    const feed = { scrollHeight: 900, clientHeight: 400, scrollTop: 500 }
    h.state.chatFeed.value = feed
    const empty = snapshot('12')
    empty.conversation.id = 'empty-consultation'
    empty.recent_messages = []
    empty.active_run = null
    h.setSnapshot(empty)
    const ready = h.nextSubscription()
    void h.state.select('empty-consultation')
    await ready
    await nextTick()
    assert.equal(feed.scrollTop, 0)
  } finally {
    h.close()
  }
})
