<template>
  <view class="agent-page" @keydown.esc="closePanels">
    <view class="support-topbar">
      <button class="brand" aria-label="返回商城首页" @click="goHome"
        ><text class="brand-mark">M</text
        ><text class="brand-name"
          >MALL<text class="brand-caption">品质好物 · 贴心服务</text></text
        ></button
      >
      <view class="topbar-title"
        ><text>客服中心</text><text class="environment-badge">演示商城</text></view
      >
      <button class="home-link" @click="goHome"><SupportIcon name="shop" /> 返回商城</button>
    </view>
    <view class="workspace-heading"
      ><view
        ><text class="workspace-title">我的客服</text
        ><text class="workspace-caption">每一个问题，都值得被认真对待</text></view
      ><view class="service-promise"
        ><SupportIcon name="shield" /><text>商城专属服务</text></view
      ></view
    >
    <view class="support-workspace">
      <view
        v-if="!needsLogin"
        :class="['history-panel', { 'is-open': historyVisible }]"
        aria-label="会话列表"
      >
        <view class="panel-heading"
          ><text>咨询会话</text
          ><button
            class="icon-button mobile-only"
            aria-label="关闭会话列表"
            @click="historyVisible = false"
            ><SupportIcon name="close" /></button
        ></view>
        <button class="new-conversation" :disabled="busy || needsLogin" @click="startConsultation"
          ><SupportIcon name="plus" /> 发起新咨询</button
        >
        <view class="history-search">
          <input
            v-model="searchDraft"
            :maxlength="100"
            aria-label="搜索会话标题"
            placeholder="搜索会话标题"
            @confirm="searchConversations"
          />
          <button size="mini" :disabled="listing" @click="searchConversations">搜索</button>
          <button size="mini" :disabled="listing" @click="resetSearch">全部会话</button>
          <button size="mini" :disabled="listing" @click="loadConversations(false)"
            >刷新列表</button
          >
        </view>
        <view class="history" aria-label="历史会话">
          <view
            v-for="item in conversations"
            :key="item.id"
            :class="['history-item', { selected: item.id === selected }]"
          >
            <button size="mini" class="history-open" @click="selectFromHistory(item.id)">
              <text class="history-title">{{ item.title }}</text
              ><text v-if="item.unread_count" class="unread">{{ item.unread_count }} 条未读</text>
              <text class="history-preview">{{ item.last_message_preview || '暂无消息' }}</text>
            </button>
          </view>
          <text v-if="!listing && !conversations.length" class="label">{{
            searchQuery ? '未找到匹配会话，可换个标题关键词。' : '暂无历史会话。'
          }}</text>
          <button
            v-if="listCursor"
            size="mini"
            :disabled="listing"
            @click="loadConversations(true)"
            >{{ listing ? '正在读取…' : '加载更多会话' }}</button
          >
        </view>

        <view class="history-footer"
          ><SupportIcon name="clock" /><text>历史咨询，随时接着聊</text></view
        >
      </view>
      <view class="conversation-panel">
        <view class="conversation-heading">
          <button
            v-if="!needsLogin"
            class="icon-button mobile-only"
            aria-label="打开会话列表"
            :aria-expanded="historyVisible"
            @click="openPanel('history')"
            ><SupportIcon name="chat"
          /></button>
          <view class="service-avatar"><SupportIcon name="headset" /></view>
          <view class="service-heading"
            ><text class="heading">商城智能客服</text
            ><text class="mode">{{
              needsLogin
                ? '登录后享受专属咨询服务'
                : current
                ? receptionLabel
                : '您的购物与售后小助手'
            }}</text></view
          >
          <button
            v-if="current"
            class="service-toggle mobile-only"
            :aria-expanded="servicesVisible"
            @click="openPanel('services')"
            >服务 <SupportIcon name="bag"
          /></button>
        </view>
        <view v-if="needsLogin" class="login-state">
          <view class="welcome-avatar"><SupportIcon name="headset" /></view
          ><text class="welcome-title">您的专属客服，在这里</text
          ><text class="welcome-description">登录后可咨询商品、查询订单，随时找回聊天记录。</text
          ><button class="primary-button" @click="login"
            >登录并开始咨询 <SupportIcon name="arrow"
          /></button>
        </view>
        <template v-else>
          <view v-if="current" class="conversation-controls"
            ><text class="current-title">{{ current.title }}</text
            ><view class="conversation-tools"
              ><button size="mini" :disabled="busy" @click="beginRename">重命名</button
              ><button size="mini" :disabled="busy" @click="deleting = true">删除会话</button></view
            ></view
          >
          <view v-if="renaming" class="conversation-form">
            <input
              v-model="renameTitle"
              :maxlength="100"
              aria-label="会话新标题"
              placeholder="输入新标题"
            />
            <button size="mini" :disabled="busy || !renameTitle.trim()" @click="renameConversation"
              >保存标题</button
            >
            <button size="mini" :disabled="busy" @click="renaming = false">取消改名</button>
          </view>
          <view
            v-if="deleting && current"
            class="conversation-form"
            role="dialog"
            aria-label="删除会话确认"
          >
            <text
              >删除“{{
                current.title
              }}”后将无法再查看此会话。聊天资料会安排清理，订单和操作记录按独立期限保留。</text
            >
            <button size="mini" :disabled="busy" @click="deleteConversation">确认删除会话</button>
            <button size="mini" :disabled="busy" @click="deleting = false">保留会话</button>
          </view>
          <view v-if="error" class="hint error" role="alert"
            ><text>{{ error }}</text
            ><button size="mini" @click="load">刷新</button></view
          >

          <view v-if="!selected && !error" class="empty-state"
            ><view class="welcome-avatar"><SupportIcon name="chat" /></view
            ><text class="welcome-title">有问题，随时来聊聊</text
            ><text class="welcome-description">从一段新咨询开始，帮您解决选购与售后问题。</text
            ><button class="primary-button" :disabled="busy" @click="newConversation"
              >开始咨询 <SupportIcon name="arrow" /></button
          ></view>
          <template v-if="selected">
            <view
              v-if="connectionStatus !== 'connected'"
              class="hint connection-hint"
              role="status"
              >{{
                connectionStatus === 'connecting'
                  ? '正在同步会话…'
                  : '实时连接已中断，正在恢复消息…'
              }}</view
            >
            <view v-if="current" class="handoff-status">
              <text>{{ receptionLabel }}</text>
              <button
                v-if="current.state === 'AI_ACTIVE'"
                size="mini"
                :disabled="busy"
                @click="handoffCommand('request')"
                >转人工</button
              >
              <template v-if="current.state === 'WAITING_HUMAN'">
                <button size="mini" :disabled="busy" @click="handoffCommand('withdraw')"
                  >取消排队</button
                >
                <button size="mini" :disabled="busy" @click="handoffCommand('toggle')">{{
                  current.waiting_ai_enabled ? '暂停等待期间的 AI' : '等待期间继续使用 AI'
                }}</button>
              </template>
            </view>
            <view ref="chatFeed" class="messages" aria-live="polite" @scroll="trackChatScroll">
              <view v-if="!messages.length" class="welcome">
                <view class="welcome-avatar"><SupportIcon name="headset" /></view>
                <text class="welcome-eyebrow">MALL · 智能客服</text>
                <text class="welcome-title">您好，很高兴为您服务</text>
                <text class="welcome-description">选购建议、订单进度、售后问题，都可以问我。</text>
                <view class="suggestions" aria-label="常见咨询">
                  <button
                    :disabled="busy || !!activeRun || current?.state === 'CLOSED'"
                    @click="draft = '帮我查询一下订单的物流进度'"
                    class="suggestion"
                    ><SupportIcon name="box" /><text>查订单物流</text><SupportIcon name="arrow"
                  /></button>
                  <button
                    :disabled="busy || !!activeRun || current?.state === 'CLOSED'"
                    @click="draft = '我想了解如何申请退换货'"
                    class="suggestion"
                    ><SupportIcon name="return" /><text>退换货咨询</text><SupportIcon name="arrow"
                  /></button>
                  <button
                    :disabled="busy || !!activeRun || current?.state === 'CLOSED'"
                    @click="draft = '我想选购商品，请帮我推荐一下'"
                    class="suggestion"
                    ><SupportIcon name="bag" /><text>帮我选商品</text><SupportIcon name="arrow"
                  /></button>
                </view>
                <text class="suggestion-tip">点击问题填入输入框，补充后再发送</text>
              </view>
              <view v-else class="timeline-label"
                ><SupportIcon name="shield" /><text>商城客服为您服务</text></view
              >
              <button class="older-button" v-if="historyCursor" size="mini" @click="older"
                >加载更早消息</button
              >
              <view
                v-for="message in messages"
                :key="message.id"
                :class="['message', message.sender_kind.toLowerCase()]"
              >
                <view class="message-avatar" aria-hidden="true"
                  ><SupportIcon
                    :name="
                      message.sender_kind === 'MEMBER'
                        ? 'user'
                        : message.sender_kind === 'SYSTEM'
                        ? 'shield'
                        : 'headset'
                    "
                /></view>
                <view class="message-content">
                  <text class="sender-name">{{ message.sender_display_name }}</text>
                  <view v-if="imageBlocks(message).length" class="message-images">
                    <view v-for="picture in imageBlocks(message)" :key="picture.id">
                      <image
                        v-if="imageUrls[picture.id]"
                        :src="imageUrls[picture.id]"
                        mode="aspectFit"
                        class="chat-image"
                        :aria-label="picture.name"
                        @click="preview(imageUrls[picture.id])"
                      />
                      <text v-else class="label">{{
                        imageErrors[picture.id] || '图片加载中…'
                      }}</text>
                    </view>
                  </view>
                  <text class="body" selectable>{{
                    message.text || (message.status === 'GENERATING' ? '正在准备回复…' : '')
                  }}</text>
                  <CartCards
                    v-if="
                      current && (productBlocks(message).length || actionBlocks(message).length)
                    "
                    :products="productBlocks(message)"
                    :actions="actionBlocks(message)"
                    :conversation="current"
                    :disabled="!aiEnabled || !!activeRun || busy"
                    @changed="refreshCurrent"
                  />
                  <view
                    v-for="order in message.blocks
                .filter((b) => b.type === 'order' && b.order)
                .map((b) => b.order!)"
                    :key="order.id"
                    class="source-card"
                    ><text class="body"
                      >订单 {{ order.order_sn }} · 查询时{{ order.status_label }} · ¥{{
                        order.pay_amount
                      }}</text
                    ><button size="mini" @click="openOrderPanel(order.id)"
                      >读取最新订单、物流与售后</button
                    ></view
                  >
                  <view
                    v-for="order in staffOrderBlocks(message)"
                    :key="order.id"
                    class="source-card"
                  >
                    <text class="body"
                      >客服分享订单 {{ order.order_sn }} · {{ order.status_label }}</text
                    >
                    <text class="label">发送时实付金额 ¥{{ order.pay_amount ?? '未提供' }}</text>
                    <text v-for="(line, n) in order.lines" :key="n" class="label"
                      >{{ line.name }} × {{ line.quantity }} · {{ line.specification }}</text
                    >
                    <button size="mini" @click="openStaffOrder(order.id)">查看商城当前订单</button>
                  </view>
                  <view
                    v-for="source in sourceBlocks(message)"
                    :key="source.chunk_id"
                    class="source-card"
                  >
                    <button size="mini" @click="readSource(source)"
                      >查看来源 · {{ source.title }} {{ source.section }}</button
                    >
                    <text v-if="sourceErrors[source.chunk_id]" class="label">{{
                      sourceErrors[source.chunk_id]
                    }}</text>
                  </view>
                  <text v-if="message.status === 'STOPPED'" class="label">已停止</text>
                  <AnswerFeedback
                    v-if="message.sender_kind === 'ASSISTANT' && message.status === 'COMPLETED'"
                    :key="message.id"
                    :message-id="message.id"
                  />
                  <AnswerSpeech
                    v-if="
                      message.sender_kind === 'ASSISTANT' &&
                      message.status === 'COMPLETED' &&
                      message.text.trim()
                    "
                    :key="`${message.id}-${voiceSession}`"
                    :message-id="message.id"
                  />
                  <text v-if="['FAILED', 'INTERRUPTED'].includes(message.status)" class="label">{{
                    message.reply_failure?.message || '本次回复未完成，原问题已保留。'
                  }}</text>
                  <text v-if="retryDelayed(message)" class="label"
                    >下个额度窗口：{{
                      new Date(message.reply_failure!.retry_after!).toLocaleString()
                    }}。窗口更新后可重试，仍以实际额度与服务状态为准。</text
                  >
                  <button
                    v-if="canRetry(message)"
                    size="mini"
                    :disabled="busy || !!activeRun || !aiEnabled || retryDelayed(message)"
                    @click="retryReply(message)"
                    >重试此回复</button
                  >
                </view>
              </view>
            </view>

            <view v-if="current?.state !== 'CLOSED'" class="composer-panel">
              <view v-if="pictures.length" class="pending-images">
                <view v-for="picture in pictures" :key="picture.key" class="pending-image">
                  <image :src="picture.preview" mode="aspectFit" class="chat-image" />
                  <text class="label">{{
                    picture.state === 'uploading'
                      ? '正在上传…'
                      : picture.state === 'failed'
                      ? '上传失败，点击重试'
                      : '已上传，待发送'
                  }}</text>
                  <button
                    v-if="picture.state === 'failed'"
                    size="mini"
                    :disabled="busy"
                    @click="uploadPicture(picture)"
                    >重试</button
                  >
                  <button
                    size="mini"
                    :disabled="busy || picture.state === 'uploading'"
                    @click="removePicture(picture.key)"
                    >移除</button
                  >
                </view>
              </view>
              <text v-if="hasPendingConfirmation && !activeRun" class="label image-tip"
                >发送新消息后，之前未确认的操作卡会失效；请按最新内容重新准备并核对。</text
              >

              <view class="composer-toolbar">
                <button
                  size="mini"
                  :disabled="busy || !!activeRun || pictures.length >= 2 || uploading"
                  @click="choosePictures"
                  ><SupportIcon name="image" /> 图片</button
                >
                <VoiceComposer
                  :key="`${selected}-${voiceSession}`"
                  :disabled="busy || !!activeRun"
                  @transcribed="appendTranscription"
                />
              </view>
              <view class="compose">
                <textarea
                  v-model="draft"
                  :maxlength="10000"
                  aria-label="咨询内容"
                  placeholder="请描述您遇到的问题，我来为您解答…"
                />
                <button
                  v-if="activeRun"
                  class="send-button"
                  size="mini"
                  :disabled="busy"
                  @click="stop"
                  >停止</button
                >
                <button
                  v-else
                  class="send-button"
                  size="mini"
                  :disabled="
                    busy ||
                    uploading ||
                    pictures.some((p) => p.state !== 'ready') ||
                    (!draft.trim() && !pictures.length)
                  "
                  @click="send"
                  >发送</button
                >
              </view>
              <text v-if="aiEnabled" class="label image-tip"
                >每次最多2张静态图片，每张不超过5MB。图片用于理解线索，商品信息以商城查询为准。</text
              >
              <text v-else class="label image-tip"
                >可发送文字和图片，每次最多2张，每张不超过5MB。等待期间的消息会保留供客服查看，不会自动启动AI。</text
              >
            </view>
          </template>
        </template>
      </view>
      <view
        v-if="!needsLogin && current"
        :class="['service-panel', { 'is-open': servicesVisible }]"
        aria-label="自助服务"
      >
        <view class="panel-heading"
          ><text>自助服务</text
          ><button
            class="icon-button mobile-only"
            aria-label="关闭自助服务"
            @click="servicesVisible = false"
            ><SupportIcon name="close" /></button
        ></view>
        <view class="service-panel-intro"
          ><view class="service-intro-icon"><SupportIcon name="bag" /></view
          ><text class="service-intro-title">购物问题，轻松解决</text
          ><text class="service-intro-description">订单、物流、售后，一站式处理</text></view
        >
        <view class="service-section-label">为您提供以下服务</view>
        <OrderCheckout
          v-if="current"
          :conversation="current"
          :disabled="!aiEnabled || !!activeRun || busy"
          @changed="refreshCurrent"
        />
        <OrderBrowser
          v-if="current"
          :conversation="current"
          :disabled="!aiEnabled || !!activeRun || busy"
          :order-id="orderPanelId"
          :open-request="orderPanelRequest"
          @changed="refreshCurrent"
        />
        <ProductBrowser
          v-if="current"
          :conversation="current"
          :disabled="!aiEnabled || !!activeRun || busy"
          @changed="refreshCurrent"
          @consult="productConversation"
        />

        <view class="service-note"
          ><SupportIcon name="shield" /><view
            ><text class="service-note-title">操作前，请您确认</text
            ><text>加购、下单、取消和售后申请，均需在确认卡上核对后提交。</text></view
          ></view
        >
        <view class="service-bottom"
          ><text>用心服务每一次咨询</text><text>MALL CUSTOMER SERVICE</text></view
        >
      </view>
      <view
        v-if="historyVisible || servicesVisible"
        class="panel-backdrop mobile-only"
        aria-label="关闭侧边面板"
        role="button"
        tabindex="0"
        @click="closePanels"
        @keydown.enter="closePanels"
      />
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { onHide, onShow, onUnload, onLoad } from '@dcloudio/uni-app'
import { agent, AgentError, agentId, streamAgent } from '@/services/agent'
import {
  applyMessageDelta,
  compareEventIds,
  mergeSnapshotMessages,
  snapshotIsCurrent,
} from '@/services/agentStream'
import SupportIcon from './SupportIcon.vue'
import CartCards from './CartCards.vue'
import OrderCheckout from './OrderCheckout.vue'
import OrderBrowser from './OrderBrowser.vue'
import ProductBrowser from './ProductBrowser.vue'
import AnswerFeedback from './AnswerFeedback.vue'
import AnswerSpeech from './AnswerSpeech.vue'
import VoiceComposer from './VoiceComposer.vue'
import { stopAllSpeech } from '@/services/agentAudio'
// Presentation state stays local; business commands and subscriptions are unchanged.
const historyVisible = ref(false)
const servicesVisible = ref(false)
function closePanels() {
  historyVisible.value = false
  servicesVisible.value = false
}
function openPanel(panel: 'history' | 'services') {
  historyVisible.value = panel === 'history'
  servicesVisible.value = panel === 'services'
}
function selectFromHistory(id: string) {
  closePanels()
  void select(id)
}
function startConsultation() {
  closePanels()
  void newConversation()
}
const chatFeed = ref<HTMLElement | { $el: HTMLElement } | null>(null)
let followLatest = true
function feedElement() {
  const feed = chatFeed.value
  return feed && ('$el' in feed ? feed.$el : feed)
}
function trackChatScroll() {
  const feed = feedElement()
  if (feed) followLatest = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80
}
function goHome() {
  uni.switchTab({ url: '/pages/index/index' })
}
const voiceSession = ref(0)
function appendTranscription(text: string) {
  draft.value = draft.value.trim() ? draft.value + '\n' + text : text
}
import type { Action, Product } from '@/services/agent'
const orderPanelId = ref('')
const orderPanelRequest = ref(0)
function openOrderPanel(id: string) {
  servicesVisible.value = true
  orderPanelId.value = id
  orderPanelRequest.value++
}
const entryOrderId = ref('')
onLoad((options) => {
  if (options?.orderId && /^[1-9][0-9]{0,18}$/.test(String(options.orderId)))
    entryOrderId.value = String(options.orderId)
})
import type {
  Attachment,
  ChatMessage,
  Conversation,
  Handoff,
  Run,
  Snapshot,
  Source,
} from '@/services/agent'

