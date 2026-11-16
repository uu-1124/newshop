<template>
  <main class="knowledge-page" v-loading="loading">
    <header>
      <div>
        <h1>知识管理</h1>
        <p>上传资料、校对原文、构建索引，再由有权限的员工确认发布。</p>
      </div>
      <el-button @click="load">刷新</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-alert
      title="请仅发布已经业务确认的资料。内部资料与限定商品/分类资料当前不会用于通用咨询。"
      type="info"
      :closable="false"
      show-icon
    />
    <div class="toolbar">
      <el-input
        v-model="query"
        placeholder="搜索资料标题"
        clearable
        @keyup.enter="load"
      /><el-button @click="load">搜索</el-button
      ><el-button v-if="can('agent:kb:edit')" type="primary" @click="newDocument"
        >新增资料</el-button
      >
    </div>
    <el-table :data="documents" highlight-current-row @row-click="select">
      <el-table-column prop="title" label="资料标题" min-width="220" />
      <el-table-column prop="category" label="分类" />
      <el-table-column label="可见范围"
        ><template #default="{ row }">{{
          row.visibility === 'MEMBER' ? '会员可见' : '员工内部'
        }}</template></el-table-column
      >
      <el-table-column label="发布状态"
        ><template #default="{ row }"
          ><el-tag :type="row.enabled && row.active_version_id ? 'success' : 'info'">{{
            !row.enabled ? '已停用' : row.active_version_id ? '已发布' : '未发布'
          }}</el-tag></template
        ></el-table-column
      >
    </el-table>
    <el-button v-if="nextCursor" class="more" @click="moreDocuments">加载更多资料</el-button>
    <p v-if="!documents.length && !loading" class="empty">
      暂无资料。新增正式业务资料后，才能回答有依据的政策问题。
    </p>

    <section v-if="selected" class="detail">
      <div class="section-head">
        <div>
          <h2>{{ selected.title }}</h2>
          <p>{{ selected.category }} · {{ scopeLabel(selected) }}</p>
        </div>
        <div>
          <el-button v-if="can('agent:kb:edit')" @click="editDocument">编辑资料</el-button>
          <el-button v-if="can('agent:kb:edit')" type="primary" @click="openVersion"
            >新增版本</el-button
          >
          <el-button
            v-if="can('agent:kb:publish')"
            :type="selected.enabled ? 'warning' : 'success'"
            @click="toggleEnabled"
            >{{ selected.enabled ? '停用资料' : '启用资料' }}</el-button
          >
        </div>
      </div>
      <el-table :data="versions" @row-click="showVersion" highlight-current-row>
        <el-table-column label="版本" width="90"
          ><template #default="{ row }">v{{ row.version_no }}</template></el-table-column
        >
        <el-table-column label="状态"
          ><template #default="{ row }"
            >{{ stateLabel(row.state)
            }}{{ selected.active_version_id === row.id ? ' · 当前发布' : '' }}</template
          ></el-table-column
        >
        <el-table-column prop="chunk_count" label="切片数" width="90" />
        <el-table-column prop="embedding_model" label="向量模型" />
        <el-table-column label="创建时间"
          ><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column
        >
      </el-table>
      <el-button v-if="versionCursor" class="more" @click="moreVersions">加载更多版本</el-button>
      <div v-if="current" class="version-panel">
        <div class="section-head">
          <h3>版本 v{{ current.version_no }} · {{ stateLabel(current.state) }}</h3>
          <div>
            <el-button
              v-if="canEdit && ['DRAFT', 'FAILED'].includes(current.state)"
              :disabled="working"
              @click="startTask('parse')"
              >解析原文</el-button
            >
            <el-button
              v-if="canEdit && current.state === 'PREVIEW_READY'"
              :disabled="working"
              @click="savePreview"
              >保存校对文本</el-button
            >
            <el-button
              v-if="canEdit && ['PREVIEW_READY', 'READY'].includes(current.state)"
              :disabled="working || dirty"
              @click="startTask('build')"
              >构建索引</el-button
            >
            <el-button
              v-if="can('agent:kb:publish') && (current.state === 'RETIRED' || (current.state === 'READY' && !current.published_at))"
              type="success"
              :disabled="working || current.quality_passed !== true"
              @click="publish"
              >{{ current.state === 'RETIRED' ? '回滚至此版本' : '确认发布' }}</el-button
            >
          </div>
        </div>
        <el-alert
          v-if="current.quality_passed"
          title="索引写入与回读校验通过。该检查不代表政策准确性已经验收，请由业务人员核对后发布。"
          type="success"
          :closable="false"
        />
        <el-alert
          v-if="dirty"
          title="校对文本尚未保存，请先保存再构建索引。"
          type="warning"
          :closable="false"
        />
        <p v-for="warning in warnings" :key="warning" class="warning">{{ warning }}</p>
        <template v-if="hasPreview && previewPages.length">
          <p>按 PDF 原始页校对；页数与页序保持不变，引用页码将沿用对应原件页。</p>
          <div v-for="page in previewPages" :key="page.page">
            <strong>第 {{ page.page }} 页</strong>
            <el-input v-model="page.text" type="textarea" :rows="8" :maxlength="500000" :readonly="!canEdit || current.state !== 'PREVIEW_READY'" />
          </div>
        </template>
        <el-input
          v-else-if="hasPreview"
          v-model="previewText"
          type="textarea"
          :rows="15"
          :maxlength="500000"
          :readonly="!canEdit || current.state !== 'PREVIEW_READY'"
          placeholder="解析后在这里校对文本"
        />
        <p v-else class="empty">解析完成后可预览和校对正文。</p>
      </div>
      <div v-if="job" class="job-panel" role="status">
        <strong>后台任务 · {{ job.state }}</strong
        ><span>第 {{ job.attempts }} 次执行</span
        ><span v-if="job.error_code">{{ job.error_code }} · {{ job.error_message }}</span
        ><el-button
          v-if="job.state === 'FAILED' && can('agent:job:retry')"
          :disabled="working"
          @click="retryJob"
          >重试任务</el-button
        ><el-button @click="refreshJob">刷新任务</el-button>
      </div>
    </section>

    <el-dialog
      v-model="documentDialog"
      :title="editing ? '编辑资料' : '新增资料'"
      width="min(620px, 95vw)"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="资料标题"
          ><el-input v-model="form.title" maxlength="200"
        /></el-form-item>
        <el-form-item label="分类"
          ><el-input v-model="form.category" maxlength="80"
        /></el-form-item>
        <el-form-item label="可见范围"
          ><el-radio-group v-model="form.visibility"
            ><el-radio value="MEMBER">会员可见</el-radio
            ><el-radio value="STAFF">员工内部</el-radio></el-radio-group
          ></el-form-item
        >
        <el-form-item label="商品 ID（选填，用逗号分隔）"
          ><el-input v-model="form.products"
        /></el-form-item>
        <el-form-item label="分类 ID（选填，用逗号分隔）"
          ><el-input v-model="form.categories"
        /></el-form-item>
      </el-form>
      <el-alert
        v-if="editing"
        title="修改可见范围或适用范围会撤回当前发布；需要新建版本、构建并重新发布。"
        type="warning"
        :closable="false"
      />
      <template #footer
        ><el-button @click="documentDialog = false">取消</el-button
        ><el-button type="primary" :loading="working" @click="saveDocument"
          >保存资料</el-button
        ></template
      >
    </el-dialog>
    <el-dialog
      v-model="versionDialog"
      title="新增资料版本"
      width="min(720px, 95vw)"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="变更说明"
          ><el-input v-model="changeNote" maxlength="500"
        /></el-form-item>
        <el-radio-group v-model="sourceKind"
          ><el-radio value="text">直接录入</el-radio
          ><el-radio v-if="can('agent:attachment:upload')" value="file"
            >上传文件</el-radio
          ></el-radio-group
        >
        <el-form-item v-if="sourceKind === 'text'" label="原始资料正文"
          ><el-input v-model="sourceText" type="textarea" :rows="12" :maxlength="500000"
        /></el-form-item>
        <el-form-item v-else label="PDF / DOCX / UTF-8 TXT、MD，最多20MB"
          ><input type="file" accept=".pdf,.docx,.txt,.md" @change="chooseFile" />
          <p>扫描版 PDF 请先人工转成文字。解析后仍需核对表格、顺序和遗漏内容。</p></el-form-item
        >
      </el-form>
      <template #footer
        ><el-button @click="versionDialog = false">取消</el-button
        ><el-button type="primary" :loading="working" @click="saveVersion"
          >创建版本</el-button
        ></template
      >
    </el-dialog>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { knowledgeApi as api, requestKey } from '@/apis/agent'
