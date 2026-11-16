import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import test from 'node:test'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = await readFile(new URL('../src/pages/product/product.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const code = ts.transpileModule(compileScript(descriptor, { id: 'product-support' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

test('the product page support action forwards only a valid current product ID', () => {
  const navigations = [], module = { exports: {} }
  vm.runInNewContext(code, {
    module, exports: module.exports, BigInt, console,
    uni: { navigateTo: value => navigations.push(value) },
    require(name) {
      if (name === '@dcloudio/uni-app') return { onLoad() {}, onPageScroll() {} }
      if (name === '@/stores/member') return { useMemberStore: () => ({ hasLogin: false }) }
      if (name.startsWith('@/')) return {}
      return require(name)
    },
  })
  const state = module.exports.default.setup({}, { expose() {} })
  for (const id of [undefined, 0, '9223372036854775808', '45?orderId=40']) {
    state.product.value.id = id
    state.openProductSupport()
  }
  assert.equal(navigations.length, 0)
  state.product.value.id = '9007199254740993'
  state.openProductSupport()
  assert.equal(navigations[0].url, '/pages/agent/chat?productId=9007199254740993')
})