const conversations = ref<Conversation[]>([])
const searchDraft = ref(''),
  searchQuery = ref(''),
  listCursor = ref<string | null>(null),
  listing = ref(false)
const renaming = ref(false),
  renameTitle = ref(''),
  deleting = ref(false)
let listGeneration = 0
let pageVisible = false
const commandKeys = new Map<string, string>()
function commandKey(value: string) {
  if (!commandKeys.has(value)) commandKeys.set(value, agentId())
  return commandKeys.get(value)!
}
const selected = ref('')
const messages = ref<ChatMessage[]>([])
watch(
  selected,
  () => {
    followLatest = true
  },
  { flush: 'sync' },
)
watch(
  () => {
    const last = messages.value[messages.value.length - 1]
    return [selected.value, last?.id, last?.text, last?.status, last?.blocks]
  },
  async () => {
    if (!followLatest) return
    await nextTick()
    if (!followLatest) return
    const feed = feedElement()
    if (feed) feed.scrollTop = messages.value.length ? feed.scrollHeight : 0
  },
  { flush: 'post' },
)
const activeRun = ref<Run | null>(null)
const pageTime = ref(Date.now())
const current = ref<Conversation | null>(null)
const handoff = ref<Handoff | null>(null)
const connectionStatus = ref<'connecting' | 'connected' | 'reconnecting'>('connecting')
let appliedCursor = '0'
const blockedRuns = new Set<string>()
const failedControls = new Map<string, { conversationId: string; epoch: number }>()
const hasPendingConfirmation = computed(() =>
  messages.value.some((message) =>
    message.blocks.some(
      (block) => block.type === 'action' && block.action?.state === 'PENDING_CONFIRMATION',
    ),
  ),
)
const aiEnabled = computed(
  () =>
    current.value?.state === 'AI_ACTIVE' ||
    (current.value?.state === 'WAITING_HUMAN' && current.value.waiting_ai_enabled),
)
const receptionLabel = computed(() => {
  const c = current.value
  if (!c) return ''
  if (c.state === 'WAITING_HUMAN')
    return '正在等待人工接待 · ' + (c.waiting_ai_enabled ? 'AI 可继续回复' : 'AI 已暂停')
  if (c.state === 'HUMAN_ACTIVE')
    return `人工接待中 · ${c.assigned_staff?.display_name || '客服'}${
      c.assigned_staff?.presence === 'OFFLINE' ? '（暂时离线，接待仍保留）' : ''
    }`
  return c.state === 'CLOSED' ? '本次接待已结束，可新建会话继续咨询。' : 'AI 咨询'
})

