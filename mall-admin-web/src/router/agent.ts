import Layout from '@/views/layout/Layout.vue'
import type { RouteRecordExt } from '@/types/router'

// These routes pass through the existing server-menu filter. No role is granted
// by adding a route; unfinished sections continue to show their capability boundary.
export const agentRouter: RouteRecordExt = {
  path: '/agent',
  component: Layout,
  redirect: '/agent/knowledge',
  name: 'agent',
  meta: { title: '智能客服', icon: 'ums-admin' },
  children: [
    {
      path: 'service',
      name: 'agentService',
      component: () => import('@/views/agent/Service.vue'),
      meta: { title: '客服工作台' },
    },
    {
      path: 'knowledge',
      name: 'agentKnowledge',
      component: () => import('@/views/agent/Knowledge.vue'),
      meta: { title: '知识管理' },
    },
    {
      path: 'models',
      name: 'agentModels',
      component: () => import('@/views/agent/Models.vue'),
      meta: { title: '模型与提示词' },
    },
    {
      path: 'governance',
      name: 'agentGovernance',
      component: () => import('@/views/agent/Governance.vue'),
      meta: { title: '治理与任务' },
    },
    {
      path: 'operations',
      name: 'agentOperations',
      component: () => import('@/views/agent/Operations.vue'),
      meta: { title: '业务操作' },
    },
  ],
}
