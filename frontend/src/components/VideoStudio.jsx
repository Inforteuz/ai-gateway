import { translateText as tr, useLang as useLocaleRefresh, modelDescription } from '../i18n/index.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { fmtSom } from '../format.js'

/**
 * Playground video tab: the same jobs as POST /v1/videos, through the portal session
 * (routes_playground.py) and paid from the user's balance. A clip renders in about a
 * minute (Pro takes longer: the frame rate is raised afterwards), so each job is a card
 * that polls its status and turns into a player when the clip is ready.
 */
const MAX_IMAGE = 10 * 1024 * 1024
const POLL_MS = 4000
const KEEP = 12

function loadJobs(key) {
  try { return JSON.parse(localStorage.getItem(key) || '[]').slice(0, KEEP) } catch { return [] }
}
function saveJobs(key, jobs) {
  try { localStorage.setItem(key, JSON.stringify(jobs.slice(0, KEEP).map(({ preview, ...j }) => j))) } catch { /* storage blocked */ }
}

function Elapsed({ since }) {
  const [, tick] = useState(0)
  useEffect(() => { const id = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(id) }, [])
  const s = Math.max(0, Math.round(Date.now() / 1000 - since))
  return <b>{Math.floor(s / 60)}:{String(s % 60).padStart(2, '0')}</b>
}

function JobCard({ job }) {
  useLocaleRefresh()
  const src = `/portal/playground/videos/${job.id}/content`
  return (
    <article className={`vs-card vs-card--${job.status}`}>
      {job.status === 'completed' ? (
        <video className="vs-video" src={src} controls playsInline loop preload="metadata" />
      ) : (
        <div className={`vs-stage ${job.size === '720x1280' ? 'is-portrait' : ''}`}>
          {job.status === 'failed'
            ? <p className="vs-error" role="alert">{tr('Video yaratilmadi.')} {tr('Pul yechilmadi.')}</p>
            : <div className="vs-rendering" role="status">
                <span className="vs-bar" aria-hidden="true" />
                <span>{job.status === 'queued' ? tr('Navbatda') : tr('Tayyorlanmoqda')} <Elapsed since={job.created_at} /></span>
                <small>{tr('O‘rtacha 1 daqiqa')}</small>
              </div>}
        </div>
      )}
      <div className="vs-meta">
        <p className="vs-prompt">{job.prompt}</p>
        <div className="vs-row">
          <span>{job.model}</span>
          {job.status === 'completed' && <a href={src} download={`${job.id}.mp4`}>{tr('Yuklab olish')}</a>}
        </div>
      </div>
    </article>
  )
}

export default function VideoStudio({ models, userId, onCharged }) {
  useLocaleRefresh()
  const storeKey = `pg-video:${userId || 'anon'}`
  const [model, setModel] = useState(models[0]?.id || '')
  const [prompt, setPrompt] = useState('')
  const [size, setSize] = useState('1280x720')
  const [image, setImage] = useState(null)        // { dataUrl, name }
  const [jobs, setJobs] = useState(() => loadJobs(storeKey))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef(null)
  const current = models.find((m) => m.id === model) || models[0]

  useEffect(() => { saveJobs(storeKey, jobs) }, [jobs, storeKey])
  useEffect(() => { if (!models.some((m) => m.id === model) && models[0]) setModel(models[0].id) }, [models, model])

  // Poll every unfinished job; stop when none is left
  const active = jobs.filter((j) => j.status === 'queued' || j.status === 'in_progress').map((j) => j.id).join(',')
  useEffect(() => {
    if (!active) return undefined
    const id = setInterval(async () => {
      for (const vid of active.split(',')) {
        try {
          const r = await api.get(`/portal/playground/videos/${vid}`)
          setJobs((list) => list.map((j) => (j.id === vid ? { ...j, status: r.status, error: r.error?.message || null } : j)))
          if (r.status === 'completed' || r.status === 'failed') onCharged?.()
        } catch { /* a missed poll is retried */ }
      }
    }, POLL_MS)
    return () => clearInterval(id)
  }, [active, onCharged])

  function pickImage(e) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { setError(tr('Faqat PNG, JPEG yoki WebP rasm.')); return }
    if (f.size > MAX_IMAGE) { setError(tr('Rasm 10 MB dan oshmasligi kerak.')); return }
    const reader = new FileReader()
    reader.onload = () => { setImage({ dataUrl: reader.result, name: f.name }); setError('') }
    reader.readAsDataURL(f)
  }

  async function create(e) {
    e.preventDefault()
    if (!prompt.trim() || busy || !current) return
    setBusy(true)
    setError('')
    try {
      const body = { model: current.id, prompt: prompt.trim(), size }
      if (image) body.input_reference = image.dataUrl
      const r = await api.post('/portal/playground/videos', body)
      setJobs((list) => [{ id: r.id, status: r.status, prompt: prompt.trim(), model: current.display_name || current.id,
        size, created_at: r.created_at || Math.round(Date.now() / 1000) }, ...list].slice(0, KEEP))
      setPrompt('')
      onCharged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!models.length) {
    return <div className="vs-empty">{tr('Hozircha video modeli yo‘q.')}</div>
  }
  const price = current ? current.price_out_per_1m : 0
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
            placeholder={tr('Masalan: quyosh botayotgan sohilda to‘lqinlar, kamera sekin chapga suriladi.')} />
        </label>
        <div className="pg-field">
          <span>{tr('Format')}</span>
          <div className="vs-seg" role="radiogroup" aria-label={tr('Format')}>
            {[['1280x720', '16:9'], ['720x1280', '9:16']].map(([v, label]) => (
              <button key={v} type="button" role="radio" aria-checked={size === v} className={size === v ? 'is-on' : ''}
                onClick={() => setSize(v)}>{label}</button>
            ))}
          </div>
        </div>
        <div className="pg-field">
          <span>{tr('Birinchi kadr (ixtiyoriy)')}</span>
          {image
            ? <div className="vs-thumb"><img src={image.dataUrl} alt="" /><button type="button" className="btn btn--small btn--ghost" onClick={() => setImage(null)}>{tr('Olib tashlash')}</button></div>
            : <button type="button" className="btn btn--small" onClick={() => fileRef.current?.click()}>{tr('Rasm tanlash')}</button>}
          <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pickImage} />
        </div>
        {error && <div className="pg-error" role="alert">{error}{/402|mablag|balance/i.test(error) && <> <Link to="/app/billing">{tr('Hisobni to‘ldirish')}</Link></>}</div>}
        <button type="submit" className="btn btn--primary" disabled={busy || !prompt.trim()}>
          {busy ? tr('Yuborilmoqda…') : <>{tr('Video yaratish')} · {fmtSom(price)}</>}
        </button>
        <small className="dim">{tr('8 soniyalik klip, o‘rtacha 1 daqiqada tayyor bo‘ladi (Pro biroz uzoqroq). Narx bitta klip uchun; video chiqmasa pul yechilmaydi.')}</small>
      </form>
      <section className="vs-list" aria-label={tr('Videolar')}>
        {jobs.length === 0
          ? <div className="vs-empty">{tr('Tasvir yozing — tayyor klip shu yerda paydo bo‘ladi.')}</div>
          : jobs.map((j) => <JobCard key={j.id} job={j} />)}
      </section>
    </div>
  )
}