function applyState(data: Snapshot, preserveHistory = true) {
  if (
    selected.value !== data.conversation.id ||
    !snapshotIsCurrent(data, current.value, appliedCursor)
  )
    return false
  // Failed commands are uncertain until an authoritative snapshot is accepted.
  // An unchanged epoch means generation may resume; an advanced epoch retains
  // the stop fence, including when the command succeeded but its response was lost.
  failedControls.forEach((control, runId) => {
    if (
      control.conversationId === data.conversation.id &&
      data.conversation.control_epoch >= control.epoch
    ) {
      if (data.conversation.control_epoch === control.epoch) blockedRuns.delete(runId)
      failedControls.delete(runId)
    }
  })
  const sameEpoch = current.value?.control_epoch === data.conversation.control_epoch
  messages.value = mergeSnapshotMessages(
    data,
    messages.value,
    current.value,
    preserveHistory,
    blockedRuns,
  )
  if (!preserveHistory || !sameEpoch) historyCursor.value = data.history_cursor
  current.value = data.conversation
  handoff.value = data.handoff
  activeRun.value = data.active_run
  appliedCursor = data.event_cursor
  conversations.value = conversations.value.map((item) =>
    item.id === data.conversation.id ? data.conversation : item,
  )
  return true
}

async function recoverFailedControl(conversation: Conversation, runId?: string) {
  if (selected.value !== conversation.id) return
  if (runId && blockedRuns.has(runId))
    failedControls.set(runId, {
      conversationId: conversation.id,
      epoch: conversation.control_epoch,
    })
  const data = await agent.snapshot(conversation.id).catch(() => null)
  if (data && selected.value === conversation.id) applyState(data)
}

