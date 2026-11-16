import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import { createSSRApp } from 'vue'

// Render the actual component with inert transport, without changing an app's state.
const source = await readFile(new URL('../src/pages/agent/CartCards.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'cancellation-card-test', inlineTemplate: true })
const javascript = ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText
const nodeRequire = createRequire(import.meta.url)
const module = { exports: {} }
vm.runInNewContext(javascript, {
  module,
  exports: module.exports,
  require(name) {
    if (name === '@/services/agent') return { agent: {}, agentId: () => 'unused' }
    if (name === './OrderCheckout.vue') return { render: () => null }
    return nodeRequire(name)
  },
  setInterval: () => 0,
  clearInterval: () => {},
})
const component = module.exports.default

function fixture(state = 'PENDING_CONFIRMATION') {
  return {
    id: 'f56c201b-bd1b-45a3-bd60-fc04f3e64458',
    action_type: 'CANCEL_ORDER',
    state,
    version: 1,
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    parameters: { action_type: 'CANCEL_ORDER', order_id: '9007199254740993' },
    quote: {
      rule_version: 'CANCEL_PENDING_V2',
      cancellation: {
        id: '9007199254740993',
        order_sn: '20261114000000000000001234567890',
        pay_amount: '1234.56',
        status: 0,
      },
      pricing: { payable: '1234.56' },
      lines: [],
    },
    result: state === 'SUCCEEDED' ? { order: { status: 4 } } : null,
  }
}

async function render(action) {
  return renderToString(
    createSSRApp(component, {
      products: [],
      actions: [action],
      conversation: { id: 'unused' },
      disabled: false,
    }),
  )
}

test('a valid pending card displays the complete trusted number and exact amount', async () => {
  const html = await render(fixture())
  assert.match(html, /20261114000000000000001234567890/)
  assert.match(html, /确认时订单金额 ¥1234\.56/)
  assert.match(html, /准备确认时订单为待付款/)
  assert.match(html, /<button(?![^>]*disabled)[^>]*>确认取消订单<\/button>/)
})

test('a successful cancellation describes its result without claiming it is still unpaid', async () => {
  const html = await render(fixture('SUCCEEDED'))
  assert.match(html, /订单已取消/)
  assert.match(html, /确认时订单金额 ¥1234\.56/)
  assert.doesNotMatch(html, /当前待付款|准备确认时订单为待付款|>确认取消订单<\/button>/)
})

for (const kind of ['ADD_CART', 'CREATE_ORDER', 'CANCEL_ORDER', 'CREATE_RETURN']) {
  test(`${kind}: superseded member context explains the stale card and removes confirmation`, async () => {
    const action = fixture('STALE')
    action.action_type = kind
    action.error_code = 'MEMBER_CONTEXT_CHANGED'
    action.parameters = {
      action_type: kind,
      order_id: '40',
      order_item_id: '50',
      quantity: 1,
      reason_code: '60',
      description: '演示',
      attachment_ids: [],
    }
    const html = await render(action)
    assert.match(html, /你发送了新的消息，此卡已失效/)
    assert.doesNotMatch(html, />确认(?:加购|下单|取消订单|提交退货)/)
    assert.doesNotMatch(html, /<button[^>]*>取消本次操作/)
  })
}

test('unknown, expired and stale cards do not present a current unpaid state', async () => {
  for (const state of ['EXECUTING', 'UNKNOWN', 'EXPIRED', 'STALE', 'CANCELLED', 'FAILED']) {
    const html = await render(fixture(state))
    assert.doesNotMatch(html, /当前待付款|准备确认时订单为待付款|>确认取消订单<\/button>/)
    if (state === 'UNKNOWN') assert.match(html, /正在核对原操作结果/)
  }
})

test('legacy, mismatched and incomplete summaries cannot enable confirmation', async () => {
  for (const change of [
    (a) => {
      delete a.quote.cancellation
    },
    (a) => {
      a.quote.rule_version = 'CANCEL_PENDING_V1'
    },
    (a) => {
      a.quote.cancellation.id = '42'
    },
    (a) => {
      a.quote.cancellation.pay_amount = '0.01'
    },
    (a) => {
      a.quote.cancellation.order_sn = ' '
    },
    (a) => {
      a.quote.cancellation.status = 4
    },
  ]) {
    const action = fixture()
    change(action)
    const html = await render(action)
    assert.match(html, /此旧确认卡缺少订单号或金额/)
    assert.match(html, /<button[^>]*disabled[^>]*>确认取消订单<\/button>/)
  }
})

test('a locally expired pending card cannot show a confirmation control', async () => {
  const action = fixture()
  action.expires_at = new Date(Date.now() - 1000).toISOString()
  const html = await render(action)
  assert.match(html, /确认卡已过期/)
  assert.match(html, /取消未付款订单 · 已过期/)
  assert.match(html, /请重新读取订单状态和金额，准备取消卡/)
  assert.doesNotMatch(html, /待你确认/)
  assert.doesNotMatch(html, /准备确认时订单为待付款|>确认取消订单<\/button>/)
})

for (const kind of ['ADD_CART', 'CREATE_ORDER', 'CANCEL_ORDER', 'CREATE_RETURN']) {
  test(`${kind}: persisted expiry and takeover explain why controls disappeared`, async () => {
    const expired = fixture('EXPIRED')
    expired.action_type = kind
    const html = await render(expired)
    assert.match(html, /确认卡已过期/)
    assert.doesNotMatch(html, /<button[^>]*>确认/)
    const stale = fixture('STALE')
    stale.action_type = kind
    const takeover = await render(stale)
    assert.match(takeover, /会话状态已变化，此卡已失效/)
    assert.match(takeover, /人工接待期间请向客服说明需求；旧卡不会执行/)
    assert.doesNotMatch(takeover, /<button[^>]*>确认/)
  })
}

test('historical creation results label the saved amount without asserting the current payment state', async () => {
  const action = fixture('SUCCEEDED')
  action.action_type = 'CREATE_ORDER'
  action.parameters = { action_type: 'CREATE_ORDER' }
  action.quote.pricing = {
    subtotal: '1234.56',
    freight: '0.00',
    promotion_discount: '0.00',
    coupon_discount: '0.00',
    points_discount: '0.00',
    payable: '1234.56',
  }
  action.result = { order: { order_sn: '20261114000000000000001234567890', pay_amount: '1234.56' } }
  const html = await render(action)
  assert.match(html, /已创建，创建时应付 ¥1234\.56/)
  assert.doesNotMatch(html, /已创建，待付款/)
})

test('a return card labels the submission snapshot separately from subsequent refund progress', async () => {
  const action = fixture('SUCCEEDED')
  action.action_type = 'CREATE_RETURN'
  action.parameters = {
    action_type: 'CREATE_RETURN',
    order_id: '42',
    order_item_id: '43',
    quantity: 1,
    reason_code: '1',
    description: '演示凭证',
    attachment_ids: [],
  }
  action.result = { return_apply: { id: '44', stage: 'REQUESTED' } }
  const html = await render(action)
  assert.match(html, /提交时状态 REQUESTED/)
  assert.match(html, /退款仅为演示，须另行处理/)
})
