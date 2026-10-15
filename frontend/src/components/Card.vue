<script setup>
import { ref } from 'vue'

// 可折叠板块：标题整行可点击，点击展开/收起正文。
// nested=true 时只作为卡片内部的分区（外层卡片边框由页面保留），
// 用于一个 <section class="card"> 里含多个 <h2> 板块的表单页。
const props = defineProps({
  title: { type: String, default: '' },
  nested: { type: Boolean, default: false },
  open: { type: Boolean, default: true },
})

const expanded = ref(props.open)
</script>

<template>
  <section class="card" :class="{ nested, collapsed: !expanded }">
    <h2 class="card-head">
      <button
        type="button"
        class="card-toggle"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="chev" :class="{ open: expanded }" aria-hidden="true"></span>{{ title }}
      </button>
    </h2>
    <div v-show="expanded" class="card-body"><slot /></div>
  </section>
</template>