const readRequests = new Map<string, Promise<unknown>>()
async function markSnapshotRead(data: Snapshot, signal?: AbortSignal) {
  const id = data.conversation.id
  if (signal?.aborted || selected.value !== id) return
  const completed = data.recent_messages.filter((message) => message.status !== 'GENERATING')
  const lastSeq = completed.at(-1)?.seq || '0'
  if (BigInt(lastSeq) <= BigInt(data.read_position.last_seq)) return
  const fingerprint = `${id}:${lastSeq}`
  if (readRequests.has(fingerprint)) return
  const request = agent
    .markRead(id, lastSeq, commandKey(`read:${fingerprint}`))
    .then((position) => {
      conversations.value = conversations.value.map((item) =>
        item.id === id ? { ...item, unread_count: position.unread_count } : item,
      )
      if (current.value?.id === id)
        current.value = { ...current.value, unread_count: position.unread_count }
    })
    .catch((e) => {
      if (!signal?.aborted && selected.value === id) problem(e)
    })
    .finally(() => readRequests.delete(fingerprint))
  readRequests.set(fingerprint, request)
  await request
}

async function handoffCommand(kind: 'request' | 'withdraw' | 'toggle') {
  const c = current.value,
    h = handoff.value
  if (!c || busy.value) return
  const runId = activeRun.value?.id
  busy.value = true
  try {
    if (runId) blockedRuns.add(runId)
    if (kind === 'request') await agent.handoff(c, agentId())
    else if (kind === 'withdraw' && h) await agent.withdraw(c, h, agentId())
    else if (kind === 'toggle') await agent.waitingAI(c, !c.waiting_ai_enabled, agentId())
    if (selected.value === c.id) {
      clearPictures()
      void select(c.id)
    }
  } catch (e) {
    if (selected.value === c.id) {
      problem(e)
      await recoverFailedControl(c, runId)
    }
  } finally {
    busy.value = false
  }
}
const historyCursor = ref<string | null>(null)
const draft = ref('')
const error = ref('')
const needsLogin = ref(false)
const busy = ref(false)
const sourceErrors = ref<Record<string, string>>({})
function productBlocks(message: ChatMessage): Product[] {
  return (message.blocks || []).flatMap((block) =>
    block.type === 'product' && block.product ? [block.product] : [],
  )
}
function staffOrderBlocks(message: ChatMessage) {
  return (message.blocks || []).flatMap((block) =>
    block.type === 'staff_order' && block.staff_order ? [block.staff_order] : [],
  )
}
function openStaffOrder(id: string) {
  uni.navigateTo({ url: '/pages/order/orderDetail?orderId=' + id })
}
function actionBlocks(message: ChatMessage): Action[] {
  return (message.blocks || []).flatMap((block) =>
    block.type === 'action' && block.action ? [block.action] : [],
  )
}
async function refreshCurrent() {
  const id = selected.value
  if (!id) return
  try {
    const data = await agent.snapshot(id)
    if (selected.value !== id) return
    if (applyState(data)) void markSnapshotRead(data, controller?.signal)
  } catch (e) {
    problem(e)
  }
}
function sourceBlocks(message: ChatMessage): Source[] {
  return (message.blocks || []).flatMap((block) =>
    block.type === 'source' && block.source ? [block.source] : [],
  )
}
async function readSource(source: Source) {
  try {
    const result = await agent.citation(source)
    sourceErrors.value[source.chunk_id] = ''
    uni.showModal({
      title: result.source.title,
      content: result.excerpt,
      showCancel: false,
      confirmText: '已了解',
    })
  } catch (reason) {
    sourceErrors.value[source.chunk_id] =
      reason instanceof AgentError && reason.status === 410
        ? '来源已更新或停用，请重新提问获取当前规则。'
        : reason instanceof Error
        ? reason.message
        : '来源读取失败'
  }
}
type PictureDraft = {
  key: string
  preview: string
  blob: Blob
  name: string
  state: 'uploading' | 'ready' | 'failed'
  attachment?: Attachment
}
const pictures = ref<PictureDraft[]>([])
const uploading = computed(() => pictures.value.some((item) => item.state === 'uploading'))
const imageUrls = ref<Record<string, string>>({})
const imageErrors = ref<Record<string, string>>({})
const loadingImages = new Map<string, AbortSignal>()
let controller: AbortController | undefined
let pending: { conversation: string; text: string; imageIds: string; id: string } | undefined
let createKey: string | undefined

