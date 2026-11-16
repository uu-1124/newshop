<template>
  <view class="catalog">
    <button size="mini" :disabled="disabled || busy" @click="toggle">{{ opened ? '收起商品筛选与比较' : '筛选商品与比较' }}</button>
    <view v-if="opened" class="catalog-content">
      <view class="filters">
        <input v-model="keyword" :maxlength="200" aria-label="商品关键词" placeholder="商品关键词" />
        <view class="price-inputs"><input v-model="minimum" type="digit" :maxlength="19" aria-label="最低价格" placeholder="最低价格" /><input v-model="maximum" type="digit" :maxlength="19" aria-label="最高价格" placeholder="最高价格" /></view>
        <picker :range="['全部品牌', ...brands.map(item => item.name)]" @change="brand = brands[Number($event.detail.value) - 1]?.id || ''"><view class="picker">{{ brands.find(item => item.id === brand)?.name || '全部品牌' }}</view></picker>
        <picker :range="['全部分类', ...categories.map(item => item.name)]" @change="category = categories[Number($event.detail.value) - 1]?.id || ''"><view class="picker">{{ categories.find(item => item.id === category)?.name || '全部分类' }}</view></picker>
        <input v-model="specification" :maxlength="100" aria-label="规格关键词" placeholder="规格关键词，例如黑色、128G" />
        <picker :range="sorts.map(item => item.label)" @change="sort = sorts[Number($event.detail.value)]!.value"><view class="picker">{{ sorts.find(item => item.value === sort)?.label }}</view></picker>
        <button size="mini" :disabled="busy" @click="inStock = !inStock">{{ inStock ? '仅看有库存：开' : '仅看有库存：关' }}</button>
        <button size="mini" :disabled="disabled || busy" @click="search(false)">按条件查询</button>
        <button size="mini" :disabled="busy" @click="reset">重置筛选</button>
      </view>
      <text v-if="error" role="alert" class="error">{{ error }}</text>
      <text v-if="searched && !items.length && !busy">未找到满足条件的商品，可调整筛选；不会用超预算商品替代结果。</text>
      <checkbox-group @change="chooseComparisons">
        <view v-for="product in items" :key="product.id" class="product-row">
          <label><checkbox :value="product.id" :checked="selected.includes(product.id)" :disabled="busy || (!selected.includes(product.id) && selected.length >= 4)" /><text>{{ product.name }}</text></label>
          <text>{{ product.brand || '品牌未提供' }} · 匹配规格最低价 ¥{{ product.price_from }}</text>
          <button size="mini" :disabled="busy" @click="showDetail(product.id)">查看真实参数与规格</button>
        </view>
      </checkbox-group>
      <button v-if="cursor" size="mini" :disabled="disabled || busy" @click="search(true)">加载更多商品</button>
      <button size="mini" :disabled="busy || selected.length < 2" @click="compare">比较已选商品（{{ selected.length }}/4）</button>
      <view v-if="detail" class="detail">
        <text class="title">{{ detail.name }}</text>
        <text class="muted">商品参数读取于 {{ new Date(detail.checked_at).toLocaleString() }}，结算以确认卡为准。</text>
        <view v-for="(fact, index) in detail.facts" :key="index">{{ fact.name }}：{{ fact.value || '未提供' }}</view>
        <text v-if="!detail.facts.length">商品参数未提供。</text>
        <button size="mini" :disabled="busy || disabled" @click="emit('consult', detail!.id)">新建此商品的规则咨询</button>
        <CartCards :products="[detail]" :actions="[]" :conversation="conversation" :disabled="disabled" @changed="emit('changed')" />
      </view>
      <view v-if="compared.length" class="comparison">
        <text class="title">本次商品比较</text>
        <picker :range="['优先可售最低价', '优先可售规格库存']" @change="criterion = Number($event.detail.value) === 0 ? 'price' : 'availability'"><view class="picker">{{ criterion === 'price' ? '优先可售最低价' : '优先可售规格库存' }}</view></picker>
        <scroll-view scroll-x class="comparison-scroll">
          <view class="comparison-table" :style="{ gridTemplateColumns: `120px repeat(${compared.length}, minmax(170px, 1fr))` }">
            <text class="cell">字段</text><text v-for="product in compared" :key="product.id" class="cell title">{{ product.name }}</text>
            <text class="cell">商品参考最低价</text><text v-for="product in compared" :key="'price'+product.id" class="cell">¥{{ product.price_from }}</text>
            <text class="cell">预算内可售数量</text><text v-for="product in compared" :key="'stock'+product.id" class="cell">{{ availability(product).quantity }}</text>
            <template v-for="name in factNames" :key="name"><text class="cell">{{ name }}</text><text v-for="product in compared" :key="name+product.id" class="cell">{{ product.facts.filter(fact => fact.name === name).map(fact => fact.value).join(' / ') || '未提供' }}</text></template>
          </view>
        </scroll-view>
        <text v-if="recommended">按当前选择依据推荐：{{ recommended.name }}。{{ criterion === 'price' ? '它在本次比较且符合预算、有库存的规格中价格最低。' : '它在本次比较中符合预算的可售规格库存总数最多。' }}</text>
        <text v-else>没有同时符合预算且有库存的商品，不能推荐购买。</text>
        <text class="muted">比较仅覆盖本次选择；未提供参数不能推断。库存或价格排序不代表性能、质量评分。</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { agent } from '@/services/agent'