import type { Job, KBDocument, KBVersion } from '@/apis/agent-contracts'

const permissions = ref<string[]>([]),
  documents = ref<KBDocument[]>([]),
  versions = ref<KBVersion[]>([])
const selected = ref<KBDocument>(),
  current = ref<KBVersion>(),
  job = ref<Job>()
const query = ref(''),
  nextCursor = ref<string | null>(null),
  versionCursor = ref<string | null>(null)
const loading = ref(false),
  working = ref(false),
  error = ref(''),
  documentDialog = ref(false),
  versionDialog = ref(false),
  editing = ref(false)
const form = reactive({
  title: '',
  category: '',
  visibility: 'MEMBER' as 'MEMBER' | 'STAFF',
  products: '',
  categories: '',
})
const sourceKind = ref('text'),
  sourceText = ref(''),
  changeNote = ref(''),
  file = ref<File>(),
  previewText = ref(''),
  savedText = ref(''),
  warnings = ref<string[]>([]),
  hasPreview = ref(false)
const can = (permission: string) => permissions.value.includes(permission)
const canEdit = computed(() => can('agent:kb:edit') && !current.value?.published_at)
const previewPages = ref<{ page: number; text: string }[]>([])
const effectiveText = computed(() => previewPages.value.length ? previewPages.value.map(page => page.text).join('\n\n') : previewText.value)
const dirty = computed(() => effectiveText.value !== savedText.value)
const keys = new Map<string, string>()
let timer: ReturnType<typeof setTimeout> | undefined,
  disposed = false,
  selection = 0