function problem(reason: unknown) {
  if (reason instanceof AgentError && reason.status === 401) {
    needsLogin.value = true
    controller?.abort()
  }
  error.value = reason instanceof Error ? reason.message : '咨询服务暂不可用'
}

function login() {
  uni.navigateTo({ url: '/pages/public/login' })
}

function imageBlocks(message: ChatMessage): Attachment[] {
  return (message.blocks || []).flatMap((block) =>
    block.type === 'attachment' && block.attachment ? [block.attachment] : [],
  )
}

function clearPictures() {
  pictures.value.forEach((picture) => URL.revokeObjectURL(picture.preview))
  pictures.value = []
}

function clearImages() {
  Object.values(imageUrls.value).forEach((url) => URL.revokeObjectURL(url))
  imageUrls.value = {}
  imageErrors.value = {}
  loadingImages.clear()
}

async function loadImages(signal: AbortSignal) {
  for (const attachment of messages.value.flatMap(imageBlocks)) {
    const loading = loadingImages.get(attachment.id)
    if (
      signal.aborted ||
      imageUrls.value[attachment.id] ||
      (loading && !loading.aborted) ||
      imageErrors.value[attachment.id]
    )
      continue
    loadingImages.set(attachment.id, signal)
    try {
      const blob = await agent.image(attachment.id, signal)
      if (!signal.aborted) imageUrls.value[attachment.id] = URL.createObjectURL(blob)
    } catch (reason) {
      if (!signal.aborted)
        imageErrors.value[attachment.id] = reason instanceof Error ? reason.message : '图片加载失败'
    } finally {
      if (loadingImages.get(attachment.id) === signal) loadingImages.delete(attachment.id)
    }
  }
}

