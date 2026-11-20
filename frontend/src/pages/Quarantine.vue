<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')

function member(raw) {
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

const decorated = computed(() =>
  rows.value.map((r) => ({ ...r, member: member(r.member_json) })),
)

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>搁置区</h1>
    <p class="muted">
      脏击点 / 冲突组 / 突发窗内重复搁置落库后在此可见。
      突发去重记录 reason 为 burst_window_dup（去重引用与所属窗界证明见成员 JSON）。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!rows.length" class="muted">暂无搁置记录。</p>
    <table v-else class="data">
      <thead>
        <tr><th>ID</th><th>访客</th><th>原因</th><th>击点</th><th>窗界证明</th><th>成员</th><th>时刻</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in decorated" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td><span class="chip" :class="r.reason.startsWith('burst_window') ? 'held' : ''">{{ r.reason }}</span></td>
          <td class="mono">{{ r.member.hit_id || '—' }}</td>
          <td class="mono">{{ r.member.proof_run_id || '—' }}</td>
          <td class="mono">{{ r.member_json }}</td>
          <td class="mono">{{ r.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