const key = (name: string, body: unknown) => {
  const signature = name + JSON.stringify(body)
  if (!keys.has(signature)) keys.set(signature, requestKey())
  return keys.get(signature)!
}
const date = (value: string) => new Date(value).toLocaleString('zh-CN')
const stateLabel = (state: string) =>
  ({
    DRAFT: '草稿',
    PARSING: '解析中',
    PREVIEW_READY: '待校对/构建',
    INDEXING: '构建中',
    READY: '索引就绪',
    FAILED: '任务失败',
    RETIRED: '已替换',
  })[state] || state
const scopeLabel = (doc: KBDocument) =>
  doc.scope.product_ids?.length || doc.scope.category_ids?.length
    ? '限定范围 · 仅匹配已核验商品咨询'
    : '全局资料'
function fail(reason: unknown) {
  error.value = reason instanceof Error ? reason.message : '操作失败，请刷新后重试'
}
async function action(fn: () => Promise<void>) {
  if (working.value) return
  working.value = true
  error.value = ''
  try {
    await fn()
    keys.clear()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') fail(e)
  } finally {
    working.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    permissions.value = (await api.me()).permissions
    const page = await api.list(query.value)
    documents.value = page.items
    nextCursor.value = page.next_cursor
    if (selected.value) await refresh()
  } catch (e) {
    fail(e)
  } finally {
    loading.value = false
  }
}
async function moreDocuments() {
  try {
    const p = await api.list(query.value, nextCursor.value || '')
    documents.value.push(...p.items)
    nextCursor.value = p.next_cursor
  } catch (e) {
    fail(e)
  }
}
async function select(doc: KBDocument) {
  const ticket = ++selection
  clearTimeout(timer)
  selected.value = doc
  current.value = undefined
  job.value = undefined
  versions.value = []
  try {
    const p = await api.versions(doc.id)
    if (ticket !== selection) return
    versions.value = p.items
    versionCursor.value = p.next_cursor
    if (p.items[0]) await showVersion(p.items[0])
  } catch (e) {
    fail(e)
  }
}
async function moreVersions() {
  if (!selected.value) return
  try {
    const p = await api.versions(selected.value.id, versionCursor.value || '')
    versions.value.push(...p.items)
    versionCursor.value = p.next_cursor
  } catch (e) {
    fail(e)
  }
}
async function refresh() {
  if (!selected.value) return
  const id = selected.value.id
  selected.value = await api.get(id)
  documents.value = documents.value.map(d => (d.id === id ? selected.value! : d))
  const p = await api.versions(id)
  versions.value = p.items
  versionCursor.value = p.next_cursor
  if (current.value) await showVersion(await api.version(current.value.id))
}
async function showVersion(v: KBVersion) {
  current.value = v
  hasPreview.value = false
  previewText.value = savedText.value = ''
  previewPages.value = []
  warnings.value = []
  if (['PREVIEW_READY', 'INDEXING', 'READY', 'RETIRED'].includes(v.state)) {
    try {
      const p = await api.preview(v.id)
      if (current.value?.id !== v.id || disposed) return
      previewText.value = savedText.value = p.parsed_text
      previewPages.value = p.pages || []
      warnings.value = p.warnings
      hasPreview.value = true
    } catch (e) {
      fail(e)
    }
  }
}
function newDocument() {
  editing.value = false
  Object.assign(form, {
    title: '',
    category: '',
    visibility: 'MEMBER',
    products: '',
    categories: '',
  })
  documentDialog.value = true
}
function editDocument() {
  if (!selected.value) return
  editing.value = true
  Object.assign(form, {
    title: selected.value.title,
    category: selected.value.category,
    visibility: selected.value.visibility,
    products: selected.value.scope.product_ids?.join(',') || '',
    categories: selected.value.scope.category_ids?.join(',') || '',
  })
  documentDialog.value = true
}
function ids(value: string) {
  const list = [...new Set(value.split(/[,，\s]+/).filter(Boolean))]
  if (
    list.length > 100 ||
    list.some(id => !/^[1-9]\d{0,18}$/.test(id) || BigInt(id) > 9223372036854775807n)
  )
    throw new Error('范围 ID 须为有效的正整数，最多100个')
  return list
}
async function saveDocument() {
  await action(async () => {
    if (!form.title.trim() || !form.category.trim()) throw new Error('请填写资料标题和分类')
    const body = {
      title: form.title.trim(),
      category: form.category.trim(),
      visibility: form.visibility,
      scope: { product_ids: ids(form.products), category_ids: ids(form.categories) },
    }
    const payload = editing.value ? { ...body, expected_version: selected.value!.version } : body
    const doc = editing.value
      ? await api.update(selected.value!.id, payload, key('update:' + selected.value!.id, payload))
      : await api.create(payload, key('create', payload))
    documentDialog.value = false
    await load()
    await select(doc)
    ElMessage.success('资料已保存')
  })
}
function openVersion() {
  sourceText.value = ''
  changeNote.value = ''
  file.value = undefined
  sourceKind.value = 'text'
  versionDialog.value = true
}
function chooseFile(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0]
}
async function saveVersion() {
  await action(async () => {
    if (!selected.value) return
    let source: { source_text: string } | { source_attachment_id: string }
    if (sourceKind.value === 'text') {
      if (!sourceText.value.trim()) throw new Error('请填写原始资料正文')
      source = { source_text: sourceText.value }
    } else {
      if (!file.value || file.value.size > 20 * 1024 * 1024) throw new Error('请选择20MB以内的文件')
      const f = file.value
      const uploaded = await api.upload(f, key('upload', [f.name, f.size, f.lastModified]))
      source = { source_attachment_id: uploaded.id }
    }
    const body = { ...source, change_note: changeNote.value }
    const v = await api.newVersion(
      selected.value.id,
      body,
      key('version:' + selected.value.id, body),
    )
    versionDialog.value = false
    await refresh()
    await showVersion(v)
    ElMessage.success('草稿版本已创建，请解析原文')
  })
}
async function savePreview() {
  await action(async () => {
    const v = current.value!
    current.value = await api.edit(
      v,
      effectiveText.value,
      key('preview:' + v.id, [v.version, effectiveText.value]),
      previewPages.value.length ? previewPages.value : undefined,
    )
    savedText.value = effectiveText.value
    ElMessage.success('校对文本已保存')
  })
}
async function startTask(kind: 'parse' | 'build') {
  await action(async () => {
    const v = current.value!
    job.value = await api.task(v, kind, key(kind + v.id, v.version))
    await refresh()
    poll()
  })
}
function poll() {
  clearTimeout(timer)
  if (disposed || !job.value || !['QUEUED', 'RUNNING'].includes(job.value.state)) return
  timer = setTimeout(() => {
    void refreshJob()
  }, 1800)
}
async function refreshJob() {
  if (!job.value) return
  const id = job.value.id
  try {
    const j = await api.job(id)
    if (job.value?.id !== id || disposed) return
    job.value = j
    if (!['QUEUED', 'RUNNING'].includes(j.state)) await refresh()
    poll()
  } catch (e) {
    fail(e)
  }
}
async function retryJob() {
  await action(async () => {
    const j = job.value!
    job.value = await api.retry(j, key('retry:' + j.id, j.version))
    poll()
  })
}
async function publish() {
  await action(async () => {
    const rollback = current.value?.state === 'RETIRED'
    await ElMessageBox.confirm(
      rollback ? '确认将当前发布切换到所选历史版本？当前版本会保留，旧来源将按所选版本重新核验。' : '确认原文、适用范围和政策内容已经业务审核？发布后会员咨询可引用这份资料。',
      rollback ? '确认回滚版本' : '确认发布',
      { confirmButtonText: rollback ? '确认回滚' : '确认发布', cancelButtonText: '取消', type: 'warning' },
    )
    const d = selected.value!,
      v = current.value!
    selected.value = await api.publish(d, v, key('publish:' + d.id, [d.version, v.id]))
    await refresh()
    ElMessage.success(rollback ? '已回滚到所选历史版本' : '资料版本已发布')
  })
}
async function toggleEnabled() {
  await action(async () => {
    const d = selected.value!
    await ElMessageBox.confirm(
      d.enabled
        ? '停用后，新回答和来源读取将停止使用这份资料。'
        : '启用后，当前发布版本可重新用于回答。',
      d.enabled ? '停用资料' : '启用资料',
      { type: 'warning' },
    )
    selected.value = await api.enabled(
      d,
      !d.enabled,
      key('enabled:' + d.id, [d.version, !d.enabled]),
    )
    await refresh()
  })
}
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  selection++
  clearTimeout(timer)
})
</script>

