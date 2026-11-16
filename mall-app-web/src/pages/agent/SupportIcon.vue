<template>
  <svg
    class="support-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <template v-if="name === 'chat'">
      <path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a9 9 0 0 1 18 0Z" />
      <path d="M7 11h.01M12 11h.01M17 11h.01" />
    </template>
    <template v-else-if="name === 'headset'">
      <path d="M4 13v-2a8 8 0 0 1 16 0v2M4 12H3v6h4v-6H4ZM20 12h1v6h-4v-6h3ZM20 18c0 3-3 3-6 3" />
    </template>
    <template v-else-if="name === 'search'">
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 5 5" />
    </template>
    <template v-else-if="name === 'box'">
      <path d="m12 2 9 5v10l-9 5-9-5V7l9-5ZM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5" />
    </template>
    <template v-else-if="name === 'bag'">
      <path d="M5 7h14l1 14H4L5 7ZM9 8V6a3 3 0 0 1 6 0v2" />
    </template>
    <template v-else-if="name === 'image'">
      <rect x="3" y="3" width="18" height="18" rx="3" />
      <circle cx="8" cy="8" r="1.5" />
      <path d="m3 17 5-5 4 4 4-6 5 7" />
    </template>
    <template v-else-if="name === 'arrow'"><path d="M4 12h16m-6-6 6 6-6 6" /></template>
    <template v-else-if="name === 'back'"><path d="m14 5-7 7 7 7" /></template>
    <template v-else-if="name === 'close'"><path d="m6 6 12 12M6 18 18 6" /></template>
    <template v-else-if="name === 'plus'"><path d="M12 5v14M5 12h14" /></template>
    <template v-else-if="name === 'clock'">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </template>
    <template v-else-if="name === 'return'">
      <path d="M7 8h10a4 4 0 0 1 0 8h-5M7 8l4-4M7 8l4 4M4 17h4M6 15v4" />
    </template>
    <template v-else-if="name === 'shield'">
      <path d="m12 2 8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5l8-3Z" />
      <path d="m8 12 3 3 5-6" />
    </template>
    <template v-else-if="name === 'user'">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21v-2a8 8 0 0 1 16 0v2" />
    </template>
    <template v-else-if="name === 'shop'">
      <path d="M3 10h18l-2-7H5l-2 7ZM4 10v11h16V10M9 21v-7h6v7" />
      <path d="M3 10a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0" />
    </template>
  </svg>
</template>
<script setup lang="ts">
defineProps<{ name: string }>()
</script>
<style scoped>
.support-icon {
  display: inline-block;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  vertical-align: middle;
}
</style>
