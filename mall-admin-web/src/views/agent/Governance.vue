<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { governanceApi, knowledgeApi, receptionApi, requestKey } from '@/apis/agent'
import type { Settings, Statistics, UnknownModelUsage, UsageSettlement, Job, ActionRecovery, BackupPage, BackupPolicyState } from '@/apis/agent-contracts'

const capabilities = ['DIALOG', 'VISION', 'EMBEDDING', 'RERANK', 'ASR', 'TTS'] as const
const permissions = ref<string[]>([])
const policy = ref<Settings | null>(null)
const statistics = ref<Statistics | null>(null)
const unknown = ref<UnknownModelUsage[]>([])
const nextUsage = ref<string | null>(null)
const jobs = ref<Job[]>([])
const nextJob = ref<string | null>(null)
const capability = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const selected = ref<UnknownModelUsage | null>(null)
const previewJob = ref<Job | null>(null)
const executionJob = ref<Job | null>(null)
const previewValue = ref('')
const retentionConfirmed = ref(false)
const unresolved = ref<ActionRecovery[]>([])
const nextAction = ref<string | null>(null)
const recovering = ref<ActionRecovery | null>(null)
const backups = ref<BackupPage | null>(null)
const backupPolicy = ref<BackupPolicyState | null>(null)
const backupNote = ref('')
const maintenanceConfirmed = ref(false)
const backupJob = ref<Job | null>(null)
const backupSelection = ref('')
const recoveryReason = ref('')
const usage = ref<UsageSettlement>({ expected_version: 1, statement_ref: '', reason: '' })
const numericFields = ['input_tokens', 'output_tokens', 'total_tokens', 'characters'] as const
const fieldLabels = { input_tokens: '输入令牌', output_tokens: '输出令牌', total_tokens: '总令牌', characters: '字符' }
let pending = { body: '', key: '' }
const can = (permission: string) => permissions.value.includes(permission)
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })

async function act(work: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await work() } catch (e) { error.value = e instanceof Error ? e.message : '操作失败，请刷新状态后重试' }
  finally { busy.value = false }
}

async function loadStatistics() {
  const end = new Date()
  statistics.value = await governanceApi.statistics(new Date(end.getTime() - 86400000).toISOString(), end.toISOString(), capability.value)
}

async function load() {
  permissions.value = (await receptionApi.me()).permissions
  await Promise.all([
    can('agent:settings:read') ? governanceApi.settings().then(value => { policy.value = value }) : Promise.resolve(),
    can('agent:statistics:read') ? loadStatistics() : Promise.resolve(),
    can('agent:usage:read') ? moreUsage(true) : Promise.resolve(),
    can('agent:job:read') ? moreJobs(true) : Promise.resolve(),
    can('agent:action:read') ? moreActions(true) : Promise.resolve(),
    can('agent:backup:verify') ? loadBackups() : Promise.resolve(),
  ])
}

async function loadBackups() {
  backups.value = await governanceApi.backups()
  backupPolicy.value = JSON.parse(JSON.stringify(backups.value.policy)) as BackupPolicyState
  if (backupJob.value) backupJob.value = await knowledgeApi.job(backupJob.value.id)
}

async function moreBackups() {
  if (!backups.value?.next_before) return
  const next = await governanceApi.backups(backups.value.next_before)
  backups.value = { ...next, items: [...backups.value.items, ...next.items] }
}

function jobResult(job: Job) {
  if (job.error_message || job.error_code) return job.error_message || job.error_code
  if (job.result?.verified) return `隔离恢复通过 · ${job.result.restore_seconds ?? '—'} 秒 · 演练资源已清理`
  if (job.result?.restore_job_id) return `归档认证通过，恢复任务 ${job.result.restore_job_id}`
  if (job.result?.backup_record_id) return `已登记备份 ${job.result.backup_record_id}`
  return '—'
}

async function saveBackupPolicy() {
  if (!backupPolicy.value || !maintenanceConfirmed.value) return
  const body = { value: backupPolicy.value.value, expected_version: backupPolicy.value.version }
  const encoded = JSON.stringify({ backupPolicy: body })
  if (pending.body !== encoded) pending = { body: encoded, key: requestKey() }
  backupPolicy.value = await governanceApi.backupPolicy(body, pending.key)
  pending = { body: '', key: '' }
  maintenanceConfirmed.value = false
  notice.value = '备份策略已保存。每日备份在配置的 UTC 时刻进入短维护窗口，每周在独立环境验证恢复。'
  await loadBackups()
}

