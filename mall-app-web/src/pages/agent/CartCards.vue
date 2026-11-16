<template>
  <view class="cart-cards">
    <view v-for="p in products" :key="p.id" class="product-card">
      <text class="product-name">{{ p.name }}</text>
      <text class="muted">{{ p.brand }} · 商品参考价 ¥{{ p.price_from }}</text>
      <picker :range="p.sku_options.map(skuLabel)" @change="choose(p, $event)">
        <view class="selector">{{
          selectedSku(p) ? skuLabel(selectedSku(p)!) : '点击选择商品规格'
        }}</view>
      </picker>
      <view class="quantity"
        ><text>数量</text
        ><input v-model="quantities[p.id]" type="number" placeholder="请输入数量" :maxlength="3"
      /></view>
      <button
        size="mini"
        :disabled="busy || disabled || !selectedSku(p) || !validQuantity(p)"
        @click="prepare(p)"
        >查看加购确认卡</button
      >
      <OrderCheckout
        v-if="selectedSku(p) && validQuantity(p)"
        :conversation="conversation"
        :disabled="disabled || busy"
        :direct-items="[
          { product_id: p.id, sku_id: selectedSku(p)!.id, quantity: Number(quantities[p.id]) },
        ]"
        @changed="emit('changed')"
      />
    </view>
    <view v-for="action in presentedActions" :key="action.id" class="action-card">
      <text class="product-name"
        >{{ actionNames[action.action_type] }} ·
        {{
          labels[
            action.state === 'PENDING_CONFIRMATION' && expired(action) ? 'EXPIRED' : action.state
          ]
        }}</text
      >
      <template v-if="action.action_type === 'CANCEL_ORDER'">
        <template v-if="cancellationReady(action)">
          <text>订单号：{{ action.quote.cancellation!.order_sn }}</text>
          <text>确认时订单金额 ¥{{ action.quote.cancellation!.pay_amount }}</text>
          <text v-if="action.state === 'PENDING_CONFIRMATION' && !expired(action)" class="muted"
            >准备确认时订单为待付款。确认后关闭订单并按商城规则释放已占用资源，不产生付款或退款。</text
          >
          <text v-else-if="action.state === 'SUCCEEDED'" class="muted"
            >订单已取消，商城已按执行结果处理占用资源，不产生付款或退款。</text
          >
        </template>
        <text v-else class="error">此旧确认卡缺少订单号或金额，请重新读取订单并准备取消。</text>
      </template>
      <template v-if="action.parameters.action_type === 'CREATE_RETURN'"
        ><text
          >订单 {{ action.parameters.order_id }} · 明细 {{ action.parameters.order_item_id }} ·
          退货数量 {{ action.parameters.quantity }}</text
        ><text
          >原因编号 {{ action.parameters.reason_code }} ·
          {{ action.parameters.description || '未补充说明' }}</text
        ><text
          >凭证 {{ action.parameters.attachment_ids.length }} 张 · 申请金额 ¥{{
            action.quote.pricing.payable
          }}</text
        ><text class="muted"
          >提交后等待员工审核、收货与独立演示退款，不会调用真实退款渠道。</text
        ></template
      >
      <view v-for="line in action.quote.lines" :key="line.sku_id" class="line">
        <text>{{ line.name }} × {{ line.quantity }}</text>
        <text class="muted">{{ specification(line.specification) }}</text
        ><text>单价 ¥{{ line.unit_price }} · 小计 ¥{{ line.line_payable }}</text>
      </view>
      <template v-if="action.action_type === 'CREATE_ORDER'">
        <text>收货地址：{{ action.quote.address_summary }}</text>
        <text
          >商品合计 ¥{{ action.quote.pricing.subtotal }} · 运费 ¥{{
            action.quote.pricing.freight
          }}</text
        >
        <text
          >活动优惠 ¥{{ action.quote.pricing.promotion_discount }} · 优惠券 ¥{{
            action.quote.pricing.coupon_discount
          }}
          · 积分抵扣 ¥{{ action.quote.pricing.points_discount }}</text
        >
        <text class="product-name">应付金额 ¥{{ action.quote.pricing.payable }}</text>
        <text class="muted"
          >确认后创建待付款订单，并锁定库存、使用所选优惠券和积分。付款需另行操作。</text
        >
      </template>
      <text v-else-if="action.action_type === 'ADD_CART'"
        >商品参考合计 ¥{{ action.quote.pricing.payable }}</text
      >
      <text v-if="action.action_type === 'ADD_CART'" class="muted"
        >加购不会下单或付款。结算优惠和最终金额以商城结算页为准。</text
      >
      <template v-if="action.state === 'PENDING_CONFIRMATION' && !expired(action)">
        <text class="muted"
          >请在
          {{ new Date(action.expires_at).toLocaleTimeString() }}
          前确认；商品状态变化时需重新准备。</text
        >
        <view class="buttons"
          ><button
            size="mini"
            :disabled="busy || disabled || !cancellationReady(action)"
            @click="command(action, true)"
            >{{ confirmNames[action.action_type] }}</button
          ><button size="mini" :disabled="busy || disabled" @click="command(action, false)"
            >取消本次操作</button
          ></view
        >
      </template>
      <text v-else-if="expired(action) && action.state === 'PENDING_CONFIRMATION'"
        >确认卡已过期，{{ reprepareHints[action.action_type] }}</text
      >
      <text v-if="['EXECUTING', 'UNKNOWN'].includes(action.state)"
        >正在核对原操作结果，请等待核对完成。</text
      >
      <text v-if="action.state === 'SUCCEEDED' && action.action_type === 'ADD_CART'"
        >已加入购物车；执行完成时购物车共有 {{ action.result?.cart_quantity }} 件商品。</text
      >
      <text v-if="action.state === 'SUCCEEDED' && action.action_type === 'CREATE_ORDER'"
        >订单 {{ action.result?.order?.order_sn }} 已创建，创建时应付 ¥{{
          action.result?.order?.pay_amount
        }}。</text
      >
      <text
        v-if="['FAILED', 'STALE', 'CANCELLED', 'EXPIRED'].includes(action.state)"
        class="muted"
        >{{
          action.state === 'STALE' && action.error_code === 'MEMBER_CONTEXT_CHANGED'
            ? '你发送了新的消息，此卡已失效。请按最新选择重新准备，旧卡不会执行。'
            : action.state === 'FAILED'
              ? '本次操作未完成，请按当前商品、地址和优惠重新准备确认。'
              : action.state === 'EXPIRED'
                ? `确认卡已过期，${reprepareHints[action.action_type]}`
                : action.state === 'STALE'
                  ? '会话状态已变化，此卡已失效。人工接待期间请向客服说明需求；旧卡不会执行。'
                  : '本次确认卡已失效，可按当前会话状态重新准备。'
        }}</text
      >
      <button
        v-if="['SUCCEEDED', 'EXECUTING', 'UNKNOWN'].includes(action.state)"
        size="mini"
        :disabled="busy"
        @click="refresh(action)"
        >刷新操作结果</button
      >
      <button
        v-if="action.state === 'SUCCEEDED' && action.action_type === 'ADD_CART'"
        size="mini"
        @click="openCart"
        >查看我的购物车</button
      >
      <button
        v-if="action.state === 'SUCCEEDED' && action.result?.order"
        size="mini"
        @click="openOrder(action)"
        >查看订单</button
      >
      <text v-if="action.result?.return_apply"
        >申请编号 {{ action.result.return_apply.id }} · 提交时状态
        {{ action.result.return_apply.stage }} · 退款仅为演示，须另行处理</text
      >
    </view>
    <text v-if="error" class="error" role="alert">{{ error }}</text>
  </view>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { agent, agentId } from '@/services/agent'
