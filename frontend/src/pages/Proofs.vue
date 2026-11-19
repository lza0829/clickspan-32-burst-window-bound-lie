<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const proof = ref(null)
const err = ref('')
const visitor = ref('')

async function load() {
  err.value = ''
  try {
    proof.value = await api.burstProof(visitor.value.trim() || undefined)
  } catch (e) {
    err.value = String(e.message || e)
  }
}

const groups = computed(() =>
  (proof.value?.groups || []).filter(
    (g) => g.held_count || g.formal_count,
  ),
)

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>突发去重核对</h1>
    <p class="muted">
      只读。窗秒取自 settings.burst_window_sec；正式集 / 搁置重复集 / 会话末击点时钟 /
      hit_count 四者对照。
    </p>

    <p class="row">
      <input
        v-model="visitor"
        placeholder="访客 ID（可空）"
        class="mono"
        @keyup.enter="load"
      />
      <button class="btn" @click="load">核对</button>
    </p>
    <p v-if="err" class="err">{{ err }}</p>

    <template v-if="proof">
      <div class="stats">
        <div class="stat"><b>{{ proof.burst_window_sec }}s</b><span>突发窗</span></div>
        <div class="stat"><b>{{ proof.summary.formal_hits }}</b><span>正式</span></div>
        <div class="stat"><b>{{ proof.summary.held_hits }}</b><span>搁置</span></div>
        <div class="stat"><b>{{ proof.summary.duplicates_within_window }}</b><span>窗内去重</span></div>
        <div class="stat"><b>{{ proof.summary.anomalies }}</b><span>异常</span></div>
        <div class="stat">
          <b>{{ proof.ok ? '通过' : '异常' }}</b><span>核对结论</span>
        </div>
      </div>
      <p class="muted mono">
        窗配置形态 {{ proof.window_config_form }} · 分流形态 {{ proof.approach }} · 只读
      </p>

      <h2>同键组</h2>
      <p v-if="!groups.length" class="muted">无记录。</p>
      <table v-else class="data">
        <thead>
          <tr>
            <th>访客</th><th>page_path</th><th>event_name</th>
            <th>正式</th><th>搁置</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in groups" :key="`${g.visitor_id}|${g.page_path}|${g.event_name}`">
            <td>{{ g.visitor_id }}</td>
            <td class="mono">{{ g.page_path }}</td>
            <td class="mono">{{ g.event_name }}</td>
            <td class="mono">
              <span v-for="h in g.formal" :key="h.id" class="chip formal">{{ h.id }} · {{ h.event_ts }}</span>
            </td>
            <td class="mono">
              <span v-for="h in g.held" :key="h.quarantine_id" class="chip held">
                {{ h.hit_id }} → {{ h.dedupe_of }} ({{ h.delta_sec }}s)
              </span>
            </td>
            <td :class="g.ok ? 'ok' : 'err'">{{ g.ok ? '一致' : '异常' }}</td>
          </tr>
        </tbody>
      </table>

      <h2>会话末击点时钟</h2>
      <table class="data">
        <thead>
          <tr>
            <th>会话</th><th>访客</th><th>hit_count</th><th>正式点数</th>
            <th>ended_at</th><th>末击点</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in proof.session_clocks" :key="s.session_id">
            <td class="mono">{{ s.session_id }}</td>
            <td>{{ s.visitor_id }}</td>
            <td>{{ s.hit_count }}</td>
            <td>{{ s.primary_hits }}</td>
            <td class="mono">{{ s.ended_at || '…' }}</td>
            <td class="mono">{{ s.last_hit_ts || '…' }}</td>
            <td :class="s.ok ? 'ok' : 'err'">{{ s.ok ? '一致' : '异常' }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="proof.anomalies.length">
        <h2>异常明细</h2>
        <table class="data">
          <thead>
            <tr><th>代码</th><th>说明</th><th>明细</th></tr>
          </thead>
          <tbody>
            <tr v-for="(a, i) in proof.anomalies" :key="i">
              <td class="mono">{{ a.code }}</td>
              <td>{{ a.message }}</td>
              <td class="mono">{{ a }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </template>
  </section>
</template>
