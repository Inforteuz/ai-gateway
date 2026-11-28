import { translateText as tr, useLang as useLocaleRefresh, translate } from '../i18n/index.jsx'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { apiOrigin } from '../site.js'
import { CodeTabs, ErrorNote, Loading, Modal, PageHead, useLoad } from '../components/ui.jsx'
import {
  IconBook, IconBulb, IconChat, IconCheck, IconCode, IconCopy, IconDoc, IconGlobe, IconImage, IconLayers, IconPaperclip,
  IconPlus, IconRefresh, IconSend, IconSettings, IconSliders, IconSparkles, IconVideo, IconX,
} from '../components/icons.jsx'
import { Markdown } from '../components/Markdown.jsx'
import VideoStudio from '../components/VideoStudio.jsx'
import ImageStudio from '../components/ImageStudio.jsx'
import { fmtInt, fmtSom } from '../format.js'

/**
 * API playground: try a listed model from the panel, paid from the user's own
 * balance. The backend (routes_playground.py) authenticates by the session and a
 * hidden per-user key, so no API key ever reaches the browser. Each answer gets a
 * receipt strip (tokens, time, cost) once the usage row is written.
 */

const STORE_VERSION = 1
const DEFAULTS = { model: '', system: '', temperature: null, maxTokens: 1024, reasoning: '', stream: true }
const REASONING = [
  ['', 'Model odatiy'], ['none', 'O‘chiq'], ['low', 'Past'], ['medium', 'O‘rta'], ['high', 'Yuqori'],
]
// Starter prompts: an icon, a short title and the text that goes into the composer.
const STARTERS = [
  [IconCode, 'Kod yozish', 'Python’da fayldagi so‘zlarni sanaydigan funksiya yoz.'],
  [IconBook, 'Tushuntirish', 'REST va GraphQL farqini uch jumlada tushuntir.'],
  [IconGlobe, 'Tarjima', 'Shu matnni ingliz tiliga tarjima qil: “Ertaga uchrashamiz.”'],
  [IconBulb, 'G‘oyalar', 'Kichik onlayn do‘kon uchun beshta marketing g‘oyasini yoz.'],
]
// Text files the composer can attach; their text goes into the message (the playground takes text only).
const ATTACH_ACCEPT = '.txt,.md,.csv,.json,.py,.js,.jsx,.ts,.tsx,.html,.css,.xml,.yaml,.yml,.sql,.log,.ini,.toml,.sh,text/*'
const ATTACH_MAX = 30_000
const GATEWAY_NOTE = '[gateway]'          // the cluster's own "still waiting" text; not the model's
const reduceMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function load(key) {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || 'null')
    if (raw?.v === STORE_VERSION) return raw
  } catch { /* private window or bad JSON: start empty */ }
  return null
}

function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify({ v: STORE_VERSION, ...value })) } catch { /* storage full or blocked */ }
}

/** Reads a chat-completions SSE body; calls onDelta for each parsed data object. */
async function readStream(body, onDelta) {
  const reader = body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim()
      buf = buf.slice(i + 1)
      if (!line.startsWith('data:')) continue
      const data = line.slice(5).trim()
      if (data === '[DONE]') return
      try { onDelta(JSON.parse(data)) } catch { /* partial or foreign line */ }
    }
  }
}

/** Counts a number from `from` to `to` in ~600 ms (instant when motion is reduced). */
function useTicker(value) {
  const [shown, setShown] = useState(value)
  const prev = useRef(value)
  useEffect(() => {
    if (value == null || prev.current == null || reduceMotion()) { setShown(value); prev.current = value; return }
    const from = prev.current
    prev.current = value
    if (from === value) return
    const t0 = performance.now()
    let raf
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 600)
      setShown(Math.round(from + (value - from) * (1 - (1 - k) ** 3)))
      if (k < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [value])
  return shown
}

function Receipt({ r }) {
  useLocaleRefresh()
  const secs = (ms) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)
  return (
    <div className="pg-receipt print-in" aria-label={tr('Javob cheki')}>
      <span title={tr('Kiruvchi')}><i>{tr('Kiruvchi')}</i>{fmtInt(r.prompt_tokens)}</span>
      <span title={tr('Chiquvchi')}><i>{tr('Chiquvchi')}</i>{fmtInt(r.completion_tokens)}</span>
      <span><i>{tr('Birinchi so‘z')}</i>{secs(r.ttfb_ms)}</span>
      <span><i>{tr('Jami vaqt')}</i>{secs(r.latency_ms)}</span>
      <span className="pg-receipt__cost"><i>{tr('Narxi')}</i>{fmtSom(r.cost)}</span>
      {r.routed_model && r.routed_model !== r.model && (
        <span className="pg-receipt__note">{tr('Javob bergan model')}: {r.routed_model}</span>
      )}
    </div>
  )
}

