import { translateText as tr, useLang as useLocaleRefresh, modelDescription } from '../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api.js'
import { fmtSom } from '../format.js'

/**
 * Playground image tab: POST /portal/playground/images (the same pipeline and billing as
 * /v1/images/generations, through the session and the hidden playground key). Results are
 * kept in memory only — base64 images are too big for localStorage.
 */
function Elapsed({ since }) {
  const [, tick] = useState(0)
  useEffect(() => { const id = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(id) }, [])
  return <b>{Math.max(0, Math.round((Date.now() - since) / 1000))} s</b>
}

export default function ImageStudio({ models, onCharged }) {
  useLocaleRefresh()
  const [model, setModel] = useState(models[0]?.id || '')
  const [prompt, setPrompt] = useState('')
  const [size, setSize] = useState('1024x1024')
  const [items, setItems] = useState([])     // { id, prompt, model, status, src?, error?, cost?, started }
  const busy = items.some((i) => i.status === 'running')
  const current = models.find((m) => m.id === model) || models[0]

  useEffect(() => { if (!models.some((m) => m.id === model) && models[0]) setModel(models[0].id) }, [models, model])

  async function create(e) {
    e.preventDefault()
    const text = prompt.trim()
    if (!text || busy || !current) return
    const id = crypto.randomUUID?.() || String(Date.now())
    setItems((list) => [{ id, prompt: text, model: current.display_name || current.id, status: 'running', started: Date.now() }, ...list])
    setPrompt('')
    const patch = (p) => setItems((list) => list.map((i) => (i.id === id ? { ...i, ...p } : i)))
    try {
      const res = await fetch('/portal/playground/images', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-SF-CSRF': '1' },
        body: JSON.stringify({ model: current.id, prompt: text, size }),
      })
      const data = await res.json().catch(() => null)
      if (!res.ok) {
        const err = new ApiError(res.status, data?.error?.code || 'error', data?.error?.message || `HTTP ${res.status}`)
        patch({ status: 'failed', error: err.message, code: res.status })
        return
      }
      const img = data?.data?.[0]
      patch({ status: 'done', src: img ? `data:${img.mime_type || 'image/png'};base64,${img.b64_json}` : null })
      const rid = res.headers.get('X-Request-Id')
      if (rid) {
        for (let k = 0; k < 6; k++) {
          const r = await api.get(`/portal/playground/receipt/${rid}`).catch(() => null)
          if (r?.ready) { patch({ cost: r.cost }); break }
          await new Promise((ok) => setTimeout(ok, 500 + k * 300))
        }
      }
      onCharged?.()
    } catch {
      patch({ status: 'failed', error: tr('Tarmoq xatosi. Qayta urinib ko‘ring.') })
    }
  }

  if (!models.length) return <div className="vs-empty">{tr('Hozircha rasm modeli yo‘q.')}</div>
  return (
    <div className="vs">
      <form className="vs-form" onSubmit={create}>
        <label className="pg-field">
          <span>{tr('Model')}</span>
          <select className="input" value={current?.id} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}
          </select>
          {current && <small className="vs-hint">{modelDescription(current)}</small>}
        </label>
        <label className="pg-field">
          <span>{tr('Tasvir')}</span>
          <textarea className="input" rows={5} maxLength={4000} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={tr('Masalan: tog‘ etagidagi yog‘och uy, tong tumanida, realistik surat.')} />
        </label>
        <div className="pg-field">
          <span>{tr('Format')}</span>
          <div className="vs-seg" role="radiogroup" aria-label={tr('Format')}>
            {[['1024x1024', '1:1'], ['1536x1024', '3:2'], ['1024x1536', '2:3']].map(([v, label]) => (
              <button key={v} type="button" role="radio" aria-checked={size === v} className={size === v ? 'is-on' : ''}
                onClick={() => setSize(v)}>{label}</button>
            ))}
          </div>
        </div>
        <button type="submit" className="btn btn--primary" disabled={busy || !prompt.trim()}>
          {busy ? tr('Chizilmoqda…') : tr('Rasm yaratish')}
        </button>
        <small className="dim">{tr('Rasm odatda 15–30 soniyada tayyor bo‘ladi. Narx chekda ko‘rinadi; rasm chiqmasa pul yechilmaydi.')}</small>
      </form>
      <section className="vs-list" aria-label={tr('Rasmlar')}>
        {items.length === 0
          ? <div className="vs-empty">{tr('Tasvir yozing — tayyor rasm shu yerda paydo bo‘ladi.')}</div>
          : items.map((i) => (
            <article key={i.id} className={`vs-card vs-card--${i.status}`}>
              {i.status === 'done' && i.src
                ? <img className="is-image" src={i.src} alt={i.prompt} />
                : <div className="vs-stage is-square">
                    {i.status === 'failed'
                      ? <p className="vs-error" role="alert">{i.error} {tr('Pul yechilmadi.')}
                          {i.code === 402 && <> <Link to="/app/billing">{tr('Hisobni to‘ldirish')}</Link></>}</p>
                      : <div className="vs-rendering" role="status"><span className="vs-bar" aria-hidden="true" />
                          <span>{tr('Chizilmoqda')} <Elapsed since={i.started} /></span></div>}
                  </div>}
              <div className="vs-meta">
                <p className="vs-prompt">{i.prompt}</p>
                <div className="vs-row">
                  <span>{i.model}{i.cost != null && <> · {fmtSom(i.cost)}</>}</span>
                  {i.src && <a href={i.src} download={`image-${i.id.slice(0, 8)}.png`}>{tr('Yuklab olish')}</a>}
                </div>
              </div>
            </article>
          ))}
      </section>
    </div>
  )
}