function preview(url: string) {
  uni.previewImage({ urls: [url] })
}

async function uploadPicture(picture: PictureDraft) {
  picture.state = 'uploading'
  try {
    picture.attachment = await agent.upload(picture.blob, picture.name, picture.key)
    picture.state = 'ready'
    error.value = ''
  } catch (reason) {
    picture.state = 'failed'
    problem(reason)
  }
}

function choosePictures() {
  const conversation = selected.value
  uni.chooseImage({
    count: 2 - pictures.value.length,
    sizeType: ['original'],
    sourceType: ['album', 'camera'],
    success: async (result) => {
      const paths = Array.isArray(result.tempFilePaths)
        ? result.tempFilePaths
        : [result.tempFilePaths]
      for (const path of paths.slice(0, 2 - pictures.value.length)) {
        try {
          const blob = await (await fetch(path)).blob()
          if (selected.value !== conversation) return
          if (blob.size > 5 * 1024 * 1024) throw new Error('图片大小不能超过5MB')
          const picture: PictureDraft = {
            key: agentId(),
            preview: URL.createObjectURL(blob),
            blob,
            name:
              '咨询图片.' +
              (blob.type === 'image/png' ? 'png' : blob.type === 'image/webp' ? 'webp' : 'jpg'),
            state: 'uploading',
          }
          pictures.value.push(picture)
          // Use the reactive array entry so asynchronous state changes update the view.
          await uploadPicture(pictures.value[pictures.value.length - 1])
        } catch (reason) {
          problem(reason)
        }
      }
    },
  })
}

function removePicture(key: string) {
  const picture = pictures.value.find((item) => item.key === key)
  if (picture) URL.revokeObjectURL(picture.preview)
  pictures.value = pictures.value.filter((item) => item.key !== key)
}

async function load() {
  needsLogin.value = !uni.getStorageSync('token')
  if (needsLogin.value) return
  error.value = ''
  if (entryOrderId.value) {
    const id = entryOrderId.value
    try {
      const result = await agent.createOrderConversation(commandKey(`order-entry:${id}`), id)
      conversations.value = [result]
      await select(result.id)
      orderPanelId.value = id
      entryOrderId.value = ''
    } catch (reason) {
      problem(reason)
    }
    return
  }
  await loadConversations(false, true)
}

async function loadConversations(more: boolean, chooseDefault = false) {
  if (more && (listing.value || !listCursor.value)) return
  const generation = ++listGeneration
  const query = searchQuery.value
  const cursor = more ? listCursor.value || undefined : undefined
  listing.value = true
  try {
    const page = await agent.list(query, cursor)
    if (!pageVisible || generation !== listGeneration || searchQuery.value !== query) return
    conversations.value = more
      ? [
          ...new Map(
            [...conversations.value, ...page.items].map((item) => [item.id, item]),
          ).values(),
        ]
      : page.items
    listCursor.value = page.next_cursor
    if (chooseDefault) {
      const id =
        conversations.value.find((item) => item.id === selected.value)?.id ||
        conversations.value[0]?.id
      if (id) void select(id)
      else clearCurrent()
    }
  } catch (e) {
    if (generation === listGeneration) problem(e)
  } finally {
    if (generation === listGeneration) listing.value = false
  }
}

async function searchConversations() {
  searchQuery.value = searchDraft.value.trim()
  await loadConversations(false)
}