function LiveMeter({ live }) {
  useLocaleRefresh()
  const [, setNow] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setNow((n) => n + 1), 200)
    return () => clearInterval(id)
  }, [])
  const elapsed = (performance.now() - live.t0) / 1000
  const out = Math.ceil(live.chars / 4)
  return (
    <div className="pg-meter" role="status" aria-live="off">
      <span className="pg-meter__dot" />
      {live.first == null ? tr('Model o‘ylayapti') : tr('Yozmoqda')}
      <b>{elapsed.toFixed(1)} s</b>
      {out > 0 && <span>≈ {fmtInt(out)} {tr('token')}</span>}
    </div>
  )
}

function CopyAction({ text }) {
  useLocaleRefresh()
  const [done, setDone] = useState(false)
  async function copy() {
    try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1400) } catch { /* clipboard blocked */ }
  }
  return (
    <button type="button" className="pg-act" onClick={copy} aria-label={tr(done ? 'Nusxa olindi' : 'Nusxa olish')} title={tr(done ? 'Nusxa olindi' : 'Nusxa olish')}>
      {done ? <IconCheck /> : <IconCopy />}<span>{tr(done ? 'Nusxa olindi' : 'Nusxa olish')}</span>
    </button>
  )
}

function Message({ m, onRegenerate }) {
  useLocaleRefresh()
  const [showThink, setShowThink] = useState(false)
  if (m.role === 'user') {
    return (
      <div className="pg-msg pg-msg--user">
        <div className="pg-bubble">
          {m.attachment && <span className="pg-file pg-file--sent"><IconPaperclip /> {m.attachment.name}</span>}
          {m.text ?? m.content}
        </div>
      </div>
    )
  }
  return (
    <div className={`pg-msg pg-msg--ai ${m.error ? 'is-error' : ''}`}>
      <div className="pg-msg__who"><span className="pg-avatar" aria-hidden="true"><IconSparkles /></span>{m.model}</div>
      <div className="pg-msg__body">
        {m.reasoning && (
          <div className="pg-think">
            <button type="button" className="pg-think__toggle" aria-expanded={showThink} onClick={() => setShowThink((v) => !v)}>
              {tr(showThink ? 'Fikrlashni yashirish' : 'Fikrlashni ko‘rish')}
            </button>
            {showThink && <div className="pg-think__body">{m.reasoning}</div>}
          </div>
        )}
        {m.content ? <div className="pg-answer"><Markdown>{m.content}</Markdown>{m.live && <span className="pg-caret" aria-hidden="true" />}</div>
          : m.live ? null : !m.error && <div className="dim">{tr('(bo‘sh javob)')}</div>}
        {m.live && <LiveMeter live={m.live} />}
        {m.stopped && <div className="pg-flag">{tr('To‘xtatildi')}</div>}
        {m.error && (
          <div className="pg-error" role="alert">
            {m.error}
            {m.status === 402 && <> <Link to="/app/billing">{tr('Hisobni to‘ldirish')}</Link></>}
          </div>
        )}
        {!m.live && (m.content || onRegenerate) && (
          <div className="pg-actions">
            {m.content && <CopyAction text={m.content} />}
            {onRegenerate && (
              <button type="button" className="pg-act" onClick={onRegenerate} title={tr('Qayta yaratish')}>
                <IconRefresh /><span>{tr('Qayta yaratish')}</span>
              </button>
            )}
          </div>
        )}
        {m.receipt && <Receipt r={m.receipt} />}
      </div>
    </div>
  )
}