import type { Action, Conversation, Product, Sku } from '@/services/agent'
import OrderCheckout from './OrderCheckout.vue'
const props = defineProps<{
  products: Product[]
  actions: Action[]
  conversation: Conversation
  disabled: boolean
}>()
const emit = defineEmits<{ changed: [] }>()
const selections = ref<Record<string, string>>({}),
  quantities = ref<Record<string, string>>({})
const busy = ref(false),
  error = ref(''),
  now = ref(Date.now())
const overrides = ref<Record<string, Action>>({})
const presentedActions = computed(() =>
  props.actions.map((action) =>
    (overrides.value[action.id]?.version || 0) > action.version
      ? overrides.value[action.id]
      : action,
  ),
)
const timer = setInterval(() => {
  now.value = Date.now()
}, 1000)
onUnmounted(() => clearInterval(timer))
const labels: Record<string, string> = {
  PENDING_CONFIRMATION: '待你确认',
  EXECUTING: '处理中',
  UNKNOWN: '核对中',
  SUCCEEDED: '已完成',
  FAILED: '未完成',
  CANCELLED: '已取消',
  EXPIRED: '已过期',
  STALE: '已失效',
}
const actionNames = {
  ADD_CART: '加入购物车',
  CREATE_ORDER: '创建订单',
  CANCEL_ORDER: '取消未付款订单',
  CREATE_RETURN: '提交退货申请',
}
const confirmNames = {
  ADD_CART: '确认加入购物车',
  CREATE_ORDER: '确认创建订单',
  CANCEL_ORDER: '确认取消订单',
  CREATE_RETURN: '确认提交退货申请',
}
const reprepareHints = {
  ADD_CART: '请重新核对商品、规格和数量，准备加购卡。',
  CREATE_ORDER: '请重新核对商品、地址和优惠，准备订单卡。',
  CANCEL_ORDER: '请重新读取订单状态和金额，准备取消卡。',
  CREATE_RETURN: '请重新核验明细资格、数量、原因和凭证，准备退货卡。',
}
const keys = new Map<string, string>()
function key(fingerprint: string) {
  if (!keys.has(fingerprint)) keys.set(fingerprint, agentId())
  return keys.get(fingerprint)!
}
function skuLabel(s: Sku) {
  return `${s.attributes.map((a) => `${a.name}：${a.value}`).join(' / ') || '默认规格'} · ¥${
    s.price
  } · 可用 ${s.available_quantity}`
}
function specification(value: string) {
  try {
    const attributes = JSON.parse(value)
    if (Array.isArray(attributes))
      return (
        attributes
          .filter((a) => typeof a?.key === 'string' && typeof a?.value === 'string')
          .map((a) => `${a.key}：${a.value}`)
          .join(' / ') || '默认规格'
      )
  } catch {}
  return value || '默认规格'
}
function selectedSku(p: Product) {
  return p.sku_options.find((s) => s.id === selections.value[p.id])
}
function choose(p: Product, event: { detail: { value: string | number } }) {
  const sku = p.sku_options[Number(event.detail.value)]
  if (sku) {
    selections.value[p.id] = sku.id
    quantities.value[p.id] ||= '1'
  }
}
function validQuantity(p: Product) {
  const q = Number(quantities.value[p.id])
  return (
    Number.isInteger(q) && q >= 1 && q <= Math.min(999, selectedSku(p)?.available_quantity || 0)
  )
}
function expired(action: Action) {
  return now.value >= Date.parse(action.expires_at)
}
function cancellationReady(action: Action) {
  if (action.parameters.action_type !== 'CANCEL_ORDER') return true
  const summary = action.quote.cancellation
  return !!(
    summary &&
    summary.id === action.parameters.order_id &&
    summary.status === 0 &&
    summary.order_sn.trim() &&
    summary.pay_amount === action.quote.pricing.payable &&
    action.quote.rule_version === 'CANCEL_PENDING_V2'
  )
}
async function act(fn: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作暂不可用，请刷新核对'
  } finally {
    busy.value = false
    emit('changed')
  }
}
async function prepare(p: Product) {
  const sku = selectedSku(p)
  if (!sku || !validQuantity(p) || props.disabled) return
  const c = props.conversation,
    quantity = Number(quantities.value[p.id])
  const fingerprint = JSON.stringify({
    conversation: c.id,
    version: c.version,
    sku: sku.id,
    quantity,
  })
  await act(() => agent.prepareCart(c, p, sku, quantity, key(fingerprint)))
}
async function command(action: Action, confirm: boolean) {
  // An event queued from an older render must use the latest projected card.
  const current = presentedActions.value.find((item) => item.id === action.id)
  if (
    props.disabled ||
    !current ||
    current.state !== 'PENDING_CONFIRMATION' ||
    Date.now() >= Date.parse(current.expires_at) ||
    (confirm && !cancellationReady(current))
  )
    return
  await act(async () => {
    overrides.value[action.id] = await (confirm
      ? agent.confirmAction(current, key(`confirm:${current.id}:${current.version}`))
      : agent.cancelAction(current, key(`cancel:${current.id}:${current.version}`)))
  })
}
async function refresh(action: Action) {
  await act(async () => {
    overrides.value[action.id] = await agent.action(action.id)
  })
}
function openOrder(action: Action) {
  const id = action.result?.order?.id
  if (id && /^[1-9][0-9]*$/.test(id))
    uni.navigateTo({ url: `/pages/order/orderDetail?orderId=${id}` })
}
function openCart() {
  uni.switchTab({ url: '/pages/cart/cart' })
}
</script>

<style scoped>
.cart-cards {
  display: grid;
  gap: 12px;
  margin: 10px 0;
}
.product-card,
.action-card {
  padding: 14px;
  border: 1px solid #dedee8;
  border-radius: 10px;
  background: #fff;
  display: grid;
  gap: 10px;
}
.action-card {
  background: #f7faff;
  border-color: #b9ccec;
}
.product-name {
  font-weight: 600;
}
.muted {
  display: block;
  color: #606b7c;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.selector {
  padding: 10px;
  border: 1px solid #d5dbe5;
  border-radius: 6px;
}
.quantity,
.buttons {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.quantity input {
  width: 90px;
  border: 1px solid #d5dbe5;
  padding: 6px;
  border-radius: 6px;
}
.line {
  display: grid;
  gap: 6px;
  border-bottom: 1px solid #e2e6ed;
  padding-bottom: 10px;
}
.error {
  color: #ac2444;
}
</style>