async function resetSearch() {
  searchDraft.value = ''
  searchQuery.value = ''
  await loadConversations(false)
}

function clearCurrent() {
  controller?.abort()
  selected.value = ''
  current.value = null
  handoff.value = null
  messages.value = []
  activeRun.value = null
  historyCursor.value = null
  renaming.value = false
  deleting.value = false
  draft.value = ''
  pending = undefined
  appliedCursor = '0'
  blockedRuns.clear()
  failedControls.clear()
  clearPictures()
  clearImages()
}

function beginRename() {
  if (!current.value) return
  renameTitle.value = current.value.title
  renaming.value = true
  deleting.value = false
}

async function renameConversation() {
  const c = current.value,
    title = renameTitle.value.trim()
  if (!c || busy.value || !title) return
  busy.value = true
  try {
    const updated = await agent.rename(c, title, commandKey(`rename:${c.id}:${c.version}:${title}`))
    if (current.value?.id === c.id) current.value = updated
    renaming.value = false
    error.value = ''
    await loadConversations(false)
  } catch (e) {
    problem(e)
    await refreshCurrent()
  } finally {
    busy.value = false
  }
}

async function deleteConversation() {
  const c = current.value
  if (!c || busy.value || !deleting.value) return
  busy.value = true
  try {
    await agent.deleteConversation(c, commandKey(`delete:${c.id}:${c.version}`))
    if (selected.value === c.id) clearCurrent()
    conversations.value = conversations.value.filter((item) => item.id !== c.id)
    error.value = ''
    await loadConversations(false, true)
  } catch (e) {
    problem(e)
    await refreshCurrent()
  } finally {
    busy.value = false
  }
}

function retryDelayed(message: ChatMessage) {
  if (message.reply_failure?.code !== 'MODEL_QUOTA_EXCEEDED' || !message.reply_failure.retry_after)
    return false
  const deadline = Date.parse(message.reply_failure.retry_after)
  return Number.isFinite(deadline) && deadline > pageTime.value
}

function canRetry(message: ChatMessage) {
  return (
    message.sender_kind === 'ASSISTANT' &&
    !!message.run_id &&
    ['FAILED', 'INTERRUPTED', 'STOPPED'].includes(message.status) &&
    !messages.value.some(
      (item) =>
        ['MEMBER', 'ASSISTANT'].includes(item.sender_kind) &&
        BigInt(item.seq) > BigInt(message.seq),
    )
  )
}

async function retryReply(message: ChatMessage) {
  if (
    !canRetry(message) ||
    !message.run_id ||
    busy.value ||
    activeRun.value ||
    !aiEnabled.value ||
    retryDelayed(message)
  )
    return
  const cid = selected.value
  busy.value = true
  try {
    const old = await agent.run(message.run_id)
    await agent.retry(old, commandKey(`retry:${old.id}:${old.version}`))
    error.value = ''
    if (selected.value === cid) await refreshCurrent()
  } catch (e) {
    problem(e)
    if (selected.value === cid) await refreshCurrent()
  } finally {
    busy.value = false
  }
}

async function select(id: string) {
  if (!pageVisible) return
  controller?.abort()
  clearImages()
  const connection = new AbortController()
  controller = connection
  if (selected.value !== id) {
    appliedCursor = '0'
    blockedRuns.clear()
    failedControls.clear()
    messages.value = []
    activeRun.value = null
    current.value = null
    handoff.value = null
    historyCursor.value = null
    renaming.value = false
    deleting.value = false
    clearPictures()
    clearImages()
  }
  selected.value = id
  error.value = ''
  connectionStatus.value = 'connecting'
  let cursor: string | undefined
  while (!connection.signal.aborted) {
    try {
      // Every reconnect begins with an authenticated authoritative snapshot.
      // This also repairs terminal state missed while the connection was down.
      const data = await agent.snapshot(id)
      if (connection.signal.aborted) return
      if (applyState(data, false)) {
        void loadImages(connection.signal)
        void markSnapshotRead(data, connection.signal)
      }
      cursor = appliedCursor
      await streamAgent(
        id,
        cursor,
        connection.signal,
        async (event) => {
          if (connection.signal.aborted) return
          if (event.conversation_id !== id)
            throw new AgentError('实时消息归属不符，正在重新同步', 503)
          if (compareEventIds(event.event_id, appliedCursor) <= 0) {
            cursor = appliedCursor
            return
          }
          let resync = event.type !== 'message.delta'
          if (event.type === 'message.delta') {
            const result = applyMessageDelta(
              event,
              current.value,
              activeRun.value,
              messages.value,
              blockedRuns,
            )
            resync = result.kind === 'resync'
            if (result.kind === 'applied') {
              const position = messages.value.findIndex((item) => item.id === result.message.id)
              messages.value[position] = result.message
            }
          }
          if (resync) {
            const data = await agent.snapshot(id)
            if (connection.signal.aborted) return
            if (compareEventIds(data.event_cursor, event.event_id) < 0)
              throw new AgentError('消息状态尚未同步，正在重新读取', 503)
            if (applyState(data)) {
              void loadImages(connection.signal)
              void markSnapshotRead(data, connection.signal)
            }
          }
          if (compareEventIds(event.event_id, appliedCursor) > 0) appliedCursor = event.event_id
          cursor = appliedCursor
          error.value = ''
        },
        () => {
          connectionStatus.value = 'connected'
        },
      )
    } catch (e) {
      if (connection.signal.aborted) return
      if (e instanceof AgentError && e.status === 410) {
        cursor = undefined
        continue
      }
      problem(e)
      if (e instanceof AgentError && e.status === 404) {
        clearCurrent()
        conversations.value = conversations.value.filter((item) => item.id !== id)
        error.value = '此会话已删除或不可访问，请选择其他会话。'
        return
      }
      if (needsLogin.value) return
    }
    if (!connection.signal.aborted) connectionStatus.value = 'reconnecting'
    await new Promise((resolve) => setTimeout(resolve, 1000))
  }
}

