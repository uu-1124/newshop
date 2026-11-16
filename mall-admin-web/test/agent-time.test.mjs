import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/agentTime.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText
const { shanghaiDateTimeInput, shanghaiInputToInstant, displayShanghaiTime } = await import(
  'data:text/javascript;base64,' + Buffer.from(javascript).toString('base64')
)

test('UTC and offset instants use Beijing calendar dates, seconds and milliseconds', () => {
  assert.equal(shanghaiDateTimeInput('2026-11-13T02:30:04.719870Z'), '2026-11-13T10:30:04.719')
  assert.equal(shanghaiDateTimeInput('2026-11-13T23:30:08.125-05:00'), '2026-11-14T12:30:08.125')
  assert.equal(shanghaiInputToInstant('2026-11-14T00:00:00.001'), '2026-11-13T16:00:00.001Z')
})

test('description-only corrections preserve the full original microsecond value', () => {
  const original = '2026-11-13T02:30:04.719870Z'
  assert.equal(shanghaiInputToInstant(shanghaiDateTimeInput(original), original), original)
  assert.equal(
    shanghaiInputToInstant('2026-11-13T10:31:04.719', original),
    '2026-11-13T02:31:04.719Z',
  )
})

test('invalid dates, missing timezone and invalid clock times are refused', () => {
  for (const value of [
    '2026-02-29T10:30',
    '2026-04-31T10:30',
    '2026-11-13T24:01',
    '2026-11-13T10:30:61',
    '2026-13-01T10:30',
    '0000-01-01T00:00',
    '',
    '2026-11-13T10:30Z',
  ]) {
    assert.throws(() => shanghaiInputToInstant(value), /北京时间/)
  }
  assert.throws(() => shanghaiDateTimeInput('2026-11-13T02:30:04'), /时区/)
})

test('valid leap day and minute-only input round-trip without browser timezone dependence', () => {
  assert.equal(shanghaiInputToInstant('2028-02-29T00:01'), '2028-02-28T16:01:00.000Z')
  assert.match(displayShanghaiTime('2026-11-13T16:01:00Z'), /2026\/11\/14.*00:01:00/)
})
