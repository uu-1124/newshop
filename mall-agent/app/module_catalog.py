"""Activation map for the reviewed M01–M20 architecture. Values describe this slice only."""

MODULES = {
    "M01": {
        "name": "身份与授权",
        "state": "PARTIAL",
        "owner": "api.dependencies/infrastructure.java_bridge/staff_auth/runtime_auth",
    },
    "M02": {"name": "会话与历史", "state": "PARTIAL", "owner": "application.conversations"},
    "M03": {
        "name": "消息与事件",
        "state": "PARTIAL",
        "owner": "application.conversations/api.member/postgres.database/wakeups/outbox",
    },
    "M04": {
        "name": "智能体运行",
        "state": "PARTIAL",
        "owner": "application.runs/agents.live_workflow/agents.workflow/workers.generation",
    },
    "M05": {"name": "工具与商城查询", "state": "PARTIAL", "owner": "infrastructure.java_bridge"},
    "M06": {
        "name": "确认与结果核对",
        "state": "PARTIAL",
        "owner": "application.actions/action_security; ADD_CART, DIRECT/CART checkout and confirmed cancellation",
    },
    "M07": {
        "name": "人工接待",
        "state": "PARTIAL",
        "owner": "application.handoffs/api.handoffs/staff_events; images, cards, scoped business context",
    },
    "M08": {
        "name": "知识管理",
        "state": "PARTIAL",
        "owner": "application.knowledge/knowledge_files/knowledge_parse",
    },
    "M09": {
        "name": "知识检索",
        "state": "PARTIAL",
        "owner": "application.retrieval/agents.knowledge_answer/knowledge_index; trusted product/category scopes",
    },
    "M10": {
        "name": "媒体与附件",
        "state": "PARTIAL",
        "owner": "application.attachments/media/private_objects/gpt_vision; private images, editable ASR and segmented TTS",
    },
    "M11": {
        "name": "模型与提示词",
        "state": "PARTIAL",
        "owner": "dialog_provider/aliyun_models/model_ledger/model_admin/model_compatibility",
    },
    "M12": {
        "name": "留存与删除",
        "state": "PARTIAL",
        "owner": "conversation_cleanup; tombstone-bound deletion",
    },
    "M13": {
        "name": "审计与统计",
        "state": "PARTIAL",
        "owner": "api.admin/application.governance/feedback; scoped statistics, model usage and member answer feedback",
    },
    "M14": {
        "name": "后台任务",
        "state": "PARTIAL",
        "owner": "application.jobs/workers.jobs; runtime check, knowledge parse and index active",
    },
    "M15": {"name": "买家前端", "state": "PARTIAL", "owner": "mall-app-web/src/pages/agent/chat.vue"},
    "M16": {
        "name": "员工前端",
        "state": "PARTIAL",
        "owner": "mall-admin-web/views/agent/Knowledge.vue/Service.vue/Models.vue",
    },
    "M17": {
        "name": "Java查询桥接",
        "state": "PARTIAL",
        "owner": "AgentMemberBridgeController/AgentStaffBridgeController",
    },
    "M18": {
        "name": "Java交易桥接",
        "state": "PARTIAL",
        "owner": "AgentCartService/AgentOrderService/AgentCartBridgeController; ADD_CART, DIRECT/CART checkout and confirmed cancellation",
    },
    "M19": {
        "name": "Java售后与物流",
        "state": "PARTIAL",
        "owner": "AgentOrderLifecycle/AgentAfterSaleStore; item return, review, receipt, demo refund, versioned logistics",
    },
    "M20": {
        "name": "部署与恢复",
        "state": "PARTIAL",
        "owner": "deployment/scripts/migrations/workers.scheduler",
    },
}
