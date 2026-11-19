<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const err = ref('')
const busy = ref(false)

async function run() {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>收流击点</h1>
    <p class="muted">
      读取夹具 ingest_batch.json，按突发窗去重分流后灌入正式通道：
      窗内同键重复进搁置，不更新末击点、不改 hit_count。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '收流中…' : '跑一批收流' }}</button>
    <p v-if="err" class="err">{{ err }}</p>
    <pre v-if="result" class="mono">{{ result }}</pre>
  </section>
</template>
