<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { operationsApi, receptionApi, requestKey } from '@/apis/agent'
import ReturnRules from './ReturnRules.vue'
import type { Logistics, LogisticsNode, ReturnAddress, ReturnSummary } from '@/apis/agent-contracts'
import {
  displayShanghaiTime,
  shanghaiDateTimeInput,
  shanghaiInputToInstant,
} from '@/utils/agentTime'
const busy = ref(false),
  error = ref(''),
  stage = ref('REQUESTED'),
  items = ref<ReturnSummary[]>([]),
  next = ref<string | null>(null),
  current = ref<ReturnSummary | null>(null)
const addresses = ref<ReturnAddress[]>([]),
  address = ref(''),
  amount = ref(''),
  note = ref('')
const orderId = ref(''),
  logistics = ref<Logistics | null>(null),
  carrier = ref(''),
  tracking = ref(''),
  occurred = ref(''),
  location = ref(''),
  description = ref(''),
  edit = ref<LogisticsNode | null>(null),
  editReason = ref('')
const me = ref<{ permissions: string[] } | null>(null),
  images = ref<Record<string, string>>({})
const labels: Record<string, string> = {
  REQUESTED: '待审核',
  APPROVED: '已批准待退货',
  GOODS_RECEIVED: '已收货待演示退款',
  SIMULATED_REFUNDED: '演示退款完成',
  REJECTED: '已拒绝',
  LEGACY: '旧申请：人工核查',
}
const keys = new Map<string, string>()
function key(value: unknown) {
  const f = JSON.stringify(value)
  if (!keys.has(f)) keys.set(f, requestKey())
  return keys.get(f)!
}
function allowed(permission: string) {
  return !!me.value?.permissions.includes(permission)
}
async function act(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '处理失败，请刷新'
  } finally {
    busy.value = false
  }
}
async function list(more = false) {
  await act(async () => {
    const result = await operationsApi.returns(stage.value, more ? next.value || '' : '')
    items.value = more
      ? [...items.value, ...result.items.filter(v => !items.value.some(old => old.id === v.id))]
      : result.items
    next.value = result.next_before
  })
}
let imageAbort: AbortController | null = null
function clearImages() {
  imageAbort?.abort()
  Object.values(images.value).forEach(URL.revokeObjectURL)
  images.value = {}
}
async function loadDetail(id: string) {
  clearImages()
  current.value = await operationsApi.detail(id)
  amount.value = current.value.requested_amount
  note.value = ''
  address.value = ''
  if (allowed('agent:return:review')) addresses.value = (await operationsApi.addresses()).items
  const target = current.value.id
  imageAbort = new AbortController()
  for (const proof of current.value.proofs || []) {
    const blob = await receptionApi.image(proof.attachment_id, imageAbort.signal)
    if (current.value?.id === target) images.value[proof.attachment_id] = URL.createObjectURL(blob)
  }
}
async function select(id: string) {
  await act(() => loadDetail(id))
}
async function transition(kind: 'APPROVE' | 'REJECT' | 'RECEIVE' | 'REFUND') {
  if (!current.value || !note.value.trim()) return
  const c = current.value,
    n = note.value.trim()
  try {
    await ElMessageBox.confirm(
      kind === 'REFUND'
        ? '确认单独记录演示退款完成？不会调用真实支付渠道，也不会向会员账户转入真实资金。'
        : `确认${{ APPROVE: '批准申请', REJECT: '拒绝申请', RECEIVE: '登记退货收货', REFUND: '演示退款' }[kind]}？`,
      '处理确认',
      { type: 'warning', confirmButtonText: '确认处理', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await act(async () => {
    const k = key([kind, c.id, c.version, amount.value, address.value, n])
    if (kind === 'APPROVE' || kind === 'REJECT') {
      const body =
        kind === 'APPROVE'
          ? {
              expected_version: c.version,
              decision: kind,
              approved_amount: amount.value,
              company_address_id: address.value,
              note: n,
            }
          : { expected_version: c.version, decision: kind, note: n }
      await operationsApi.review(c.id, body, k)
    } else if (kind === 'RECEIVE') await operationsApi.receive(c.id, c.version, n, k)
    else await operationsApi.refund(c.id, c.version, n, k)
    await loadDetail(c.id)
    const result = await operationsApi.returns(stage.value)
    items.value = result.items
    next.value = result.next_before
    ElMessage.success('处理已保存')
  })
}
async function readLogistics() {
  await act(async () => {
    const result = await operationsApi.logistics(orderId.value)
    logistics.value = result
    carrier.value = result.carrier || ''
    tracking.value = result.tracking_no || ''
    resetNodeDraft()
  })
}
async function saveShipment() {
  if (!logistics.value) return
  const s = logistics.value
  await act(async () => {
    logistics.value = await operationsApi.updateLogistics(
      s.order_id,
      s.version,
      carrier.value,
      tracking.value,
      key(['shipment', s.order_id, s.version, carrier.value, tracking.value]),
    )
  })
}
function resetNodeDraft() {
  edit.value = null
  occurred.value = ''
  location.value = ''
  description.value = ''
  editReason.value = ''
}
function chooseNode(node: LogisticsNode) {
  edit.value = node
  occurred.value = shanghaiDateTimeInput(node.occurred_at)
  location.value = node.location
  description.value = node.description
  editReason.value = ''
}
async function saveNode() {
  const s = logistics.value
  if (!s?.shipment_id || !occurred.value) return
  const sid = s.shipment_id
  await act(async () => {
    const instant = shanghaiInputToInstant(occurred.value, edit.value?.occurred_at)
    const k = key([
      'node',
      s.shipment_id,
      s.version,
      edit.value?.id,
      instant,
      location.value,
      description.value,
      editReason.value,
    ])
    if (edit.value)
      await operationsApi.editNode(
        sid,
        edit.value.id,
        {
          expected_version: edit.value.version,
          occurred_at: instant,
          location: location.value,
          description: description.value,
          reason: editReason.value,
        },
        k,
      )
    else await operationsApi.node(sid, s.version, instant, location.value, description.value, k)
    logistics.value = await operationsApi.logistics(s.order_id)
    resetNodeDraft()
  })
}
onMounted(async () => {
  await act(async () => {
    me.value = await receptionApi.me()
  })
  if (allowed('agent:return:read')) await list()
})
onUnmounted(clearImages)
</script>
<template>
  <main class="operations">
    <h1>订单售后与演示物流</h1>
    <el-alert
      title="支付与退款均为演示。审核、收货、演示退款分别记录，不连接真实资金渠道。"
      type="info"
      :closable="false"
    />
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <ReturnRules v-if="allowed('agent:return:read')" :can-publish="allowed('agent:return:review')" />
    <section v-if="allowed('agent:return:read')">
      <h2>售后申请</h2>
      <div class="controls">
        <el-select v-model="stage" aria-label="售后阶段"
          ><el-option label="全部阶段" value="" /><el-option
            v-for="(label, value) in labels"
            :key="value"
            :label="label"
            :value="value" /></el-select
        ><el-button :loading="busy" @click="list(false)">查询申请</el-button>
      </div>
      <el-table :data="items" stripe
        ><el-table-column prop="id" label="申请编号" width="120" /><el-table-column
          prop="order_id"
          label="订单编号"
          width="120"
        /><el-table-column label="阶段"
          ><template #default="scope">{{ labels[scope.row.stage] }}</template></el-table-column
        ><el-table-column prop="quantity" label="数量" /><el-table-column
          prop="requested_amount"
          label="申请金额"
        /><el-table-column label="操作"
          ><template #default="scope"
            ><el-button :disabled="busy" @click="select(scope.row.id)"
              >查看处理</el-button
            ></template
          ></el-table-column
        ></el-table
      ><el-button v-if="next" :disabled="busy" @click="list(true)">加载更多申请</el-button>
      <article v-if="current">
        <h3>申请 {{ current.id }} · {{ labels[current.stage] }}</h3>
        <p>
          订单 {{ current.order_id }} · 明细 {{ current.order_item_id || '旧数据无法确定' }} ·
          {{ current.quantity }}件
        </p>
        <p>
          申请 ¥{{ current.requested_amount }} · 批准 ¥{{ current.approved_amount ?? '待审核' }}
        </p>
        <p>{{ current.reason }} · {{ current.description }}</p>
        <p>申请时规则：{{ current.rule_version || '历史申请未记录' }}</p>
        <div class="proofs">
          <a
            v-for="proof in current.proofs || []"
            :key="proof.attachment_id"
            :href="images[proof.attachment_id]"
            target="_blank"
            rel="noopener"
            ><img
              v-if="images[proof.attachment_id]"
              :src="images[proof.attachment_id]"
              alt="退货图片凭证"
          /></a>
        </div>
        <p v-if="current.company_address">退货地址：{{ current.company_address.label }}</p>
        <p>{{ current.handle_note }} {{ current.receive_note }}</p>
        <p v-if="current.simulated_refund_no">
          演示退款编号 {{ current.simulated_refund_no }}，无真实资金流转。
        </p>
        <template
          v-if="
            current.stage !== 'LEGACY' &&
            !['REJECTED', 'SIMULATED_REFUNDED'].includes(current.stage)
          "
          ><el-input
            v-model="note"
            type="textarea"
            :maxlength="1000"
            aria-label="处理说明"
            placeholder="必填：处理说明"
          />
          <div
            v-if="current.stage === 'REQUESTED' && allowed('agent:return:review')"
            class="controls"
          >
            <el-input v-model="amount" aria-label="批准退款金额" placeholder="批准金额" /><el-select
              v-model="address"
              aria-label="退货收货地址"
              ><el-option
                v-for="a in addresses"
                :key="a.id"
                :label="a.label"
                :value="a.id" /></el-select
            ><el-button :disabled="busy || !note.trim() || !address" @click="transition('APPROVE')"
              >批准退货</el-button
            ><el-button :disabled="busy || !note.trim()" @click="transition('REJECT')"
              >拒绝申请</el-button
            >
          </div>
          <el-button
            v-if="current.stage === 'APPROVED' && allowed('agent:return:receive')"
            :disabled="busy || !note.trim()"
            @click="transition('RECEIVE')"
            >单独登记收到退货</el-button
          ><el-button
            v-if="current.stage === 'GOODS_RECEIVED' && allowed('agent:return:refund')"
            type="warning"
            :disabled="busy || !note.trim()"
            @click="transition('REFUND')"
            >独立确认演示退款</el-button
          ></template
        >
        <p v-else-if="current.stage === 'LEGACY'">
          旧申请保持原数据，禁止猜测明细和自动退款，请人工核查。
        </p>
      </article>
    </section>
    <section v-if="allowed('agent:logistics:read')">
      <h2>演示物流维护</h2>
      <div class="controls">
        <el-input v-model="orderId" aria-label="物流订单编号" placeholder="订单编号" /><el-button
          :disabled="busy || !orderId"
          @click="readLogistics"
          >读取物流</el-button
        >
      </div>
      <template v-if="logistics"
        ><p>
          订单 {{ logistics.order_id }} · 版本 {{ logistics.version }} ·
          仅演示，不改变订单发货或收货状态。
        </p>
        <div class="controls">
          <el-input v-model="carrier" aria-label="演示承运商" placeholder="演示承运商" /><el-input
            v-model="tracking"
            aria-label="演示物流单号"
            placeholder="演示单号"
          /><el-button
            v-if="allowed('agent:logistics:write')"
            :disabled="busy || !carrier.trim() || !tracking.trim()"
            @click="saveShipment"
            >保存物流资料</el-button
          >
        </div>
        <div v-for="node in logistics.nodes" :key="node.id" class="timeline">
          <span
            >{{ displayShanghaiTime(node.occurred_at) }}（北京时间） · {{ node.location }} ·
            {{ node.description }}</span
          ><el-button
            v-if="allowed('agent:logistics:write')"
            :disabled="busy"
            @click="chooseNode(node)"
            >更正节点</el-button
          >
        </div>
        <div v-if="logistics.shipment_id && allowed('agent:logistics:write')" class="node-form">
          <h3>{{ edit ? '更正' : '新增' }}演示节点（北京时间）</h3>
          <label for="agent-node-time">节点北京时间</label>
          <input
            id="agent-node-time"
            v-model="occurred"
            type="datetime-local"
            step="0.001"
            aria-label="节点北京时间"
          /><el-input
            v-model="location"
            aria-label="节点地点"
            placeholder="地点"
            :maxlength="200"
          /><el-input
            v-model="description"
            aria-label="节点描述"
            placeholder="演示状态描述"
            :maxlength="1000"
          /><el-input
            v-if="edit"
            v-model="editReason"
            aria-label="节点更正原因"
            placeholder="必填更正原因，旧值留审计"
            :maxlength="500"
          /><el-button
            :disabled="
              busy ||
              !occurred ||
              !location.trim() ||
              !description.trim() ||
              (!!edit && !editReason.trim())
            "
            @click="saveNode"
            >保存节点</el-button
          ><el-button v-if="edit" @click="resetNodeDraft">取消更正</el-button>
        </div></template
      >
    </section>
  </main>
</template>
<style scoped>
.operations {
  padding: 24px;
  max-width: 1400px;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 16px 0;
}
.controls .el-input,
.controls .el-select {
  width: 260px;
}
section,
article {
  padding: 20px;
  border: 1px solid #d9e0ea;
  border-radius: 8px;
  margin: 20px 0;
}
.error {
  color: #ad2540;
}
.proofs {
  display: flex;
  gap: 12px;
}
.proofs img {
  max-width: 200px;
  max-height: 140px;
}
.timeline {
  padding: 12px;
  border-bottom: 1px solid #e1e6ed;
  display: flex;
  gap: 14px;
}
.node-form {
  display: grid;
  gap: 12px;
  max-width: 650px;
}
</style>