/** Capability badges for the model card in the settings. */
function capBadges(m) {
  const c = m?.capabilities || {}
  const out = []
  if (!m?.price_in_per_1m && !m?.price_out_per_1m) out.push(['ok', 'Bepul'])
  if (c.vision) out.push(['sky', 'Rasmni ko\'radi'])
  if (c.file_input) out.push(['sky', 'Fayl o\'qiydi'])
  if (c.tool_calling) out.push(['violet', 'Agent rejimi'])
  if (c.reasoning_levels?.length) out.push(['amber', 'Fikrlash darajasi'])
  if ((m?.context_window || 0) >= 100_000) out.push(['', 'Uzun kontekst'])
  return out
}

function codeTabs(origin, s, messages) {
  const body = { model: s.model, messages, max_tokens: s.maxTokens }
  if (s.temperature != null) body.temperature = s.temperature
  if (s.reasoning) body.reasoning_effort = s.reasoning
  const json = JSON.stringify(body, null, 2)
  return [
    { name: 'curl', code: `curl ${origin}/v1/chat/completions \\\n  -H "Authorization: Bearer $AI_GATEWAY_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json.replace(/'/g, "'\\''")}'` },
    { name: 'Python', code: `import os\nfrom openai import OpenAI\n\nclient = OpenAI(base_url="${origin}/v1", api_key=os.environ["AI_GATEWAY_KEY"])\nresp = client.chat.completions.create(**${json.replace(/\bnull\b/g, 'None').replace(/\btrue\b/g, 'True').replace(/\bfalse\b/g, 'False')})\nprint(resp.choices[0].message.content)` },
    { name: 'JavaScript', code: `import OpenAI from "openai"\n\nconst client = new OpenAI({ baseURL: "${origin}/v1", apiKey: process.env.AI_GATEWAY_KEY })\nconst resp = await client.chat.completions.create(${json})\nconsole.log(resp.choices[0].message.content)` },
  ]
}

