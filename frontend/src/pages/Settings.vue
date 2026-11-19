<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const sec = ref(1800)
const burstSec = ref(60)
const msg = ref('')
const burstMsg = ref('')
const err = ref('')

onMounted(async () => {
  try {
    const s = await api.settings()
    sec.value = s.idle_timeout_sec
    burstSec.value = s.burst_window_sec
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function save() {
  err.value = ''
  msg.value = ''
  try {
    const res = await fetch('/api/settings/idle-timeout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idle_timeout_sec: Number(sec.value) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const data = await res.json()
    sec.value = data.idle_timeout_sec
    msg.value = '已保存（基座装载尚未按此时切开）'
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveBurst() {
  err.value = ''
  burstMsg.value = ''
  try {
    const res = await fetch('/api/settings/burst-window', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ burst_window_sec: Number(burstSec.value) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const data = await res.json()
    burstSec.value = data.burst_window_sec
    burstMsg.value = '已落库（settings 表 burst_window_sec），下一批收流生效'
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>

<template>
  <section class="panel">
    <h1>切窗</h1>
    <p class="muted">配置写入 settings 表；真正按间隙切会话由功能题空桩实现。</p>
    <label>
      空闲超时秒数
      <input v-model.number="sec" type="number" min="60" step="60" class="mono" />
    </label>
    <p>
      <button class="btn" @click="save">保存空闲超时</button>
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <hr />

    <h2>突发去重窗</h2>
    <p class="muted">
      同一访客同一 page_path + event_name 在窗内连点，只留首条进正式，其余进搁置可查。
    </p>
    <label>
      突发窗秒数
      <input v-model.number="burstSec" type="number" min="1" max="86400" class="mono" />
    </label>
    <p>
      <button class="btn" @click="saveBurst">保存突发窗</button>
    </p>
    <p v-if="burstMsg" class="ok">{{ burstMsg }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </section>
</template>
