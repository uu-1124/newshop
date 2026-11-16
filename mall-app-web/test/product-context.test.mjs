import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
const source = await readFile(new URL('../src/pages/agent/ProductContext.vue', import.meta.url), 'utf8')
const js = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'context' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function component(id, product) {
  const props = { productId: id }, hooks = {}, navigations = [], module = { exports: {} }
  vm.runInNewContext(js, { module, exports: module.exports, uni: { navigateTo: value => navigations.push(value.url) }, require(name) {
    if (name === 'vue') return { defineComponent: x => x, ref: value => ({ value }), watch: (_, fn) => { hooks.watch = fn; hooks.loaded = fn(id) }, onBeforeUnmount: fn => { hooks.unmount = fn } }
    if (name === '@/services/agent') return { agent: { product } }
    throw new Error(name)
  } })
  return { state: module.exports.default.setup(props, { expose() {} }), hooks, props, navigations }
}
test('context displays only the matching authenticated product and links to its exact id', async () => {
  const h = component('9007199254740993', async id => ({ id, name: '经核验的商品' }))
  await h.hooks.loaded
  assert.equal(h.state.name.value, '经核验的商品')
  h.state.open()
  assert.equal(h.navigations[0], '/pages/product/product?id=9007199254740993')
})
test('unavailable, mismatched and invalid product context cannot display or open a stale product', async () => {
  for (const [id, product] of [['40', async () => { throw new Error('not found') }], ['40', async () => ({ id: '45', name: '其他商品' })], ['9223372036854775808', async () => { throw new Error('must not call') }]]) {
    const h = component(id, product)
    await h.hooks.loaded
    assert.equal(h.state.name.value, '')
    assert.match(h.state.status.value, /暂不可读取/)
    h.state.open()
    assert.equal(h.navigations.length, 0)
  }
})
test('late reads cannot overwrite another product or an unmounted conversation', async () => {
  const pending = []
  const h = component('40', () => new Promise(done => { pending.push(done) }))
  h.props.productId = '45'
  const next = h.hooks.watch('45')
  pending[1]({ id: '45', name: '当前商品' })
  await next
  assert.equal(h.state.name.value, '当前商品')
  pending[0]({ id: '40', name: '过期商品' })
  await h.hooks.loaded
  assert.equal(h.state.name.value, '当前商品')
  // A separate unmounted conversation discards its pending request.
  let finish
  const old = component('40', () => new Promise(done => { finish = done }))
  old.hooks.unmount()
  finish({ id: '40', name: '过期商品' })
  await old.hooks.loaded
  assert.equal(old.state.name.value, '')
})
