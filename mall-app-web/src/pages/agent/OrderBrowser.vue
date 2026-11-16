<template>
  <view class="orders">
    <button size="mini" :disabled="busy" @click="opened = !opened">{{
      opened ? '收起订单与售后' : '我的订单、物流与售后'
    }}</button>
    <view v-if="opened" class="panel">
      <text class="title">本人订单 · 支付与退款均为演示</text>
      <input
        v-model="orderSn"
        aria-label="订单号筛选"
        placeholder="订单号（选填）"
        :maxlength="80"
      />
      <picker
        :range="['全部状态', '待付款', '待发货', '已发货', '已完成', '已关闭', '无效订单']"
        @change="statusIndex = Number($event.detail.value)"
        ><view class="selector">{{
          ['全部状态', '待付款', '待发货', '已发货', '已完成', '已关闭', '无效订单'][statusIndex]
        }}</view></picker
      >
      <button size="mini" :disabled="busy" @click="list(false)">查询本人订单</button>
      <text v-if="searched && !items.length && !busy">暂无匹配订单。</text>
      <view v-for="order in items" :key="order.id" class="row"
        ><text>{{ order.order_sn }} · {{ order.status_label }} · ¥{{ order.pay_amount }}</text
        ><button size="mini" :disabled="busy" @click="select(order.id)"
          >读取订单、物流与售后</button
        ></view
      >
      <button v-if="cursor" size="mini" :disabled="busy" @click="list(true)">加载更多订单</button>
      <view v-if="selected" class="detail">
        <text class="title">{{ selected.order_sn }} · {{ selected.status_label }}</text
        ><text>应付/成交金额 ¥{{ selected.pay_amount }} · {{ selected.address_summary }}</text>
        <button size="mini" :disabled="busy" @click="select(selected!.id)"
          >刷新当前订单与处理结果</button
        >
        <button
          v-if="selected.status === 0"
          size="mini"
          :disabled="disabled || busy"
          @click="cancelOrder"
          >准备取消此未付款订单</button
        >
        <text class="title">演示物流</text
        ><text v-if="logistics"
          >{{ logistics.carrier || '尚无承运资料' }} ·
          {{ logistics.tracking_no || '尚无单号' }}</text
        >
        <text v-if="!logistics?.nodes.length">尚无演示物流节点，不代表真实物流状态。</text>
        <text v-for="node in logistics?.nodes || []" :key="node.id"
          >{{ new Date(node.occurred_at).toLocaleString() }} · {{ node.location }} ·
          {{ node.description }}</text
        >
        <view v-for="line in selected.lines" :key="line.order_item_id" class="row"
          ><text>{{ line.name }} × {{ line.quantity }} · 实付合计 ¥{{ line.paid_line_amount }}</text
          ><text class="muted"
            >明细 {{ line.order_item_id }} · {{ specification(line.specification) }}</text
          ><button size="mini" :disabled="disabled || busy" @click="eligibility(line.order_item_id)"
            >核验此明细退货资格</button
          ></view
        >
        <view v-if="returnInfo" class="return-form">
          <text class="muted">本次资格核验规则：{{ returnInfo.rule_version }}</text>
          <text
            >明细 {{ returnInfo.order_item_id }} ·
            {{
              returnInfo.eligible
                ? `剩余可退 ${returnInfo.remaining_quantity} 件，净额 ¥${returnInfo.remaining_amount}`
                : returnInfo.failure_reason
            }}</text
          >
          <text v-if="returnInfo.deadline"
            >申请截止 {{ new Date(returnInfo.deadline).toLocaleString() }}</text
          >
          <template v-if="returnInfo.eligible">
            <input
              v-model="quantity"
              type="number"
              aria-label="退货数量"
              placeholder="退货数量"
              :maxlength="3"
            />
            <picker
              :range="returnInfo.reason_codes.map((v) => v.label)"
              @change="reason = returnInfo!.reason_codes[Number($event.detail.value)]?.code || ''"
              ><view class="selector">{{
                returnInfo.reason_codes.find((v) => v.code === reason)?.label || '请选择退货原因'
              }}</view></picker
            >
            <textarea
              v-model="description"
              aria-label="退货说明"
              placeholder="补充说明（选填）"
              :maxlength="1000"
            />
            <button
              size="mini"
              :disabled="busy || disabled || proofs.length >= 5"
              @click="uploadProof"
              >上传图片凭证（{{ proofs.length }}/5）</button
            >
            <view v-for="proof in proofs" :key="proof.id" class="row"
              ><text>{{ proof.name }} · 已上传</text
              ><button
                size="mini"
                :disabled="busy"
                @click="proofs = proofs.filter((v) => v.id !== proof.id)"
                >移除凭证</button
              ></view
            >
            <button size="mini" :disabled="busy || disabled || !validReturn" @click="prepareReturn"
              >查看退货申请确认卡</button
            >
            <text class="muted"
              >金额取原订单明细实付净额；运费不计入退货金额。下一步须在确认卡明确提交，员工分别审核、收货、演示退款。</text
            >
          </template>
        </view>
        <text class="title">售后处理记录</text
        ><text v-if="!selected.returns.length">暂无申请。</text>
        <view v-for="returned in selected.returns" :key="returned.id" class="row"
          ><text
            >申请 {{ returned.id }} · {{ stageLabels[returned.stage] }} · {{ returned.quantity }}件
            · 申请 ¥{{ returned.requested_amount }}</text
          ><text>{{ returned.reason }} · 批准金额 {{ returned.approved_amount ?? '待审核' }}</text
          ><text v-if="returned.company_address"
            >退货地址：{{ returned.company_address.label }}</text
          ><text v-if="returned.stage === 'SIMULATED_REFUNDED'"
            >演示退款已完成，无真实资金流转。</text
          ></view
        >
      </view>
      <text v-if="error" class="error" role="alert">{{ error }}</text>
    </view>
  </view>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { agent, agentId } from '@/services/agent'