async function createBackup() {
  if (!backupPolicy.value || !maintenanceConfirmed.value || !backupNote.value.trim()) return
  const encoded = JSON.stringify({ createBackup: backupPolicy.value.version, note: backupNote.value.trim() })
  if (pending.body !== encoded) pending = { body: encoded, key: requestKey() }
  backupJob.value = await governanceApi.createBackup(backupPolicy.value.version, backupNote.value.trim(), pending.key)
  pending = { body: '', key: '' }
  maintenanceConfirmed.value = false
  notice.value = '一致备份已排队。执行器会等待其他任务排空，短暂暂停商城服务后自动恢复；页面可能需要重新连接。'
}

async function verifyBackup() {
  if (!backupSelection.value || !backupNote.value.trim()) return
  const encoded = JSON.stringify({ verifyBackup: backupSelection.value, note: backupNote.value.trim() })
  if (pending.body !== encoded) pending = { body: encoded, key: requestKey() }
  backupJob.value = await governanceApi.verifyBackup(backupSelection.value, backupNote.value.trim(), pending.key)
  pending = { body: '', key: '' }
  notice.value = '归档认证任务已创建；认证后继续创建独立的隔离恢复任务。请在任务列表查看最终恢复结果。'
}

async function moreActions(reset = false) {
  const page = await governanceApi.actions(reset ? '' : nextAction.value || '')
  unresolved.value = reset ? page.items : [...unresolved.value, ...page.items]
  nextAction.value = page.next_before
}

async function resumeAction() {
  if (!recovering.value || !recoveryReason.value.trim()) return
  const item = recovering.value
  const body = { expected_version:item.action_version,expected_dispatch_version:item.dispatch_version,reason:recoveryReason.value.trim() }
  const value = JSON.stringify({ id:item.action_id,body })
  if (pending.body !== value) pending = { body:value,key:requestKey() }
  await governanceApi.resume(item.action_id,body,pending.key)
  pending = { body:'',key:'' }
  recovering.value = null
  notice.value = '原编号核对已恢复调度。结果由商城操作账本确定，不会重新执行下单、取消或退货。'
  await moreActions(true)
}

async function moreUsage(reset = false) {
  const page = await governanceApi.unknown(reset ? '' : nextUsage.value || '')
  unknown.value = reset ? page.items : [...unknown.value, ...page.items]
  nextUsage.value = page.next_before
}

async function moreJobs(reset = false) {
  const page = await governanceApi.jobs(reset ? '' : nextJob.value || '')
  jobs.value = reset ? page.items : [...jobs.value, ...page.items]
  nextJob.value = page.next_cursor
}

async function save() {
  if (!policy.value) return
  const body = { value: policy.value.value, expected_version: policy.value.version,
    ...(previewJob.value?.state === 'SUCCEEDED' && previewValue.value === JSON.stringify(policy.value.value) ? { preview_job_id: previewJob.value.id } : {}) }
  const encoded = JSON.stringify(body)
  if (pending.body !== encoded) pending = { body: encoded, key: requestKey() }
  policy.value = await governanceApi.save(body, pending.key)
  pending = { body: '', key: '' }
  notice.value = '设置已保存为新版本。运行中的模型调用保留原计价规则。'
}

async function previewRetention() {
  if (!policy.value) return
  const value = JSON.stringify(policy.value.value)
  const body = 'previewRetention:' + value
  if (pending.body !== body) pending = { body, key: requestKey() }
  previewJob.value = await governanceApi.previewRetention(policy.value.value, pending.key)
  previewValue.value = value
  retentionConfirmed.value = false
  pending = { body: '', key: '' }
}

async function refreshRetention() {
  if (previewJob.value) previewJob.value = await knowledgeApi.job(previewJob.value.id)
  if (executionJob.value) executionJob.value = await knowledgeApi.job(executionJob.value.id)
  await moreJobs(true)
}

