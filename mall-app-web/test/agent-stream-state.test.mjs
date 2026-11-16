import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/services/agentStream.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { applyMessageDelta, compareEventIds, mergeSnapshotMessages, snapshotIsCurrent } =
  await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'))

const conversation = { id: 'conversation', version: 5, control_epoch: 2, state: 'AI_ACTIVE' }
const run = { id: 'run', state: 'RUNNING', version: 2 }
const message = {
  id: 'message',
  seq: '9007199254740994',
  text: '你好😀',
  run_id: 'run',
  status: 'GENERATING',
  blocks: [],
}
const event = {
  event_id: '9007199254740995',
  type: 'message.delta',
  conversation_id: 'conversation',
  conversation_version: 5,
  control_epoch: 2,
  payload: { message_id: 'message', run_id: 'run', offset: 3, text: '世界' },
}
const snapshot = (changes = {}) => ({
  conversation: { ...conversation },
  active_run: { ...run },
  event_cursor: '9007199254740996',
  recent_messages: [{ ...message }],
  ...changes,
})

test('global event IDs and message sequences retain precision beyond JavaScript safe integers', () => {
  assert.equal(compareEventIds('9007199254740993', '9007199254740992'), 1)
  assert.equal(compareEventIds('9007199254740993', '9007199254740993'), 0)
  for (const value of ['NaN', '1.5', '-1', '']) assert.throws(() => compareEventIds(value, '0'))
})

test('late snapshots cannot roll conversation control or already applied events backward', () => {
  assert.equal(snapshotIsCurrent(snapshot(), conversation, '9007199254740995'), true)
  assert.equal(
    snapshotIsCurrent(
      snapshot({ event_cursor: '9007199254740994' }),
      conversation,
      '9007199254740995',
    ),
    false,
  )
  assert.equal(
    snapshotIsCurrent(
      snapshot({ conversation: { ...conversation, version: 4 } }),
      conversation,
      '0',
    ),
    false,
  )
  assert.equal(
    snapshotIsCurrent(
      snapshot({ conversation: { ...conversation, control_epoch: 1 } }),
      conversation,
      '0',
    ),
    false,
  )
  assert.equal(
    snapshotIsCurrent(snapshot({ active_run: null }), conversation, '9007199254740996'),
    true,
  )
})

test('delta offsets count Unicode codepoints and replay overlap never duplicates text', () => {
  let value = applyMessageDelta(event, conversation, run, [message], new Set())
  assert.equal(value.kind, 'applied')
  assert.equal(value.message.text, '你好😀世界')
  value = applyMessageDelta(event, conversation, run, [value.message], new Set())
  assert.equal(value.message.text, '你好😀世界')
  const overlap = { ...event, payload: { ...event.payload, offset: 2, text: '😀世界！' } }
  assert.equal(
    applyMessageDelta(overlap, conversation, run, [value.message], new Set()).message.text,
    '你好😀世界！',
  )
  assert.equal(message.text, '你好😀')
})

test('missing message, offset gap, conflicting overlap and malformed offset require a snapshot', () => {
  for (const change of [
    { offset: 4 },
    { offset: 1, text: '错误' },
    { offset: -1 },
    { offset: 1.5 },
    { offset: undefined },
    { text: null },
  ]) {
    assert.equal(
      applyMessageDelta(
        { ...event, payload: { ...event.payload, ...change } },
        conversation,
        run,
        [message],
        new Set(),
      ).kind,
      'resync',
    )
  }
  assert.equal(applyMessageDelta(event, conversation, run, [], new Set()).kind, 'resync')
  assert.equal(
    applyMessageDelta({ ...event, control_epoch: 3 }, conversation, run, [message], new Set()).kind,
    'resync',
  )
})

test('stopped or superseded runs and artificial reception cannot accept old deltas', () => {
  for (const state of ['STOP_REQUESTED', 'STOPPED', 'INTERRUPTED', 'COMPLETED']) {
    assert.equal(
      applyMessageDelta(event, conversation, { ...run, state }, [message], new Set()).kind,
      'ignored',
    )
  }
  assert.equal(
    applyMessageDelta(event, conversation, run, [message], new Set(['run'])).kind,
    'ignored',
  )
  assert.equal(
    applyMessageDelta(
      event,
      { ...conversation, control_epoch: 3, state: 'HUMAN_ACTIVE' },
      null,
      [message],
      new Set(),
    ).kind,
    'ignored',
  )
  assert.equal(
    applyMessageDelta(
      { ...event, payload: { ...event.payload, run_id: 'old-run' } },
      conversation,
      run,
      [message],
      new Set(),
    ).kind,
    'ignored',
  )
  assert.equal(
    applyMessageDelta(event, conversation, run, [{ ...message, status: 'STOPPED' }], new Set())
      .kind,
    'ignored',
  )
})

test('snapshot repair replaces damaged text, preserves valid older pages and drops them on epoch change', () => {
  const old = { ...message, id: 'history', seq: '9007199254740993', text: 'earlier' }
  const damaged = { ...message, text: 'damaged' }
  const merged = mergeSnapshotMessages(snapshot(), [damaged, old], conversation, true)
  assert.deepEqual(
    merged.map((m) => m.id),
    ['history', 'message'],
  )
  assert.equal(merged[1].text, message.text)
  assert.equal(
    mergeSnapshotMessages(
      snapshot({ conversation: { ...conversation, control_epoch: 3 } }),
      [old],
      conversation,
      true,
    ).length,
    1,
  )
  assert.equal(mergeSnapshotMessages(snapshot(), [old], conversation, false).length, 1)
})

test('local stop fencing never hides newly loaded history or restores a server redaction', () => {
  const blocked = new Set(['run'])
  assert.equal(mergeSnapshotMessages(snapshot(), [], null, false, blocked)[0].text, '你好😀')
  const prior = { ...message, text: '已显示内容' }
  const redacted = snapshot({ recent_messages: [{ ...message, text: '内容已不可访问' }] })
  assert.equal(
    mergeSnapshotMessages(redacted, [prior], conversation, true, blocked)[0].text,
    '内容已不可访问',
  )
})