import type { Conversation, ProductFilters, Product } from '@/services/agent'
import CartCards from './CartCards.vue'
const props = defineProps<{ conversation: Conversation; disabled: boolean }>()
const emit = defineEmits<{ changed: []; consult: [productId: string] }>()
const opened = ref(false), busy = ref(false), error = ref(''), searched = ref(false)
const keyword = ref(''), minimum = ref(''), maximum = ref(''), brand = ref(''), category = ref(''), specification = ref(''), sort = ref('newest'), inStock = ref(false)
const brands = ref<{ id: string; name: string }[]>([]), categories = ref<{ id: string; name: string }[]>([])
const items = ref<Product[]>([]), cursor = ref<string | null>(null), selected = ref<string[]>([])
const detail = ref<Product | null>(null), compared = ref<Product[]>([]), criterion = ref<'price' | 'availability'>('price')
const sorts = [{ value: 'newest', label: '最新上架' }, { value: 'price_asc', label: '匹配规格价格从低到高' }, { value: 'price_desc', label: '匹配规格价格从高到低' }, { value: 'sales', label: '销量从高到低' }]
let revision = 0, applied: ProductFilters = {}
watch(() => props.conversation.id, () => { revision++; opened.value = false; busy.value = false; detail.value = null; compared.value = []; selected.value = [] })
function amount(value: string) {
  if (!value.trim()) return undefined
  if (!/^(0|[1-9]\d{0,15})(\.\d{1,2})?$/.test(value.trim())) throw new Error('价格请输入非负金额，最多两位小数。')
  const [integer, fraction = ''] = value.trim().split('.')
  return `${integer}.${fraction.padEnd(2, '0')}`
}
function cents(value: string) { const [whole, part] = value.split('.'); return BigInt(whole!) * 100n + BigInt(part || '0') }
async function toggle() { opened.value = !opened.value; if (opened.value && !searched.value) await search(false) }
async function search(more: boolean) {
  if (busy.value || props.disabled) return
  const version = ++revision
  busy.value = true; error.value = ''
  try {
    if (!more) {
      const low = amount(minimum.value), high = amount(maximum.value)
      if (low && high && cents(low) > cents(high)) throw new Error('最低价不能超过最高价。')
      applied = { query: keyword.value.trim(), min_price: low, max_price: high, brand_id: brand.value, category_id: category.value, specification: specification.value.trim(), in_stock: inStock.value, sort: sort.value, limit: 10 }
    }
    const page = await agent.products({ ...applied, cursor: more ? cursor.value || undefined : undefined })
    if (version !== revision) return
    items.value = more ? [...new Map([...items.value, ...page.items].map(item => [item.id, item])).values()] : page.items
    cursor.value = page.next_cursor; brands.value = page.filters?.brands || []; categories.value = page.filters?.categories || []
    if (!more) { selected.value = []; detail.value = null; compared.value = [] }
    searched.value = true
  } catch (reason) { if (version === revision) error.value = reason instanceof Error ? reason.message : '商品查询失败' }
  finally { if (version === revision) busy.value = false }
}
async function reset() { keyword.value = ''; minimum.value = ''; maximum.value = ''; brand.value = ''; category.value = ''; specification.value = ''; sort.value = 'newest'; inStock.value = false; await search(false) }
function chooseComparisons(event: { detail: { value: string[] } }) { selected.value = event.detail.value.slice(0, 4); compared.value = [] }
async function showDetail(id: string) {
  if (busy.value) return
  const version = ++revision; busy.value = true; error.value = ''
  try { const product = await agent.product(id); if (version === revision) detail.value = product }
  catch (reason) { if (version === revision) error.value = reason instanceof Error ? reason.message : '商品详情不可用' }
  finally { if (version === revision) busy.value = false }
}
async function compare() {
  if (busy.value || selected.value.length < 2) return
  const version = ++revision; busy.value = true; error.value = ''; compared.value = []
  try { const products = await Promise.all(selected.value.map(id => agent.product(id))); if (version === revision) compared.value = products }
  catch (reason) { if (version === revision) error.value = reason instanceof Error ? reason.message : '部分商品已不可用，请重新选择' }
  finally { if (version === revision) busy.value = false }
}
const factNames = computed(() => [...new Set(compared.value.flatMap(product => product.facts.map(fact => fact.name)))].sort())
function availability(product: Product) {
  const skus = product.sku_options.filter(sku => sku.visible && sku.available_quantity > 0
    && (!applied.min_price || cents(sku.price) >= cents(applied.min_price))
    && (!applied.max_price || cents(sku.price) <= cents(applied.max_price))
    && (!applied.specification || sku.attributes.some(attribute => `${attribute.name} ${attribute.value}`.toLowerCase().includes(applied.specification!.toLowerCase()))))
  return { quantity: skus.reduce((sum, sku) => sum + sku.available_quantity, 0), price: skus.length ? skus.map(sku => cents(sku.price)).reduce((a, b) => a < b ? a : b) : null }
}
const recommended = computed(() => [...compared.value].filter(product => availability(product).quantity > 0).sort((a, b) => {
  const av = availability(a), bv = availability(b)
  if (criterion.value === 'availability' && av.quantity !== bv.quantity) return bv.quantity - av.quantity
  return av.price! < bv.price! ? -1 : av.price! > bv.price! ? 1 : a.id.localeCompare(b.id)
})[0])
</script>

<style scoped>
.catalog { margin: 12px 0; } .catalog-content, .filters, .detail, .comparison { display: grid; gap: 10px; padding: 12px; border: 1px solid #d5dbe5; border-radius: 10px; margin-top: 10px; }
.filters input { border: 1px solid #d5dbe5; border-radius: 6px; padding: 8px; font-size: 16px; min-height: 44px; width: auto; }
.price-inputs { display: flex; gap: 8px; } .price-inputs input { flex: 1; min-width: 0; } .picker { border: 1px solid #d5dbe5; border-radius: 6px; padding: 10px; }
.product-row { display: grid; gap: 8px; padding: 12px 0; border-bottom: 1px solid #e6e8ed; } .product-row label { display: flex; gap: 8px; align-items: center; }
.title { font-weight: 600; } .muted { color: #606b7c; font-size: 12px; } .error { color: #a4203d; } .comparison-scroll { width: 100%; } .comparison-table { display: grid; min-width: max-content; }
.cell { border: 1px solid #e6e8ed; padding: 10px; white-space: normal; font-size: 13px; }
</style>
