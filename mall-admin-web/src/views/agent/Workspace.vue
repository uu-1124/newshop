<script setup lang="ts">
import { computed } from 'vue'
import { workspaces, type WorkspaceSection } from './workspaces'

const props = defineProps<{ section: WorkspaceSection }>()
const workspace = computed(() => workspaces[props.section])
</script>

<template>
  <main class="agent-workspace">
    <header>
      <div>
        <h1>{{ workspace.title }}</h1>
        <p>{{ workspace.description }}</p>
      </div>
      <el-tag type="info">暂未开放</el-tag>
    </header>
    <el-card shadow="never">
      <div class="sections" aria-label="工作区范围">
        <span v-for="sectionName in workspace.sections" :key="sectionName">{{ sectionName }}</span>
      </div>
      <el-empty :description="workspace.unavailable">
        <p class="explanation">{{ workspace.explanation }}</p>
      </el-empty>
    </el-card>
  </main>
</template>

<style scoped>
.agent-workspace { padding: 28px; max-width: 1400px; margin: auto; }
header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 24px; }
h1 { margin: 0 0 12px; font-size: 24px; font-weight: 600; color: #202c3b; }
header p, .explanation { color: #697586; line-height: 1.8; }
.sections { display: flex; flex-wrap: wrap; gap: 28px; padding: 0 0 20px; border-bottom: 1px solid #edf0f3; color: #697586; }
.explanation { margin: 0 16px; text-align: center; max-width: 560px; }
@media (max-width: 720px) { .agent-workspace { padding: 16px; } header { align-items: flex-start; } }
</style>
