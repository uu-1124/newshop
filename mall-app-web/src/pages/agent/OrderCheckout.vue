<template>
  <view class="checkout">
    <button size="mini" :disabled="disabled || busy" @click="load">{{
      directItems?.length ? '直接购买：读取地址与优惠' : '选择购物车商品并结算'
    }}</button>
    <view v-if="opened" class="checkout-form">
      <text class="title">准备订单</text>
      <text v-if="!cart.length && !directItems?.length"
        >购物车暂无商品，可先在对话中选择商品加购。</text
      >
      <text v-if="directItems?.length">直接购买所选规格和数量，不修改购物车。支付仅演示。</text>
      <checkbox-group v-if="cart.length" @change="selectItems">
        <label v-for="item in cart" :key="item.id" class="cart-item">
          <checkbox
            :value="String(item.id)"
            :checked="selected.includes(String(item.id))"
            :disabled="busy || disabled"
          />
          <text>{{ item.productName }} × {{ item.quantity }}</text>
        </label>
      </checkbox-group>
      <button
        v-if="cart.length"
        size="mini"
        :disabled="disabled || busy || !selected.length"
        @click="loadOptions"
        >读取收货地址与优惠</button
      >
      <template v-if="options">
        <picker
          :range="options.addresses.map((a) => a.label)"
          @change="address = options!.addresses[Number($event.detail.value)]?.id || ''"
        >
          <view class="selector">{{
            options.addresses.find((a) => a.id === address)?.label || '请选择收货地址'
          }}</view>
        </picker>
        <button v-if="!options.addresses.length" size="mini" @click="manageAddress"
          >去添加收货地址</button
        >
        <picker
          :range="['不使用优惠券', ...options.coupons.map((c) => `${c.name} · 面额 ¥${c.amount}`)]"
          @change="chooseCoupon"
        >
          <view class="selector">{{
            options.coupons.find((c) => c.id === coupon)?.name || '不使用优惠券'
          }}</view>
        </picker>
        <text class="muted"
          >可用积分 {{ options.available_points }}。{{ options.points_rule_description }}</text
        >
        <view class="points"
          ><text>使用积分</text
          ><input v-model="points" type="number" :maxlength="9" aria-label="使用积分"
        /></view>
        <button
          size="mini"
          :disabled="disabled || busy || !address || !validPoints"
          @click="prepare"
          >查看订单确认卡</button
        >
        <text class="muted"
          >确认卡将显示实际优惠和应付金额；点击卡片上的“确认创建订单”后才会下单。</text
        >
      </template>
      <button size="mini" :disabled="busy" @click="opened = false">收起结算</button>
    </view>
    <text v-if="error" class="error" role="alert">{{ error }}</text>
  </view>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { agent, agentId } from '@/services/agent'
import type { CartItem, Conversation, CheckoutOptions, SelectedItem } from '@/services/agent'
const props = defineProps<{
  conversation: Conversation
  disabled: boolean
  directItems?: SelectedItem[]
}>()
const emit = defineEmits<{ changed: [] }>()
const opened = ref(false),
  busy = ref(false),
  error = ref('')
const cart = ref<CartItem[]>([]),
  selected = ref<string[]>([]),
  options = ref<CheckoutOptions | null>(null)
const address = ref(''),
  coupon = ref<string | null>(null),
  points = ref('0')
const validPoints = computed(
  () =>
    /^\d+$/.test(points.value) &&
    Number(points.value) <= Math.min(100000000, options.value?.available_points || 0),
)
const keys = new Map<string, string>()
watch(
  () => props.conversation.id,
  () => {
    opened.value = false
    options.value = null
    selected.value = []
    error.value = ''
  },
)
watch(
  () => JSON.stringify(props.directItems),
  () => {
    opened.value = false
    options.value = null
    address.value = ''
    coupon.value = null
    points.value = '0'
  },
)
async function act(fn: () => Promise<void>) {
  if (busy.value || props.disabled) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '订单准备暂不可用，请刷新'
  } finally {
    busy.value = false
    emit('changed')
  }
}
async function load() {
  await act(async () => {
    options.value = null
    selected.value = []
    if (props.directItems?.length) {
      options.value = await agent.directOptions(props.directItems, agentId())
      address.value = ''
      coupon.value = null
      points.value = '0'
    } else cart.value = await agent.cart()
    opened.value = true
  })
}
function selectItems(event: { detail: { value: string[] } }) {
  selected.value = event.detail.value
  options.value = null
  address.value = ''
  coupon.value = null
  points.value = '0'
}
async function loadOptions() {
  await act(async () => {
    options.value = await agent.checkoutOptions(selected.value, agentId())
    address.value = ''
    coupon.value = null
    points.value = '0'
  })
}
function chooseCoupon(event: { detail: { value: string | number } }) {
  coupon.value = options.value?.coupons[Number(event.detail.value) - 1]?.id || null
}
async function prepare() {
  if (!validPoints.value || !address.value) return
  await act(async () => {
    const selection = props.directItems?.length
      ? { source: 'DIRECT' as const, items: props.directItems }
      : { source: 'CART' as const, cart_item_ids: [...selected.value] }
    const params = {
      action_type: 'CREATE_ORDER' as const,
      ...selection,
      address_id: address.value,
      coupon_id: coupon.value,
      use_points: Number(points.value),
    }
    const fingerprint = JSON.stringify({
      params,
      conversation: props.conversation.id,
      version: props.conversation.version,
    })
    if (!keys.has(fingerprint)) keys.set(fingerprint, agentId())
    await agent.prepareOrder(props.conversation, params, keys.get(fingerprint)!)
    opened.value = false
    options.value = null
  })
}
function manageAddress() {
  uni.navigateTo({ url: '/pages/address/address' })
}
</script>

<style scoped>
.checkout {
  margin: 12px 0;
}
.checkout-form {
  display: grid;
  gap: 12px;
  margin-top: 10px;
  padding: 14px;
  border: 1px solid #b9ccec;
  border-radius: 10px;
  background: #fff;
}
.title {
  font-weight: 600;
}
.cart-item {
  display: flex;
  align-items: center;
  padding: 8px 0;
  gap: 8px;
}
.selector {
  border: 1px solid #d5dbe5;
  border-radius: 6px;
  padding: 10px;
}
.points {
  display: flex;
  align-items: center;
  gap: 12px;
}
.points input {
  border: 1px solid #d5dbe5;
  width: 100px;
  padding: 6px;
}
.muted {
  color: #606b7c;
  font-size: 12px;
}
.error {
  color: #ac2444;
}
</style>
