import { translateText as tr, useLang as useLocaleRefresh, modelDescription } from '../i18n/index.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { removeImage, startImage, useImageItems } from '../imageStore.js'
import { fmtSom } from '../format.js'
import { IconImage, IconSliders } from './icons.jsx'

/**
 * Playground image tab: POST /portal/playground/images (the same pipeline and billing as
 * /v1/images/generations, through the session and the hidden playground key). With an uploaded
 * picture the request becomes an edit (/v1/images/edits, img2img). The request and the results
 * live in imageStore.js, so leaving the page neither drops a picture being drawn nor the finished ones.
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

export default function ImageStudio({ models, userId, onCharged }) {
  useLocaleRefresh()
  const [model, setModel] = useState(models[0]?.id || '')
  const [prompt, setPrompt] = useState('')
  const [size, setSize] = useState('1024x1024')
  const [source, setSource] = useState(null)      // { dataUrl, name } — the picture to change (img2img)
  const [error, setError] = useState('')
  const items = useImageItems(userId, onCharged)   // { id, prompt, model, status, src?, error?, cost?, started, edited }
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

  function create(e) {
    e.preventDefault()
    const text = prompt.trim()
    if (!text || busy || !current) return
    const body = { model: current.id, prompt: text, size }
    if (editing) body.image = source.dataUrl
    setPrompt('')
    startImage({ userId, body, modelName: current.display_name || current.id, edited: editing })
  }

  if (!models.length) return <div className="vs-empty">{tr('Hozircha rasm modeli yo‘q.')}</div>
  const price = imagePrice(current)
  return (
    <div className="vs">
      <form className="vs-form" onSubmit={create}>
        <div className="vs-head"><span className="vs-head__tile" aria-hidden="true"><IconSliders /></span><b>{tr('Rasm sozlamalari')}</b></div>
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
        <div className="vs-head"><span className="vs-head__tile" aria-hidden="true"><IconImage /></span><b>{tr('Rasmlar')}</b>{items.length > 0 && <small>{items.length}</small>}</div>
        {items.length === 0
          ? <div className="vs-empty"><span className="pg-hero" aria-hidden="true"><IconImage /></span>{tr('Tasvir yozing — tayyor rasm shu yerda paydo bo‘ladi.')}</div>
          : items.map((i) => (
            <article key={i.id} className={`vs-card vs-card--${i.status}`}>
              {i.status === 'done' && i.src
                ? <img className="is-image" src={i.src} alt={i.prompt} />
                : <div className="vs-stage is-square">
                    {i.status === 'failed'
                      ? (i.interrupted
                          ? <p className="vs-error" role="alert">{tr('Sahifa yangilangani uchun natija olinmadi. Pul yechilgan bo‘lsa, rasm Sarf sahifasida ko‘rinadi.')}</p>
                          : <p className="vs-error" role="alert">{i.network ? tr('Tarmoq xatosi. Qayta urinib ko‘ring.') : i.error} {tr('Pul yechilmadi.')}
                              {i.code === 402 && <> <Link to="/app/billing">{tr('Hisobni to‘ldirish')}</Link></>}</p>)
                      : <div className="vs-rendering" role="status"><span className="vs-bar" aria-hidden="true" />
                          <span>{tr('Chizilmoqda')} <Elapsed since={i.started} /></span></div>}
                  </div>}
              <div className="vs-meta">
                <p className="vs-prompt">{i.prompt}</p>
                <div className="vs-row">
                  <span>{i.model}{i.cost != null && <> · {fmtSom(i.cost)}</>}</span>
                  <span className="vs-actions">
                    {i.src && <a href={i.src} download={`image-${i.id.slice(0, 8)}.${i.src.startsWith('data:image/jpeg') ? 'jpg' : 'png'}`}>{tr('Yuklab olish')}</a>}
                    {i.status !== 'running' && <button type="button" className="link-btn" onClick={() => removeImage(i.id)}>{tr("O'chirish")}</button>}
                  </span>
                </div>
              </div>
            </article>
          ))}
      </section>
    </div>
  )
}