import type {
  Attachment,
  Conversation,
  OrderSummary,
  Logistics,
  ReturnEligibility,
} from '@/services/agent'
const props = defineProps<{
  conversation: Conversation
  disabled: boolean
  orderId?: string
  openRequest?: number
}>()
const emit = defineEmits<{ changed: [] }>()
const opened = ref(false),
  busy = ref(false),
  error = ref(''),
  searched = ref(false),
  orderSn = ref(''),
  statusIndex = ref(0),
  cursor = ref<string | null>(null)
const items = ref<OrderSummary[]>([]),
  selected = ref<OrderSummary | null>(null),
  logistics = ref<Logistics | null>(null),
  returnInfo = ref<ReturnEligibility | null>(null)
const quantity = ref('1'),
  reason = ref(''),
  description = ref(''),
  proofs = ref<Attachment[]>([])
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
const stageLabels: Record<string, string> = {
  REQUESTED: '待审核',
  APPROVED: '已批准待退货',
  GOODS_RECEIVED: '已收货待演示退款',
  SIMULATED_REFUNDED: '演示退款完成',
  REJECTED: '已拒绝',
  LEGACY: '旧申请，需人工核查',
}
const validReturn = computed(
  () =>
    !!reason.value &&
    /^[1-9][0-9]{0,2}$/.test(quantity.value) &&
    Number(quantity.value) <= (returnInfo.value?.remaining_quantity || 0),
)
let epoch = 0
const keys = new Map<string, string>()
function key(value: unknown) {
  const v = JSON.stringify(value)
  if (!keys.has(v)) keys.set(v, agentId())
  return keys.get(v)!
}
watch(
  () => props.conversation.id,
  () => {
    epoch++
    opened.value = false
    selected.value = null
    logistics.value = null
    returnInfo.value = null
    items.value = []
    proofs.value = []
    cursor.value = null
    busy.value = false
    error.value = ''
    searched.value = false
  },
)
watch(
  () => [props.orderId, props.openRequest] as const,
  ([id]) => {
    if (id) {
      opened.value = true
      void select(id)
    }
  },
  { immediate: true },
)
async function act(fn: (current: () => boolean) => Promise<void>) {
  if (busy.value) return
  const currentEpoch = epoch
  busy.value = true
  error.value = ''
  try {
    await fn(() => epoch === currentEpoch)
  } catch (e) {
    if (epoch === currentEpoch)
      error.value = e instanceof Error ? e.message : '读取失败，请刷新重试'
  } finally {
    if (epoch === currentEpoch) {
      busy.value = false
      emit('changed')
    }
  }
}
async function list(more: boolean) {
  await act(async (current) => {
    const filters = {
      order_sn: orderSn.value || undefined,
      status: statusIndex.value ? statusIndex.value - 1 : undefined,
      cursor: more ? cursor.value || undefined : undefined,
    }
    const result = await agent.orders(filters)
    if (!current()) return
    items.value = more
      ? [...items.value, ...result.items.filter((v) => !items.value.some((old) => old.id === v.id))]
      : result.items
    cursor.value = result.next_cursor
    searched.value = true
  })
}
async function select(id: string) {
  await act(async (current) => {
    const result = await agent.order(id)
    const log = await agent.logistics(id)
    if (!current()) return
    selected.value = result
    logistics.value = log
    returnInfo.value = null
    proofs.value = []
    opened.value = true
  })
}
async function eligibility(id: string) {
  if (!selected.value) return
  const oid = selected.value.id
  await act(async (current) => {
    const result = await agent.returnEligibility(oid, id)
    if (!current()) return
    returnInfo.value = result
    quantity.value = '1'
    reason.value = ''
    description.value = ''
    proofs.value = []
  })
}
async function cancelOrder() {
  if (!selected.value || props.disabled) return
  const c = props.conversation,
    p = { action_type: 'CANCEL_ORDER' as const, order_id: selected.value.id }
  await act(async () => {
    await agent.prepareBusiness(c, p, key([c.id, c.version, p]))
  })
}
async function prepareReturn() {
  if (!returnInfo.value || !selected.value || !validReturn.value || props.disabled) return
  const c = props.conversation
  const p = {
    action_type: 'CREATE_RETURN' as const,
    order_id: selected.value.id,
    order_item_id: returnInfo.value.order_item_id,
    quantity: Number(quantity.value),
    reason_code: reason.value,
    description: description.value,
    attachment_ids: proofs.value.map((v) => v.id),
  }
  await act(async (current) => {
    await agent.prepareBusiness(c, p, key([c.id, c.version, p]))
    if (current()) returnInfo.value = null
  })
}
function uploadProof() {
  if (props.disabled || busy.value) return
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/png,image/jpeg,image/webp'
  input.onchange = () => {
    const file = input.files?.[0]
    if (!file) return
    if (file.size > 5 * 1024 * 1024) {
      error.value = '每张凭证最多5MB'
      return
    }
    void act(async (current) => {
      const result = await agent.upload(file, file.name, agentId())
      if (current()) proofs.value.push(result)
    })
  }
  input.click()
}
</script>
<style scoped>
.orders {
  margin: 12px 0;
}
.panel,
.detail,
.return-form,
.row {
  display: grid;
  gap: 10px;
}
.panel {
  padding: 14px;
  border: 1px solid #b9ccec;
  border-radius: 10px;
  background: #fff;
  margin-top: 10px;
}
.row,
.return-form {
  border-top: 1px solid #e2e6ed;
  padding: 12px 0;
  overflow-wrap: anywhere;
}
.title {
  font-weight: 600;
}
.selector,
input,
textarea {
  border: 1px solid #d5dbe5;
  border-radius: 6px;
  padding: 9px;
  box-sizing: border-box;
  width: 100%;
}
.muted {
  font-size: 12px;
  color: #606b7c;
}
.error {
  color: #ac2444;
}
</style>