async function newConversation() {
  busy.value = true
  try {
    const result = await agent.create((createKey ||= agentId()))
    createKey = undefined
    if (searchQuery.value) {
      searchDraft.value = ''
      searchQuery.value = ''
      listCursor.value = null
    }
    conversations.value = [result, ...conversations.value.filter((item) => item.id !== result.id)]
    draft.value = ''
    void select(result.id)
  } catch (e) {
    problem(e)
  } finally {
    busy.value = false
  }
}

async function productConversation(productId: string) {
  if (busy.value) return
  busy.value = true
  try {
    const result = await agent.create(
      commandKey(`product-conversation:${selected.value}:${productId}`),
      productId,
    )
    searchDraft.value = ''
    searchQuery.value = ''
    conversations.value = [result, ...conversations.value.filter((item) => item.id !== result.id)]
    draft.value = ''
    void select(result.id)
  } catch (reason) {
    problem(reason)
  } finally {
    busy.value = false
  }
}

async function send() {
  if (
    !selected.value ||
    current.value?.state === 'CLOSED' ||
    activeRun.value ||
    uploading.value ||
    pictures.value.some((p) => p.state !== 'ready') ||
    (!draft.value.trim() && !pictures.value.length)
  )
    return
  busy.value = true
  const id = selected.value,
    text = draft.value.trim()
  const attachmentIds = pictures.value.map((p) => p.attachment!.id)
  const imageIds = attachmentIds.join(',')
  if (
    !pending ||
    pending.conversation !== id ||
    pending.text !== text ||
    pending.imageIds !== imageIds
  )
    pending = { conversation: id, text, imageIds, id: agentId() }
  try {
    const result = await agent.send(id, text, pending.id, attachmentIds)
    pending = undefined
    if (selected.value === id) {
      if (!messages.value.some((item) => item.id === result.message.id))
        messages.value.push(result.message)
      const data = await agent.snapshot(id)
      if (selected.value === id) applyState(data)
      if (draft.value.trim() === text) draft.value = ''
      clearPictures()
      if (controller) void loadImages(controller.signal)
    }
    error.value = ''
  } catch (e) {
    problem(e)
  } finally {
    busy.value = false
  }
}

async function stop() {
  if (!activeRun.value || !current.value || busy.value) return
  const id = selected.value
  const conversation = current.value
  const runId = activeRun.value.id
  blockedRuns.add(runId)
  busy.value = true
  try {
    const current = await agent.run(runId)
    if (['QUEUED', 'RUNNING'].includes(current.state)) {
      try {
        await agent.stop(current)
      } catch (reason) {
        if (!(reason instanceof AgentError) || reason.status !== 409) throw reason
        const refreshed = await agent.run(current.id)
        if (['QUEUED', 'RUNNING'].includes(refreshed.state)) await agent.stop(refreshed)
      }
    }
    if (selected.value === id) void select(id)
  } catch (e) {
    if (selected.value === id) {
      problem(e)
      await recoverFailedControl(conversation, runId)
    }
  } finally {
    busy.value = false
  }
}

async function older() {
  if (!historyCursor.value) return
  const id = selected.value
  try {
    const page = await agent.history(id, historyCursor.value)
    if (selected.value !== id) return
    messages.value = [
      ...page.items.filter((item) => !messages.value.some((msg) => msg.id === item.id)),
      ...messages.value,
    ]
    if (controller) void loadImages(controller.signal)
    historyCursor.value = page.next_cursor
  } catch (e) {
    problem(e)
  }
}

let presenceTimer: ReturnType<typeof setInterval> | undefined
let checkingPresence = false
async function refreshListStatus() {
  if (
    listing.value ||
    busy.value ||
    needsLogin.value ||
    (typeof document !== 'undefined' && document.hidden)
  )
    return
  const generation = listGeneration,
    query = searchQuery.value
  const first = await agent.list(query)
  if (generation !== listGeneration || query !== searchQuery.value) return
  conversations.value = [
    ...first.items,
    ...conversations.value.filter((item) => !first.items.some((current) => current.id === item.id)),
  ]
}
onShow(() => {
  pageTime.value = Date.now()
  pageVisible = true
  void load()
  clearInterval(presenceTimer)
  presenceTimer = setInterval(async () => {
    pageTime.value = Date.now()
    if (checkingPresence) return
    checkingPresence = true
    const id = selected.value
    try {
      await refreshListStatus()
      if (current.value?.state === 'HUMAN_ACTIVE') {
        const data = await agent.snapshot(id)
        if (selected.value === id && !controller?.signal.aborted) applyState(data)
      }
    } catch (e) {
      problem(e)
    } finally {
      checkingPresence = false
    }
  }, 15000)
})
onHide(() => {
  voiceSession.value++
  stopAllSpeech()
  pageVisible = false
  listGeneration++
  listing.value = false
  controller?.abort()
  clearInterval(presenceTimer)
})
onUnload(() => {
  stopAllSpeech()
  pageVisible = false
  listGeneration++
  controller?.abort()
  clearInterval(presenceTimer)
  clearPictures()
  clearImages()
})
if (import.meta.hot)
  import.meta.hot.dispose(() => {
    pageVisible = false
    listGeneration++
    controller?.abort()
    clearInterval(presenceTimer)
    clearPictures()
    clearImages()
  })
</script>

<style scoped src="./customer-service.css"></style>
