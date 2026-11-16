export const workspaces = {
  service: {
    title: '客服工作台',
    description: '集中处理等待接待、本人接待和历史会话。',
    sections: ['等待接待', '我的会话', '历史接待'],
    unavailable: '人工接待暂未开放',
    explanation: '开放后可在此领取咨询、回复买家、记录内部备注和转交会话。',
  },
  knowledge: {
    title: '知识管理',
    description: '维护商品说明、服务规则与常见问题的知识版本。',
    sections: ['文档', '版本预览', '构建任务'],
    unavailable: '知识维护暂未开放',
    explanation: '开放后可上传原件、校对解析内容、发布版本和停用知识。',
  },
  models: {
    title: '模型与提示词',
    description: '维护模型能力、配置版本、提示词与用量。',
    sections: ['模型配置', '提示词版本', '调用用量'],
    unavailable: '后台配置暂未开放',
    explanation: '现有咨询使用已配置的模型；后台维护功能开放后可在此管理。',
  },
  governance: {
    title: '治理与任务',
    description: '查看授权范围内的后台任务、审计记录与留存策略。',
    sections: ['后台任务', '审计记录', '留存策略'],
    unavailable: '治理工作台暂未开放',
    explanation: '开放后可查看处理进度、核对失败原因并管理留存策略。',
  },
  operations: {
    title: '业务操作',
    description: '处理有权限的售后操作与演示物流记录。',
    sections: ['售后处理', '模拟物流', '操作记录'],
    unavailable: '智能体业务操作暂未开放',
    explanation: '当前商城原有订单与售后页面仍可按原流程使用。',
  },
} as const

export type WorkspaceSection = keyof typeof workspaces