async function executeRetention() {
  if (!previewJob.value || !policy.value || !retentionConfirmed.value) return
  const body = `executeRetention:${previewJob.value.id}:${policy.value.version}`
  if (pending.body !== body) pending = { body, key: requestKey() }
  executionJob.value = await governanceApi.executeRetention(previewJob.value.id, policy.value.version, pending.key)
  pending = { body: '', key: '' }
  retentionConfirmed.value = false
  notice.value = '留存执行任务已创建，执行时会重新核验设置版本和保护关系。'
  await moreJobs(true)
}

function choose(item: UnknownModelUsage) {
  selected.value = item
  usage.value = { expected_version: item.version, statement_ref: '', reason: '', input_tokens: null, output_tokens: null, total_tokens: null, characters: null, seconds: null }
  pending = { body: '', key: '' }
}

async function reconcile() {
  if (!selected.value) return
  const encoded = JSON.stringify({ id: selected.value.call_id, body: usage.value })
  if (pending.body !== encoded) pending = { body: encoded, key: requestKey() }
  await governanceApi.reconcile(selected.value.call_id, usage.value, pending.key)
  selected.value = null
  pending = { body: '', key: '' }
  notice.value = '供应商用量已核对并记录审计；原调用成功或失败状态保留。'
  await moreUsage(true)
  if (can('agent:statistics:read')) await loadStatistics()
}

function addQuota() {
  if (!policy.value) return
  ;(policy.value.value.quota_limits ??= []).push({ scope: 'GLOBAL', subject_id: null, capability: null, unit: 'REQUEST', daily_limit: '1000' })
}

function addPrice() {
  if (!policy.value) return
  ;(policy.value.value.model_prices ??= []).push({ profile_id: null, capability: 'DIALOG', unit: 'INPUT_TOKEN', currency: 'CNY', price: '0', per_units: 1000000 })
}

onMounted(() => { void act(load) })
</script>

