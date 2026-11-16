<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { operationsApi, requestKey } from '@/apis/agent'
import type { ReturnRule, ReturnRuleInput } from '@/apis/agent-contracts'
import { displayShanghaiTime, shanghaiInputToInstant } from '@/utils/agentTime'
defineProps<{ canPublish: boolean }>()
const rules = ref<ReturnRule[]>([]),
  active = ref(''),
  hours = ref(168),
  allowShipped = ref(true),
  note = ref(''),
  effective = ref(''),
  busy = ref(false),
  error = ref(''),
  notice = ref('')
let pending: { body: string; key: string; signature: string; effective: string } | undefined
async function load() {
  busy.value = true
  error.value = ''
  try {
    const page = await operationsApi.rules()
    rules.value = page.items
    active.value = page.active_rule_version
  } catch (e) {
    error.value = e instanceof Error ? e.message : '规则读取失败'
  } finally {
    busy.value = false
  }
}
function copy(rule: ReturnRule) {
  hours.value = rule.return_window_hours
  allowShipped.value = rule.allow_shipped
  note.value = ''
  effective.value = ''
  notice.value = '已复制参数；保存会创建新版本，原版本保留。'
}
async function publish() {
  if (busy.value || !rules.value.length) return
  error.value = ''
  notice.value = ''
  try {
    const latest = Math.max(...rules.value.map(r => r.version_no))
    const signature = JSON.stringify([
      latest,
      hours.value,
      allowShipped.value,
      effective.value,
      note.value.trim(),
    ])
    const instant = effective.value
      ? shanghaiInputToInstant(effective.value)
      : pending?.signature === signature
      ? pending.effective
      : new Date().toISOString()
    const body: ReturnRuleInput = {
      expected_latest_version: latest,
      return_window_hours: hours.value,
      allow_shipped: allowShipped.value,
      effective_at: instant,
      note: note.value.trim(),
    }
    if (!Number.isInteger(hours.value) || hours.value < 1 || hours.value > 720 || !body.note)
      throw new Error('请输入1至720小时的整数时限和发布说明')
    await ElMessageBox.confirm(
      `发布退货时限${hours.value}小时的新规则，${
        allowShipped.value ? '允许发货后申请' : '须收货后申请'
      }。${
        effective.value
          ? '计划于北京时间' + effective.value.replace('T', ' ') + '生效。'
          : '立即生效。'
      }旧申请保留原规则，旧确认卡需要重新准备。`,
      '发布售后规则',
      { type: 'warning', confirmButtonText: '确认发布', cancelButtonText: '取消' },
    )
    busy.value = true
    const encoded = JSON.stringify(body)
    if (pending?.body !== encoded)
      pending = { body: encoded, key: requestKey(), signature, effective: instant }
    const created = await operationsApi.publishRule(body, pending.key)
    pending = undefined
    note.value = ''
    effective.value = ''
    const page = await operationsApi.rules()
    rules.value = page.items
    active.value = page.active_rule_version
    notice.value = `已发布 ${created.rule_version}，生效时间 ${displayShanghaiTime(
      created.effective_at,
    )}`
  } catch (e) {
    if (e !== 'cancel' && e !== 'close')
      error.value = e instanceof Error ? e.message : '发布失败，请刷新核对后重试'
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <section class="return-rules">
    <h2>演示售后规则</h2>
    <p>
      当前生效：{{
        active || '正在读取'
      }}。金额按订单明细实付计算，退款保持演示。已发布版本不可修改，需调整时发布新版本。
    </p>
    <p>读取或发布规则会核对知识依据。绑定旧规则版本的资料停止用于新回答，原文和历史版本保留；新资料仍须由知识管理员审核发布。计划生效时按生效时间切换。</p>
    <el-button :loading="busy" @click="load">刷新规则</el-button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <el-table :data="rules" stripe>
      <el-table-column prop="rule_version" label="版本" width="230" />
      <el-table-column label="状态" width="110"
        ><template #default="s">{{
          s.row.rule_version === active
            ? '当前生效'
            : Date.parse(s.row.effective_at) > Date.now()
            ? '待生效'
            : '历史版本'
        }}</template></el-table-column
      >
      <el-table-column prop="return_window_hours" label="收货后小时数" width="140" />
      <el-table-column label="发货未收货"
        ><template #default="s">{{
          s.row.allow_shipped ? '允许申请' : '等待收货'
        }}</template></el-table-column
      >
      <el-table-column label="生效时间（北京时间）" width="230"
        ><template #default="s">{{
          displayShanghaiTime(s.row.effective_at)
        }}</template></el-table-column
      >
      <el-table-column prop="note" label="发布说明" />
      <el-table-column v-if="canPublish" label="操作" width="120"
        ><template #default="s"
          ><el-button :disabled="busy" @click="copy(s.row)">复制参数</el-button></template
        ></el-table-column
      >
    </el-table>
    <el-form v-if="canPublish" label-position="top" class="rule-form" @submit.prevent="publish">
      <el-form-item label="收货后的退货期限（1至720小时）"
        ><el-input-number v-model="hours" :min="1" :max="720" :precision="0" :disabled="busy"
      /></el-form-item>
      <el-form-item label="发货后、收货前是否可申请"
        ><el-switch
          v-model="allowShipped"
          :disabled="busy"
          active-text="允许"
          inactive-text="等待收货"
      /></el-form-item>
      <el-form-item label="生效时间（北京时间，留空为立即生效）"
        ><input
          v-model="effective"
          type="datetime-local"
          :disabled="busy"
          aria-label="售后规则生效时间"
      /></el-form-item>
      <el-form-item label="发布说明"
        ><el-input v-model="note" type="textarea" maxlength="500" show-word-limit :disabled="busy"
      /></el-form-item>
      <el-button
        type="primary"
        :loading="busy"
        :disabled="!rules.length || !note.trim()"
        @click="publish"
        >发布新版本</el-button
      >
    </el-form>
  </section>
</template>
<style scoped>
.rule-form {
  max-width: 600px;
  margin-top: 18px;
}
.error {
  color: #b42318;
}
.return-rules {
  margin-top: 20px;
}
</style>
