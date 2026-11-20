async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  visitors: () => req('/api/visitors'),
  sessions: (visitorId) =>
    req(visitorId ? `/api/sessions?visitor_id=${encodeURIComponent(visitorId)}` : '/api/sessions'),
  session: (id) => req(`/api/sessions/${encodeURIComponent(id)}`),
  quarantine: () => req('/api/quarantine'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (sessionId) =>
    req(sessionId ? `/api/session-proofs?session_id=${encodeURIComponent(sessionId)}` : '/api/session-proofs'),
  settings: () => req('/api/settings'),
  burstConfig: () => req('/api/burst-dedupe/config'),
  burstProof: (visitorId) =>
    req(
      visitorId
        ? `/api/burst-dedupe/proof?visitor_id=${encodeURIComponent(visitorId)}`
        : '/api/burst-dedupe/proof',
    ),
  burstWindowProof: (batchId, visitorId) => {
    const q = new URLSearchParams()
    if (batchId) q.set('batch_id', batchId)
    if (visitorId) q.set('visitor_id', visitorId)
    const qs = q.toString()
    return req(`/api/burst-dedupe/window-proof${qs ? `?${qs}` : ''}`)
  },
}