<template>
  <main class="governance">
    <header><h1>治理与任务</h1><button :disabled="busy" @click="act(load)">刷新</button></header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="!permissions.length && !busy">当前账号没有可用的治理权限。</p>

    <section v-if="statistics && can('agent:statistics:read')">
      <h2>最近24小时用量与运行情况</h2>
      <label>模型能力 <select v-model="capability" :disabled="busy" @change="act(loadStatistics)"><option value="">全部</option><option v-for="c in capabilities" :key="c">{{ c }}</option></select></label>
      <p>统计时间：{{ time(statistics.from) }} 至 {{ time(statistics.to) }}（北京时间）。普通接口统计覆盖全部能力，模型筛选仅影响模型及额度数据。</p>
      <div class="metrics">
        <p>模型调用 <strong>{{ statistics.requests }}</strong> · 非成功 {{ statistics.errors }}</p>
        <p>输入令牌 {{ statistics.input_tokens ?? '部分用量未知' }} · 输出令牌 {{ statistics.output_tokens ?? '部分用量未知' }}</p>
        <p>生成中 {{ statistics.active_runs }} · 排队 {{ statistics.queued_runs }}</p>
        <p>普通接口 P95 {{ statistics.p95_business_ms == null ? '暂无样本' : statistics.p95_business_ms + ' ms' }} · 模型 P95 {{ statistics.p95_model_ms == null ? '暂无样本' : statistics.p95_model_ms + ' ms' }}</p>
        <p>普通接口 {{ statistics.business.requests }} 次 · 系统错误 {{ statistics.business.server_errors }} · 请求拒绝 {{ statistics.business.rejected_requests }}</p>
      </div>
      <p>估算费用按调用前保存的价格计算，不同币种分别展示；{{ statistics.unpriced_calls }} 次未能估价。费用不是供应商最终账单。</p>
      <p v-for="cost in statistics.costs" :key="cost.currency">{{ cost.currency }} {{ cost.estimated_cost }}（{{ cost.calls }} 次已估价调用）</p>
      <div class="table-scroll"><table><thead><tr><th>能力</th><th>请求 / 非成功</th><th>已知输入 / 输出令牌</th><th>缺令牌用量调用</th><th>P95</th></tr></thead><tbody>
        <tr v-for="row in statistics.capabilities" :key="row.capability"><td>{{ row.capability }}</td><td>{{ row.requests }} / {{ row.errors }}</td><td>{{ row.known_input_tokens }} / {{ row.known_output_tokens }}</td><td>{{ row.unknown_token_calls }}</td><td>{{ row.p95_ms }} ms</td></tr>
      </tbody></table></div>
      <h3>额度预留与实际结算</h3>
      <p>每次调用按计量单位只展示一次，主体和能力的多桶预留不重复累加。</p>
      <div class="table-scroll"><table><thead><tr><th>单位</th><th>状态</th><th>调用数</th><th>预留</th><th>实际结算</th></tr></thead><tbody>
        <tr v-for="row in statistics.quota_usage" :key="row.unit_kind + row.state"><td>{{ row.unit_kind }}</td><td>{{ row.state }}</td><td>{{ row.reservations }}</td><td>{{ row.reserved_units }}</td><td>{{ row.settled_units ?? '未知' }}</td></tr>
      </tbody></table></div>
      <h3>运行告警</h3>
      <template v-if="statistics.runtime">
        <p>依赖与资源采样：{{ time(statistics.runtime.checked_at) }} · 本轮 {{ statistics.runtime.duration_ms }} ms。超过90秒没有新样本将告警，旧样本仅供参考。</p>
        <div class="table-scroll"><table><thead><tr><th>依赖</th><th>状态</th><th>探针耗时</th></tr></thead><tbody>
          <tr v-for="(sample, name) in statistics.runtime.dependencies" :key="name"><td>{{ name }}</td><td>{{ sample.state }} {{ sample.error_code || '' }}</td><td>{{ sample.latency_ms }} ms</td></tr>
        </tbody></table></div>
        <p v-if="statistics.runtime.cloud">云端内存可用 {{ Math.round(statistics.runtime.cloud.memory_available_bytes / 1024 ** 2) }} MiB / {{ Math.round(statistics.runtime.cloud.memory_total_bytes / 1024 ** 2) }} MiB；数据盘空闲 {{ Math.round(statistics.runtime.cloud.disk_free_bytes / 1024 ** 3 * 10) / 10 }} GiB；容器运行 {{ statistics.runtime.cloud.containers_running }} / {{ statistics.runtime.cloud.containers_expected }}。</p>
        <p v-if="statistics.runtime.postgres">PG连接 {{ statistics.runtime.postgres.connections }} / {{ statistics.runtime.postgres.max_connections }}；锁等待 {{ statistics.runtime.postgres.lock_waiters }}；最长事务 {{ statistics.runtime.postgres.longest_transaction_seconds }} 秒；连接获取P95 {{ statistics.runtime.postgres.pool_wait_p95_ms }} ms；生成占用 {{ statistics.runtime.postgres.active_slots }} / 3，排队 {{ statistics.runtime.postgres.queued_runs }}。</p>
        <p v-if="statistics.runtime.postgres">最近24小时仍保留首段事件的 {{ statistics.runtime.postgres.first_text_samples }} 个任务：首段正文P95 {{ statistics.runtime.postgres.first_text_p95_ms ?? '未知' }} ms，排队P95 {{ statistics.runtime.postgres.queue_p95_ms ?? '未知' }} ms，总耗时P95 {{ statistics.runtime.postgres.total_p95_ms ?? '未知' }} ms。删除或到期事件不包含在该样本中，不代表全部任务。</p>
        <p v-if="statistics.runtime.java_outbox">Java业务通知已到期 {{ statistics.runtime.java_outbox.pending_due }} 条；超时60秒 {{ statistics.runtime.java_outbox.overdue_60_seconds }} 条；最久 {{ statistics.runtime.java_outbox.oldest_due_seconds }} 秒；重试至少3次 {{ statistics.runtime.java_outbox.retrying }} 条。</p>
      </template>
      <p v-if="!statistics.alerts.length">当前未发现已接入指标的告警。</p>
      <ul v-else><li v-for="alert in statistics.alerts" :key="alert.code + alert.resource">{{ alert.severity }} · {{ alert.code }} · {{ alert.resource }} · {{ alert.count }}</li></ul>
    </section>

    <section v-if="backups && backupPolicy && can('agent:backup:verify')">
      <h2>备份与隔离恢复</h2>
      <p>备份覆盖商城账本、客服记录、私有附件、知识和删除记录。恢复在独立环境执行，校验后清理演练资源，不开放订单执行或模型调用。</p>
      <p>最近备份 {{ backups.executor?.last_backup_at ? time(backups.executor.last_backup_at) : '尚无有效记录' }} · 最近恢复 {{ backups.executor?.last_restore_at ? time(backups.executor.last_restore_at) : '尚未验证' }} · 删除日志 {{ backups.executor?.last_journal_at ? time(backups.executor.last_journal_at) : '尚无记录' }}</p>
      <ul v-if="backups.alerts.length"><li v-for="alert in backups.alerts" :key="alert.code">{{ alert.severity }} · {{ alert.code }}</li></ul>
      <button :disabled="busy" @click="act(loadBackups)">刷新备份与恢复状态</button>
      <fieldset :disabled="busy || !can('agent:settings:write')">
        <h3>周期策略 · 版本 {{ backupPolicy.version }}</h3>
        <div class="fields">
          <label>自动备份<select v-model="backupPolicy.value.enabled"><option :value="true">启用</option><option :value="false">停用</option></select></label>
          <label>每日备份 UTC 小时<input v-model.number="backupPolicy.value.daily_hour_utc" type="number" min="0" max="23" /></label>
          <label>每周恢复日（UTC）<select v-model.number="backupPolicy.value.restore_weekday_utc"><option v-for="(day, index) in ['周一','周二','周三','周四','周五','周六','周日']" :key="index" :value="index">{{ day }}</option></select></label>
          <label>每日副本保留天数<input v-model.number="backupPolicy.value.retention_days" type="number" min="7" max="90" /></label>
          <label>周副本保留周数<input v-model.number="backupPolicy.value.weekly_retention_weeks" type="number" min="1" max="12" /></label>
          <label>删除日志间隔分钟<input v-model.number="backupPolicy.value.journal_interval_minutes" type="number" min="5" max="60" /></label>
          <label>策略说明<input v-model="backupPolicy.value.note" maxlength="500" /></label>
        </div>
      </fieldset>
      <p>UTC 19点对应北京时间次日3点；UTC周六19点对应北京时间周日3点。</p>
      <label><input v-model="maintenanceConfirmed" type="checkbox" />我已确认每日或本次一致备份需要短维护窗口，暂停期间商城服务暂不可用。</label>
      <button :disabled="busy || !can('agent:settings:write') || !maintenanceConfirmed" @click="act(saveBackupPolicy)">保存周期策略</button>
      <label>本次操作说明<input v-model="backupNote" maxlength="500" /></label>
      <button :disabled="busy || !maintenanceConfirmed || !backupNote.trim()" @click="act(createBackup)">创建一致备份</button>
      <div class="table-scroll"><table><thead><tr><th>登记编号</th><th>备份时间</th><th>状态</th><th>最近恢复</th><th>耗时</th></tr></thead><tbody>
        <tr v-for="item in backups.items" :key="item.id"><td><label><input v-model="backupSelection" type="radio" :value="item.id" :disabled="item.state !== 'AVAILABLE'" />{{ item.id }}</label></td><td>{{ time(item.created_at) }}</td><td>{{ item.state }}</td><td>{{ item.verified_at ? time(item.verified_at) : '未验证' }}</td><td>{{ item.restore_seconds ?? '—' }} 秒</td></tr>
      </tbody></table></div>
      <button v-if="backups.has_more" :disabled="busy" @click="act(moreBackups)">更多登记备份</button>
      <button :disabled="busy || !backupSelection || !backupNote.trim()" @click="act(verifyBackup)">认证所选备份并验证隔离恢复</button>
      <p v-if="backupJob">本次任务 {{ backupJob.id }} · {{ backupJob.state }} · {{ backupJob.summary }} · {{ backupJob.result?.verified ? '隔离恢复通过' : '最终恢复结果请查看任务列表' }}</p>
    </section>

    <section v-if="policy && can('agent:settings:read')">
      <h2>运行设置 · 版本 {{ policy.version }}</h2>
      <fieldset :disabled="busy || !can('agent:settings:write')">
        <div class="fields">
          <label>文字保留天数<input v-model.number="policy.value.message_retention_days" type="number" min="1" max="3650" /></label>
          <label>图片保留天数<input v-model.number="policy.value.image_retention_days" type="number" min="1" max="3650" /></label>
          <label>原始录音保留天数<input v-model.number="policy.value.audio_retention_days" type="number" min="1" max="365" /></label>
          <label>播报缓存保留天数<input v-model.number="policy.value.tts_retention_days" type="number" min="1" max="365" /></label>
          <label>事件保留天数<input v-model.number="policy.value.event_retention_days" type="number" min="1" max="90" /></label>
          <label>操作确认有效秒数<input v-model.number="policy.value.action_ttl_seconds" type="number" min="60" max="900" /></label>
          <label>SSE心跳秒数<input v-model.number="policy.value.heartbeat_seconds" type="number" min="10" max="60" /></label>
          <label>客服在线有效秒数<input v-model.number="policy.value.presence_timeout_seconds" type="number" min="30" max="300" /></label>
          <label>消息输入字数上限<input v-model.number="policy.value.max_input_characters" type="number" min="1" max="10000" /></label>
          <label>知识文件页数上限<input v-model.number="policy.value.max_document_pages" type="number" min="1" max="2000" /></label>
        </div>
        <p>缩短留存期限需要先完成当前方案的影响试算，再保存设置并执行。确认卡有效期还受商城报价有效期限制。</p>
        <h3>每日额度（UTC自然日）</h3>
        <div v-for="(q, index) in policy.value.quota_limits" :key="index" class="rule">
          <label>范围<select v-model="q.scope" @change="q.subject_id = null"><option v-for="s in ['GLOBAL', 'CAPABILITY', 'PROFILE', 'MEMBER', 'STAFF']" :key="s">{{ s }}</option></select></label>
          <label v-if="['PROFILE', 'MEMBER', 'STAFF'].includes(q.scope)">主体编号<input v-model="q.subject_id" placeholder="配置UUID或会员/员工编号" /></label>
          <label>能力<select v-model="q.capability"><option :value="null">全部</option><option v-for="c in capabilities" :key="c">{{ c }}</option></select></label>
          <label>单位<select v-model="q.unit"><option v-for="u in ['REQUEST', 'TOKEN', 'CHARACTER', 'SECOND']" :key="u">{{ u }}</option></select></label>
          <label>每日上限<input v-model="q.daily_limit" inputmode="decimal" /></label>
          <button @click="policy.value.quota_limits!.splice(index, 1)">移除额度规则</button>
        </div>
        <button :disabled="(policy.value.quota_limits?.length || 0) >= 100" @click="addQuota">添加额度规则</button>
        <h3>供应商计价</h3>
        <p>按实际合同填写。未配置价格显示未知；输入/输出令牌可分别计价，总令牌不能与二者重复计价。</p>
        <div v-for="(p, index) in policy.value.model_prices" :key="index" class="rule">
          <label>能力<select v-model="p.capability"><option v-for="c in capabilities" :key="c">{{ c }}</option></select></label>
          <label>指定配置UUID（可空）<input :value="p.profile_id || ''" @input="p.profile_id = ($event.target as HTMLInputElement).value.trim() || null" /></label>
          <label>单位<select v-model="p.unit"><option v-for="u in ['REQUEST', 'INPUT_TOKEN', 'OUTPUT_TOKEN', 'TOKEN', 'CHARACTER', 'SECOND']" :key="u">{{ u }}</option></select></label>
          <label>每多少单位<input v-model.number="p.per_units" type="number" min="1" max="1000000000" /></label>
          <label>价格<input v-model="p.price" inputmode="decimal" /></label>
          <label>币种<select v-model="p.currency"><option>CNY</option><option>USD</option></select></label>
          <button @click="policy.value.model_prices!.splice(index, 1)">移除价格规则</button>
        </div>
        <button :disabled="(policy.value.model_prices?.length || 0) >= 100" @click="addPrice">添加价格规则</button>
        <button @click="act(save)">保存设置版本</button>
      </fieldset>
      <div v-if="can('agent:retention:preview')" class="retention">
        <h3>留存影响试算</h3>
        <button :disabled="busy || !can('agent:job:read')" @click="act(previewRetention)">试算当前设置的影响</button>
        <button v-if="previewJob" :disabled="busy" @click="act(refreshRetention)">刷新试算与执行状态</button>
        <p v-if="previewJob">试算 {{ previewJob.id }} · {{ previewJob.state }} · {{ previewJob.error_message || previewJob.error_code || '' }}</p>
        <p v-if="previewJob && previewValue !== JSON.stringify(policy.value)">设置已编辑，需重新试算。</p>
        <template v-if="previewJob?.result">
          <p>固定截止时间 {{ previewJob.result.cutoff ? time(previewJob.result.cutoff) : '—' }}；试算有效期24小时。</p>
          <p>预计到期消息 {{ previewJob.result.messages }} 条、涉及会话 {{ previewJob.result.conversations }} 个、备注 {{ previewJob.result.notes }} 条；候选附件 {{ previewJob.result.attachments }} 个，其中当前有引用或保护 {{ previewJob.result.protected_attachments }} 个。</p>
          <p>执行时再次核验保护，新增保护可能减少实际删除数量。知识活动来源、业务账本及售后凭证按各自引用保留。</p>
        </template>
        <label v-if="can('agent:retention:execute')"><input v-model="retentionConfirmed" type="checkbox" :disabled="busy || previewJob?.state !== 'SUCCEEDED' || previewValue !== JSON.stringify(policy.value)" />我已核对影响范围，并已保存与试算一致的设置</label>
        <button v-if="can('agent:retention:execute')" :disabled="busy || !retentionConfirmed || previewJob?.state !== 'SUCCEEDED' || previewValue !== JSON.stringify(policy.value)" @click="act(executeRetention)">执行本次到期清理</button>
        <p v-if="executionJob">执行 {{ executionJob.id }} · {{ executionJob.state }} · {{ executionJob.progress }}% · {{ executionJob.error_message || executionJob.error_code || '' }}</p>
        <p v-if="executionJob?.result">实际清理消息 {{ executionJob.result.messages }} 条、备注 {{ executionJob.result.notes }} 条，已安排附件回收 {{ executionJob.result.attachments_scheduled }} 个。附件和检查点的后续任务可在下方查询。</p>
      </div>
    </section>

    <section v-if="can('agent:usage:read')">
      <h2>未知用量核对</h2>
      <p>缺失用量继续占用预留额度；核对只更新计量，不重新调用模型，也不改变原业务结果。</p>
      <p v-if="!unknown.length">暂无待核对调用。</p>
      <article v-for="item in unknown" :key="item.call_id" class="usage">
        <p>{{ item.capability || '尚未落账' }} · {{ time(item.created_at) }} · {{ item.status || item.usage_state }}</p>
        <p>调用编号 {{ item.call_id }} · 供应商追踪 {{ item.provider_request_id || '未报告' }}</p>
        <p v-if="item.run_id">对话任务 {{ item.run_id }}</p>
        <p v-else-if="item.job_id">后台任务 {{ item.job_id }}</p>
        <p v-else-if="item.media_request_id">语音请求 {{ item.media_request_id }}</p>
        <p v-else>调用未关联具体任务，按原记录核对。</p>
        <p v-if="item.actor_kind">责任主体：{{ item.actor_kind === 'MEMBER' ? '买家' : item.actor_kind === 'STAFF' ? '员工' : '系统' }} {{ item.actor_id || '' }}</p>
        <p v-if="item.error_code === 'ADAPTER_IN_FLIGHT'">调用结果尚未记录；开始后十分钟内不能人工结算。</p>
        <p>预留：<span v-for="r in item.reservations" :key="r.unit">{{ r.unit }} {{ r.reserved_units }}；</span></p>
        <button v-if="can('agent:usage:write')" :disabled="busy || ((!item.status || item.error_code === 'ADAPTER_IN_FLIGHT') && Date.now() - Date.parse(item.created_at) < 600000)" @click="choose(item)">填写供应商实际用量</button>
      </article>
      <button v-if="nextUsage" :disabled="busy" @click="act(() => moreUsage())">更多待核对调用</button>
      <form v-if="selected" class="settlement" @submit.prevent="act(reconcile)">
        <h3>核对 {{ selected.call_id }}</h3>
        <label v-for="field in numericFields" :key="field">{{ fieldLabels[field] }}<input :value="usage[field] ?? ''" type="number" min="0" @input="usage[field] = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)" /></label>
        <label>时长（秒）<input :value="usage.seconds || ''" inputmode="decimal" @input="usage.seconds = ($event.target as HTMLInputElement).value.trim() || null" /></label>
        <label>供应商对账凭据编号<input v-model="usage.statement_ref" required maxlength="160" /></label>
        <label>核对说明<textarea v-model="usage.reason" required maxlength="500" /></label>
        <button :disabled="busy" type="submit">确认实际用量并记录审计</button><button type="button" :disabled="busy" @click="selected = null">取消</button>
      </form>
    </section>

    <section v-if="can('agent:action:read')">
      <h2>未决业务操作</h2>
      <p>操作结果不确定时持续核对原编号。人工恢复仅续签核对凭据，不提供重新执行业务或直接修改结果的权限。</p>
      <p v-if="!unresolved.length">暂无未决业务操作。</p>
      <article v-for="item in unresolved" :key="item.action_id" class="usage">
        <p>{{ item.action_type }} · {{ item.action_state }} · {{ item.dispatch_state }} · 更新于 {{ time(item.updated_at) }}</p>
        <p>原操作编号 {{ item.operation_no }} · {{ item.last_error_code || '正在核对或等待调度' }}</p>
        <button v-if="can('agent:action:reconcile')" :disabled="busy" @click="recovering = item; recoveryReason = ''; pending = {body:'',key:''}">恢复原编号核对</button>
      </article>
      <button v-if="nextAction" :disabled="busy" @click="act(() => moreActions())">更多未决操作</button>
      <form v-if="recovering" @submit.prevent="act(resumeAction)">
        <label>核对恢复依据<textarea v-model="recoveryReason" maxlength="500" required /></label>
        <button :disabled="busy || !recoveryReason.trim()" type="submit">仅恢复原编号核对</button>
        <button :disabled="busy" type="button" @click="recovering = null">取消</button>
      </form>
    </section>

    <section v-if="can('agent:job:read')">
      <h2>后台任务</h2><p v-if="!jobs.length">暂无当前权限范围内的任务。</p>
      <div class="table-scroll"><table><thead><tr><th>任务</th><th>状态 / 进度</th><th>更新时间</th><th>结果</th><th>操作</th></tr></thead><tbody>
        <tr v-for="job in jobs" :key="job.id"><td>{{ job.summary }}<br />{{ job.id }}</td><td>{{ job.state }} / {{ job.progress }}%</td><td>{{ time(job.updated_at) }}</td><td>{{ jobResult(job) }}</td><td><button v-if="can('agent:job:retry') && job.state === 'FAILED'" :disabled="busy" @click="act(async () => { await knowledgeApi.retry(job, requestKey()); await moreJobs(true) })">按当前权限重试</button></td></tr>
      </tbody></table></div>
      <button v-if="nextJob" :disabled="busy" @click="act(() => moreJobs())">更多任务</button>
    </section>
  </main>
