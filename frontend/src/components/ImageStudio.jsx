import { translateText as tr, useLang as useLocaleRefresh, modelDescription } from '../i18n/index.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api.js'
import { fmtSom } from '../format.js'

/**
 * Playground image tab: POST /portal/playground/images (the same pipeline and billing as
 * /v1/images/generations, through the session and the hidden playground key). With an uploaded
 * picture the request becomes an edit (/v1/images/edits, img2img). Results are kept in memory
 * only — base64 images are too big for localStorage.
 */
const MAX_IMAGE = 10 * 1024 * 1024
// One picture is billed as 1000 output tokens (the cluster's image usage)
const IMAGE_TOKENS = 1000
const FORMATS = [
  ['1024x1024', '1:1'], ['1536x1024', '3:2'], ['1024x1536', '2:3'], ['1792x1024', '16:9'],
  ['1024x1792', '9:16'], ['1536x1152', '4:3'], ['1152x1536', '3:4'],
]

function Elapsed({ since }) {
  const [, tick] = useState(0)
  useEffect(() => { const id = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(id) }, [])
  return <b>{Math.max(0, Math.round((Date.now() - since) / 1000))} s</b>
}

const resolution = (m) => m?.capabilities?.image_resolution || ''
const canEdit = (m) => Boolean(m?.capabilities?.image_editing)
const imagePrice = (m) => (m ? (m.price_out_per_1m * IMAGE_TOKENS) / 1_000_000 : 0)

export default function ImageStudio({ models, onCharged }) {
  useLocaleRefresh()
  const [model, setModel] = useState(models[0]?.id || '')
  const [prompt, setPrompt] = useState('')
  const [size, setSize] = useState('1024x1024')
  const [source, setSource] = useState(null)      // { dataUrl, name } — the picture to change (img2img)
  const [error, setError] = useState('')
  const [items, setItems] = useState([])     // { id, prompt, model, status, src?, error?, cost?, started, edited }
  const fileRef = useRef(null)
  const busy = items.some((i) => i.status === 'running')
  const current = models.find((m) => m.id === model) || models[0]
  const editing = Boolean(source) && canEdit(current)

  useEffect(() => { if (!models.some((m) => m.id === model) && models[0]) setModel(models[0].id) }, [models, model])

  function pickImage(e) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { setError(tr('Faqat PNG, JPEG yoki WebP rasm.')); return }
    if (f.size > MAX_IMAGE) { setError(tr('Rasm 10 MB dan oshmasligi kerak.')); return }
    const reader = new FileReader()
    reader.onload = () => { setSource({ dataUrl: reader.result, name: f.name }); setError('') }
    reader.readAsDataURL(f)
  }

  async function create(e) {
    e.preventDefault()
    const text = prompt.trim()
    if (!text || busy || !current) return
    const id = crypto.randomUUID?.() || String(Date.now())
    setItems((list) => [{ id, prompt: text, model: current.display_name || current.id, status: 'running',
      started: Date.now(), edited: editing }, ...list])
    setPrompt('')
    const patch = (p) => setItems((list) => list.map((i) => (i.id === id ? { ...i, ...p } : i)))
    try {
      const body = { model: current.id, prompt: text, size }
      if (editing) body.image = source.dataUrl
      const res = await fetch('/portal/playground/images', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-SF-CSRF': '1' },
        body: JSON.stringify(body),
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
  const price = imagePrice(current)
  return (
    <div className="vs">
      <form className="vs-form" onSubmit={create}>
        <label className="pg-field">
          <span>{tr('Model')}</span>
          <select className="input" value={current?.id} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.display_name || m.id}{resolution(m) ? ` · ${resolution(m)}` : ''}</option>
            ))}
          </select>
          {current && <small className="vs-hint">{modelDescription(current)}</small>}
        </label>
        <label className="pg-field">
          <span>{source ? tr('Nimani o‘zgartirish kerak') : tr('Tasvir')}</span>
          <textarea className="input" rows={5} maxLength={4000} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={source ? tr('Masalan: osmonni qizil qil, odamni olib tashla.')
              : tr('Masalan: tog‘ etagidagi yog‘och uy, tong tumanida, realistik surat.')} />
        </label>
        <div className="pg-field">
          <span>{tr('Asl rasm (ixtiyoriy)')}</span>
          {source
            ? <div className="vs-thumb"><img src={source.dataUrl} alt="" />
                <button type="button" className="btn btn--small btn--ghost" onClick={() => setSource(null)}>{tr('Olib tashlash')}</button></div>
            : <button type="button" className="btn btn--small" onClick={() => fileRef.current?.click()}>{tr('Rasm tanlash')}</button>}
          <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pickImage} />
          {source && !canEdit(current) && <small className="vs-hint">{tr('Bu model rasmni tahrirlay olmaydi — boshqa modelni tanlang.')}</small>}
        </div>
        <div className="pg-field">
          <span>{tr('Format')}</span>
          <div className="vs-seg vs-seg--grid" role="radiogroup" aria-label={tr('Format')}>
            {FORMATS.map(([v, label]) => (
              <button key={v} type="button" role="radio" aria-checked={size === v} className={size === v ? 'is-on' : ''}
                onClick={() => setSize(v)}>{label}</button>
            ))}
          </div>
        </div>
        {error && <div className="pg-error" role="alert">{error}</div>}
        <button type="submit" className="btn btn--primary" disabled={busy || !prompt.trim() || (source && !canEdit(current))}>
          {busy ? tr('Chizilmoqda…')
            : <>{editing ? tr('Rasmni o‘zgartirish') : tr('Rasm yaratish')}{price > 0 && <> · {fmtSom(price)}</>}</>}
        </button>
        <small className="dim">{tr('Rasm odatda 15–30 soniyada tayyor bo‘ladi. 1K — taxminan 1024 px, 2K — 2048 px. Narx bitta rasm uchun; rasm chiqmasa pul yechilmaydi.')}</small>
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
                  {i.src && <a href={i.src} download={`image-${i.id.slice(0, 8)}.${i.src.startsWith('data:image/jpeg') ? 'jpg' : 'png'}`}>{tr('Yuklab olish')}</a>}
                </div>
              </div>
            </article>
          ))}
      </section>
    </div>
  )
}
