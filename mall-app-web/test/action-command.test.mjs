import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

// Exercise handlers captured from actual compiled component VNodes. Transport
// is inert, and the clock can advance without firing the display timer.
const source = await readFile(new URL('../src/pages/agent/CartCards.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'action-command', inlineTemplate: true })
const javascript = ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText
const nodeRequire = createRequire(import.meta.url)
const kinds = ['ADD_CART', 'CREATE_ORDER', 'CANCEL_ORDER', 'CREATE_RETURN']

function card(kind, time) {
  return {
    id: kind,
    action_type: kind,
    state: 'PENDING_CONFIRMATION',
    version: 1,
    expires_at: new Date(time + 3000).toISOString(),
    parameters: { action_type: kind, order_id: '40', attachment_ids: [] },
    quote: {
      rule_version: 'CANCEL_PENDING_V2',
      cancellation: { id: '40', order_sn: 'DEMO-40', status: 0, pay_amount: '10.00' },
      pricing: { payable: '10.00' },
      lines: [],
    },
  }
}

function nodes(value) {
  if (!value) return []
  if (Array.isArray(value)) return value.flatMap(nodes)
  if (typeof value !== 'object') return []
  return [value, ...nodes(value.children)]
}

function content(value) {
  if (!value) return ''
  if (Array.isArray(value)) return value.map(content).join('')
  if (typeof value === 'object') return content(value.children)
  return String(value)
}

function harness(kind) {
  let clock = Date.now(),
    sequence = 0
  const calls = [],
    module = { exports: {} }
  const agent = Object.fromEntries(
    ['confirmAction', 'cancelAction'].map((name) => [
      name,
      async (action, key) => {
        calls.push({ name, action, key })
        return {
          ...action,
          state: name === 'confirmAction' ? 'SUCCEEDED' : 'CANCELLED',
          version: action.version + 1,
        }
      },
    ]),
  )
  class Clock extends Date {
    static now() {
      return clock
    }
  }
  vm.runInNewContext(javascript, {
    module,
    exports: module.exports,
    Date: Clock,
    require(name) {
      if (name === 'vue') return { ...vue, onUnmounted() {}, resolveComponent: (name) => name }
      if (name === '@/services/agent') return { agent, agentId: () => `test-${++sequence}` }
      if (name === './OrderCheckout.vue') return { render: () => null }
      return nodeRequire(name)
    },
    setInterval: () => 0,
    clearInterval() {},
  })
  const props = vue.reactive({
    products: [],
    actions: [card(kind, clock)],
    conversation: { id: 'test', version: 1 },
    disabled: false,
  })
  const render = module.exports.default.setup(props, { expose() {}, emit() {} })
  const controls = () => nodes(render({}, [])).filter((node) => node.type === 'button')
  const captured = (confirm = true) =>
    controls().find((node) =>
      confirm ? content(node).startsWith('确认') : content(node) === '取消本次操作',
    ).props.onClick
  return {
    props,
    calls,
    agent,
    captured,
    advance(ms) {
      clock += ms
    },
  }
}

for (const kind of kinds) {
  test(`${kind}: a click after the deadline is rejected before the display timer updates`, async () => {
    for (const confirm of [true, false]) {
      const h = harness(kind),
        click = h.captured(confirm)
      h.advance(3001)
      await click()
      assert.equal(h.calls.length, 0)
    }
  })

  test(`${kind}: a queued old handler cannot revive a removed or terminal card`, async () => {
    for (const state of [
      'STALE',
      'CANCELLED',
      'EXPIRED',
      'EXECUTING',
      'UNKNOWN',
      'SUCCEEDED',
      'FAILED',
      'REMOVED',
    ]) {
      const h = harness(kind),
        click = h.captured()
      h.props.actions = state === 'REMOVED' ? [] : [{ ...h.props.actions[0], state, version: 2 }]
      await click()
      assert.equal(h.calls.length, 0, state)
    }
  })

  test(`${kind}: local completion and takeover prevent a second command`, async () => {
    const h = harness(kind),
      click = h.captured()
    await click()
    await click()
    assert.equal(h.calls.length, 1)
    const takeover = harness(kind),
      old = takeover.captured()
    takeover.props.disabled = true
    await old()
    assert.equal(takeover.calls.length, 0)
  })

  test(`${kind}: a queued click sends the latest authoritative version`, async () => {
    const h = harness(kind),
      click = h.captured()
    h.props.actions = [{ ...h.props.actions[0], version: 3 }]
    await click()
    assert.equal(h.calls.length, 1)
    assert.equal(h.calls[0].action.version, 3)
    assert.match(h.calls[0].key, /^test-/)
  })
}

test('an old cancellation handler cannot send a newly invalid legacy summary', async () => {
  const h = harness('CANCEL_ORDER'),
    click = h.captured()
  h.props.actions = [
    {
      ...h.props.actions[0],
      version: 2,
      quote: { ...h.props.actions[0].quote, rule_version: 'CANCEL_PENDING_V1' },
    },
  ]
  await click()
  assert.equal(h.calls.length, 0)
})