</template>

<style scoped>
.governance { max-width: 1440px; margin: auto; padding: 24px; color: #24334b; }
header { display: flex; justify-content: space-between; align-items: center; }
section { margin-top: 24px; padding: 24px; border: 1px solid #dbe4ed; border-radius: 12px; background: white; }
h1 { font-size: 26px; } h2 { font-size: 20px; margin-top: 0; } h3 { font-size: 17px; }
p, li { line-height: 1.8; overflow-wrap: anywhere; } .error { color: #a32020; }
.metrics, .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
fieldset { border: 0; padding: 0; margin: 0; } label { display: inline-flex; flex-direction: column; gap: 7px; margin: 6px 8px 6px 0; }
input, select, textarea, button { font: inherit; border: 1px solid #aebed0; border-radius: 5px; padding: 10px; min-height: 44px; box-sizing: border-box; }
button { background: #f3f7fb; color: #183b62; cursor: pointer; margin: 5px; } button:disabled { cursor: default; opacity: .6; }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible { outline: 3px solid #4d8fd0; outline-offset: 2px; }
.rule, .usage, .settlement { padding: 14px; margin: 12px 0; border: 1px solid #dbe4ed; border-radius: 8px; }
.rule { display: flex; flex-wrap: wrap; align-items: end; } .rule input { max-width: 245px; } .table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 12px; border-bottom: 1px solid #dbe4ed; white-space: nowrap; }
@media (max-width: 700px) { .governance { padding: 12px; } section { padding: 14px; } .rule label, .rule input { width: 100%; max-width: none; } }
</style>