<style scoped>
.knowledge-page {
  padding: 28px;
  max-width: 1450px;
  margin: auto;
  color: #243445;
}
header,
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
}
h1 {
  font-size: 25px;
  margin: 0;
}
h2 {
  font-size: 21px;
  margin: 0;
}
h3 {
  font-size: 17px;
}
p {
  color: #667789;
  line-height: 1.6;
}
.toolbar {
  display: flex;
  gap: 10px;
  margin: 22px 0;
}
.toolbar .el-input {
  max-width: 380px;
}
.el-alert {
  margin: 14px 0;
}
.detail {
  margin-top: 30px;
  padding-top: 24px;
  border-top: 1px solid #e5eaf0;
}
.version-panel {
  margin-top: 22px;
  padding: 20px;
  background: #f7f9fc;
  border: 1px solid #e5eaf0;
  border-radius: 10px;
}
.job-panel {
  display: flex;
  gap: 16px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 18px;
  padding: 18px;
  background: #f0f6ff;
  border-radius: 8px;
}
.warning {
  color: #916515;
}
.more {
  margin-top: 12px;
}
.empty {
  padding: 24px;
  text-align: center;
}
.el-form-item p {
  font-size: 13px;
}
@media (max-width: 850px) {
  .knowledge-page {
    padding: 16px;
  }
  .section-head {
    display: block;
  }
  .section-head > div:last-child {
    margin: 12px 0;
  }
  .section-head .el-button {
    margin: 4px;
  }
  .toolbar {
    flex-wrap: wrap;
  }
}
</style>