export default function Playground() {
  useLocaleRefresh()
  const { user } = useAuth()
  const site = useSite()
  const storeKey = `pg:${user?.id || 'anon'}`
  const saved = useMemo(() => load(storeKey), [storeKey])
  const [s, setS] = useState({ ...DEFAULTS, ...(saved?.settings || {}) })
  const [msgs, setMsgs] = useState(() => (saved?.messages || []).filter((m) => !m.live))
  const [draft, setDraft] = useState('')
  const [attachment, setAttachment] = useState(null)   // { name, text } — a text file folded into the next message
  const [attachError, setAttachError] = useState('')
  const fileRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [showCode, setShowCode] = useState(false)
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth > 900)
  const abortRef = useRef(null)
  const listRef = useRef(null)
  const inputRef = useRef(null)
  const models = useLoad(() => api.get('/portal/models'), [])
  const summary = useLoad(() => api.get('/portal/account/summary'), [])
  const [balance, setBalance] = useState(null)
  const shownBalance = useTicker(balance)

  const all = (models.data?.items || []).filter((m) => !m.maintenance)
  // Video models take a description, not a chat: they live in the Video tab
  const items = all.filter((m) => !m.capabilities?.video_generation && !m.capabilities?.image_only)
  const videoItems = all.filter((m) => m.capabilities?.video_generation)
  const imageItems = all.filter((m) => m.capabilities?.image_generation)
  const [mode, setMode] = useState('text')
  useEffect(() => { if (summary.data) setBalance(summary.data.balance) }, [summary.data])
  useEffect(() => {
    if (items.length && !items.some((m) => m.id === s.model)) setS((v) => ({ ...v, model: items[0].id }))
  }, [items.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { save(storeKey, { settings: s, messages: msgs.filter((m) => !m.live) }) }, [s, msgs, storeKey])
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: reduceMotion() ? 'auto' : 'smooth' })
  }, [msgs.length, msgs[msgs.length - 1]?.content?.length])
  useEffect(() => () => abortRef.current?.abort(), [])

  const current = items.find((m) => m.id === s.model)
  const set = (k) => (e) => setS((v) => ({ ...v, [k]: e?.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e }))
  const history = (list) => [
    ...(s.system.trim() ? [{ role: 'system', content: s.system.trim() }] : []),
    ...list.filter((m) => !m.error && m.content).map((m) => ({ role: m.role, content: m.content })),
  ]

  function patchLast(fn) {
    setMsgs((list) => list.map((m, i) => (i === list.length - 1 ? { ...m, ...fn(m) } : m)))
  }

  async function fetchReceipt(rid) {
    for (let i = 0; i < 6; i++) {
      try {
        const r = await api.get(`/portal/playground/receipt/${rid}`)
        if (r.ready) { setBalance(r.balance); return r }
      } catch { return null }
      await new Promise((ok) => setTimeout(ok, 400 + i * 300))
    }
    return null
  }

  function pickFile(e) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    const reader = new FileReader()
    reader.onload = () => {
      const text = String(reader.result || '')
      if (text.includes('\u0000')) { setAttachError(tr('Faqat matnli fayl biriktiriladi.')); return }
      if (text.length > ATTACH_MAX) { setAttachError(tr('Fayl juda katta: {n} belgidan oshmasin.', { n: fmtInt(ATTACH_MAX) })); return }
      setAttachment({ name: f.name, text })
      setAttachError('')
    }
    reader.onerror = () => setAttachError(tr('Faylni o‘qib bo‘lmadi.'))
    reader.readAsText(f)
  }

  async function send(e) {
    e?.preventDefault()
    const text = draft.trim()
    if (!text || busy || !s.model) return
    const content = attachment ? `${text}\n\n\`\`\`${attachment.name}\n${attachment.text}\n\`\`\`` : text
    const userMsg = { role: 'user', content, id: crypto.randomUUID?.() || String(Date.now()),
      ...(attachment ? { text, attachment: { name: attachment.name } } : {}) }
    setDraft('')
    setAttachment(null)
    await run([...msgs, userMsg])
  }

  /** Asks again for the last user message, dropping the answer after it. */
  function regenerate() {
    if (busy) return
    let i = msgs.length - 1
    while (i >= 0 && msgs[i].role !== 'user') i--
    if (i < 0) return
    run(msgs.slice(0, i + 1))
  }

  async function run(before) {
    const last = before[before.length - 1]
    const live = { t0: performance.now(), first: null, chars: 0 }
    setMsgs([...before, { role: 'assistant', model: current?.display_name || s.model, content: '', reasoning: '', live, id: `${last.id}-a${Date.now()}` }])
    setBusy(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const body = { model: s.model, messages: history(before), max_tokens: Number(s.maxTokens) || undefined, stream: s.stream }
    if (s.temperature != null) body.temperature = Number(s.temperature)
    if (s.reasoning) body.reasoning_effort = s.reasoning
    let rid = null
    try {
      const res = await fetch('/portal/playground/chat', {
        method: 'POST', signal: ctrl.signal, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: s.stream ? 'text/event-stream' : 'application/json', 'X-SF-CSRF': '1' },
        body: JSON.stringify(body),
      })
      rid = res.headers.get('X-Request-Id')
      if (!res.ok) {
        let data = null
        try { data = await res.json() } catch { /* empty */ }
        const err = new ApiError(res.status, data?.error?.code || 'error', data?.error?.message || translate('api.status', { status: res.status }))
        patchLast(() => ({ live: null, error: err.message, status: res.status }))
        return
      }
      if (!s.stream) {
        const data = await res.json()
        const msg = data.choices?.[0]?.message || {}
        patchLast(() => ({ content: msg.content || '', reasoning: msg.reasoning_content || msg.reasoning || '', live: null }))
      } else {
        let content = ''
        let reasoning = ''
        let failure = null
        await readStream(res.body, (obj) => {
          if (obj.error) { failure = obj.error.message || obj.error.code; return }
          const d = obj.choices?.[0]?.delta || {}
          const think = d.reasoning_content || d.reasoning || ''
          if (think && !think.startsWith(GATEWAY_NOTE)) reasoning += think
          if (d.content) content += d.content
          if (d.content || think) {
            live.chars = content.length
            if (live.first == null && d.content) live.first = performance.now()
            patchLast(() => ({ content, reasoning, live: { ...live } }))
          }
        })
        patchLast(() => ({ content, reasoning, live: null, ...(failure ? { error: tr(failure) } : {}) }))
      }
    } catch (err) {
      if (err.name === 'AbortError') patchLast(() => ({ live: null, stopped: true }))
      else patchLast(() => ({ live: null, error: translate('api.network') }))
    } finally {
      abortRef.current = null
      setBusy(false)
      inputRef.current?.focus()
    }
    if (rid) {
      const receipt = await fetchReceipt(rid)
      if (receipt) patchLast(() => ({ receipt }))
    }
  }

  function stop() { abortRef.current?.abort() }
  function reset() {
    stop()
    setMsgs([])
    inputRef.current?.focus()
  }
  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) send(e)
  }

  const low = balance != null && balance <= 0 && (current?.price_in_per_1m || 0) > 0
  const spent = msgs.reduce((o, m) => (m.receipt
    ? { tokens: o.tokens + (m.receipt.prompt_tokens || 0) + (m.receipt.completion_tokens || 0), cost: o.cost + (m.receipt.cost || 0) }
    : o), { tokens: 0, cost: 0 })
  const lastAi = msgs.length - 1
  const tempOn = s.temperature != null

  return (
    <div className="page pg">
      <PageHead
        title={tr('Playground')}
        lead={tr('Modellarni API’ga ulanmasdan shu yerda sinab ko‘ring. Har bir javob hisobingizdan yechiladi va chekda ko‘rinadi.')}
        action={<div className="pg-balance" title={tr('Hisobdagi mablag‘')}>
          <span>{tr('Hisob')}</span><b>{shownBalance == null ? '—' : fmtSom(shownBalance)}</b>
        </div>}
      />
      {models.data && (videoItems.length > 0 || imageItems.length > 0) && (
        <div className="pg-modes" role="tablist" aria-label={tr('Playground turi')}>
          {[['text', 'Matn', IconChat], ...(imageItems.length ? [['image', 'Rasm', IconImage]] : []), ...(videoItems.length ? [['video', 'Video', IconVideo]] : [])].map(([v, label, Icon]) => (
            <button key={v} type="button" role="tab" aria-selected={mode === v} className={mode === v ? 'is-on' : ''}
              onClick={() => setMode(v)}><Icon /> {tr(label)}</button>
          ))}
        </div>
      )}
      {models.error ? <ErrorNote error={models.error} retry={models.reload} /> : !models.data ? <Loading rows={4} /> : mode === 'video' ? (
        <VideoStudio models={videoItems} userId={user?.id}
          onCharged={() => api.get('/portal/account/summary').then((r) => setBalance(r.balance)).catch(() => {})} />
      ) : mode === 'image' ? (
        <ImageStudio models={imageItems} userId={user?.id}
          onCharged={() => api.get('/portal/account/summary').then((r) => setBalance(r.balance)).catch(() => {})} />
      ) : (
        <div className="pg-grid">
          <aside className="pg-bench">
            <details className="pg-bench__fold" open={wide}>
              <summary><span className="pg-tile" aria-hidden="true"><IconSliders /></span>{tr('Sozlamalar')}</summary>

              <div className="pg-sec">
                <div className="pg-sec__t"><IconLayers /> {tr('Model')}</div>
                <select className="input" value={s.model} onChange={set('model')} disabled={busy} aria-label={tr('Model')}>
                  {items.map((m) => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}
                </select>
                {current && (
                  <div className="pg-model">
                    <div className="pg-model__badges">
                      {capBadges(current).map(([tone, label]) => <span key={label} className={`tag ${tone ? `tag--${tone}` : ''}`}>{tr(label)}</span>)}
                    </div>
                    <div className="pg-model__price">
                      <div><span>{tr('Kiruvchi tokenlar')}</span><b>{fmtSom(current.price_in_per_1m)}</b></div>
                      <div><span>{tr('Chiquvchi tokenlar')}</span><b>{fmtSom(current.price_out_per_1m)}</b></div>
                    </div>
                    <small className="pg-model__unit">{tr('Narx (1M token uchun)')}</small>
                  </div>
                )}
              </div>

              <div className="pg-sec">
                <label className="pg-sec__t" htmlFor="pg-system"><IconDoc /> {tr('Tizim ko‘rsatmasi')}</label>
                <textarea id="pg-system" className="input" rows={3} value={s.system} maxLength={8000} onChange={set('system')}
                  placeholder={tr('Masalan: qisqa va o‘zbek tilida javob ber.')} />
              </div>

              <div className="pg-sec">
                <div className="pg-sec__t"><IconSliders /> {tr('Parametrlar')}</div>
                <div className="pg-field">
                  <span className="pg-field__row">
                    {tr('Harorat')}
                    <label className="pg-switch">
                      <input type="checkbox" role="switch" checked={!tempOn}
                        onChange={(e) => setS((v) => ({ ...v, temperature: e.target.checked ? null : 0.7 }))} />
                      <span aria-hidden="true" />{tr('odatiy')}
                    </label>
                  </span>
                  <div className="pg-range">
                    <input type="range" min="0" max="2" step="0.1" value={s.temperature ?? 0.7} disabled={!tempOn}
                      style={{ '--p': `${((s.temperature ?? 0.7) / 2) * 100}%` }}
                      onChange={(e) => setS((v) => ({ ...v, temperature: Number(e.target.value) }))} aria-label={tr('Harorat')} />
                    <output>{tempOn ? Number(s.temperature).toFixed(1) : '—'}</output>
                  </div>
                </div>
                <div className="pg-field">
                  <span className="pg-field__row">{tr('Javob uzunligi (token)')}</span>
                  <div className="pg-range pg-range--num">
                    <input type="range" min="64" max="4096" step="64" value={Math.min(4096, Math.max(64, s.maxTokens))}
                      style={{ '--p': `${((Math.min(4096, Math.max(64, s.maxTokens)) - 64) / (4096 - 64)) * 100}%` }}
                      onChange={(e) => setS((v) => ({ ...v, maxTokens: Number(e.target.value) }))} aria-label={tr('Javob uzunligi (token)')} />
                    <input className="input" type="number" min="1" max="4096" value={s.maxTokens} aria-label={tr('Javob uzunligi (token)')}
                      onChange={(e) => setS((v) => ({ ...v, maxTokens: Math.max(1, Math.min(4096, Number(e.target.value) || 1)) }))} />
                  </div>
                </div>
              </div>

              <details className="pg-more">
                <summary><IconSettings /> {tr('Qo‘shimcha sozlamalar')}</summary>
                <label className="pg-field">
                  <span>{tr('Fikrlash darajasi')}</span>
                  <select className="input" value={s.reasoning} onChange={set('reasoning')}>
                    {REASONING.map(([v, label]) => <option key={v} value={v}>{tr(label)}</option>)}
                  </select>
                </label>
                <label className="pg-switch pg-switch--line">
                  <input type="checkbox" role="switch" checked={s.stream} onChange={set('stream')} />
                  <span aria-hidden="true" />{tr('Javobni oqim bilan ko‘rsatish')}
                </label>
              </details>

              <button type="button" className="btn btn--small pg-code-btn" onClick={() => setShowCode(true)} disabled={!s.model}>
                <IconCode /> {tr('Kod sifatida olish')}
              </button>
            </details>
          </aside>

          <section className="pg-chat" aria-label={tr('Suhbat')}>
            <div className="pg-chat__bar">
              <span className="pg-tile pg-tile--accent" aria-hidden="true"><IconSparkles /></span>
              <div className="pg-chat__title">
                <span className="pg-chat__model">{current?.display_name || s.model}</span>
                <span className={`pg-status ${busy ? 'is-busy' : ''}`}><i aria-hidden="true" />{tr(busy ? 'Javob yozilmoqda' : 'Tayyor')}</span>
              </div>
              <div className="pg-chat__meter" aria-label={tr('Shu suhbat')}>
                <span><i>{tr('token')}</i><b>{fmtInt(spent.tokens)}</b></span>
                <span><i>{tr('Narxi')}</i><b>{fmtSom(spent.cost)}</b></span>
              </div>
              <button type="button" className="btn btn--small btn--ghost" onClick={reset} disabled={!msgs.length && !busy}>
                <IconPlus /> <span className="pg-hide-sm">{tr('Yangi suhbat')}</span>
              </button>
            </div>
            <div className="pg-list" ref={listRef}>
              {msgs.length === 0 ? (
                <div className="pg-empty">
                  <span className="pg-hero" aria-hidden="true"><IconChat /></span>
                  <h3>{tr('Nima haqida so‘raymiz?')}</h3>
                  <p>{tr('Birinchi savolingizni yozing. Javob, tokenlar va narx shu yerda chiqadi.')}</p>
                  <div className="pg-starters">
                    {STARTERS.map(([Icon, title, q]) => (
                      <button key={q} type="button" className="pg-starter" onClick={() => { setDraft(tr(q)); inputRef.current?.focus() }}>
                        <span className="pg-starter__ico" aria-hidden="true"><Icon /></span>
                        <span><b>{tr(title)}</b><span>{tr(q)}</span></span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : msgs.map((m, i) => (
                <Message key={m.id} m={m} onRegenerate={i === lastAi && m.role === 'assistant' && !busy ? regenerate : null} />
              ))}
            </div>
            <form className="pg-composer" onSubmit={send}>
              {low && <div className="pg-low">{tr('Hisobingizda mablag‘ yo‘q.')} <Link to="/app/billing">{tr('Hisobni to‘ldirish')}</Link></div>}
              <div className="pg-composer__box">
                {(attachment || attachError) && (
                  <div className="pg-composer__files">
                    {attachment && (
                      <span className="pg-file">
                        <IconPaperclip /> {attachment.name}
                        <button type="button" aria-label={tr('Olib tashlash')} onClick={() => setAttachment(null)}><IconX /></button>
                      </span>
                    )}
                    {attachError && <span className="pg-error" role="alert">{attachError}</span>}
                  </div>
                )}
                <textarea ref={inputRef} rows={2} value={draft} maxLength={32000} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey}
                  placeholder={tr('Xabar yozing…')} aria-label={tr('Xabar')} />
                <div className="pg-composer__foot">
                  <button type="button" className="pg-tool" onClick={() => fileRef.current?.click()} disabled={busy}
                    title={tr('Matnli fayl biriktirish')} aria-label={tr('Matnli fayl biriktirish')}>
                    <IconPaperclip />
                  </button>
                  <input ref={fileRef} type="file" accept={ATTACH_ACCEPT} hidden onChange={pickFile} />
                  <label className="pg-chip-select" title={tr('Model')}>
                    <IconLayers />
                    <select value={s.model} onChange={set('model')} disabled={busy} aria-label={tr('Model')}>
                      {items.map((m) => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}
                    </select>
                  </label>
                  <small className="dim pg-hide-sm">{tr('Enter — yuborish, Shift+Enter — yangi qator')}</small>
                  {busy
                    ? <button type="button" className="btn btn--small pg-send" onClick={stop}>{tr('To‘xtatish')}</button>
                    : <button type="submit" className="btn btn--primary btn--small pg-send" disabled={!draft.trim() || !s.model}><IconSend /> {tr('Yuborish')}</button>}
                </div>
              </div>
            </form>
          </section>
        </div>
      )}
      {showCode && (
        <Modal title={tr('Shu so‘rov kod sifatida')} onClose={() => setShowCode(false)} wide>
          <p className="dim">{tr('Kalitingizni AI_GATEWAY_KEY muhit o‘zgaruvchisiga yozing. Playground kaliti kodda ishlamaydi.')}</p>
          <CodeTabs tabs={codeTabs(apiOrigin(site), s, history([...msgs, ...(draft.trim() ? [{ role: 'user', content: draft.trim() }] : [])]).slice(-6))} />
        </Modal>
      )}
    </div>
  )
}
