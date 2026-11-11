import { translateText as tr, useLang as useLocaleRefresh, modelDescription } from '../i18n/index.jsx'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { ErrorNote, Loading, PageHead, useLoad } from '../components/ui.jsx'
import SpeedFigure from '../components/SpeedFigure.jsx'
import { IconSearch } from '../components/icons.jsx'
import { MICRO, fmtNumber } from '../format.js'

// Short hints: which model to pick when.
// Labels for model capabilities. Order matters: the most requested come first.
// Capabilities shown to the user. Internal details
// ("answer is verified", "third party") are DELIBERATELY absent here:
// they reveal how the system is built and don't help with choosing.
// The privacy note is given separately, in plain language, on the model page.
const CAP_TAGS = [
  ['vision', 'Rasmni ko\'radi'],
  ['file_input', 'Fayl o\'qiydi'],
  ['image_generation', 'Rasm yaratadi'],
  ['image_editing', 'Rasmni tahrirlaydi'],
  ['tool_calling', 'Agent rejimi'],
]

// The short label is derived from model data (not tied to the id, since models differ
// per deployment). The model's own description is shown as the hint.
const LONG_CONTEXT = 100_000
function hintOf(m) {
  const c = m.capabilities || {}
  let tag = ''
  if (!m.price_in_per_1m && !m.price_out_per_1m) tag = tr("Bepul")
  else if (c.conditional_verification) tag = 'Tekshiruvli'
  else if (c.image_generation) tag = tr("Rasm yaratadi")
  else if (c.tool_calling) tag = tr("Agent rejimi")
  else if ((m.context_window || 0) >= LONG_CONTEXT) tag = tr("Uzun kontekst")
  else if (c.reasoning_levels?.length) tag = 'Fikrlaydi'
  return { tag, hint: m.description || '' }
}


const SORTS = [
  ['default', 'Tavsiya etilgan'],
  ['cheap', 'Avval arzoni'],
  ['fast', 'Avval tezi'],
]

export default function Catalog() {
  useLocaleRefresh()
  const { freeMode } = useAuth()
  // Speeds are live (median of recent requests); refresh so a slowdown shows up
  const { data, error, loading, reload } = useLoad(() => api.get('/portal/models'), [], { refreshMs: 60_000 })
  const bench = useLoad(() => api.get('/portal/benchmark'), [])
  const all = data?.items || []
  const limits = data?.limits
  const [q, setQ] = useState('')
  const [tag, setTag] = useState('')
  const [sort, setSort] = useState('default')

  const tags = useMemo(() => [...new Set(all.map((m) => hintOf(m).tag).filter(Boolean))], [all])

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    let out = all.filter((m) => {
      const h = hintOf(m)
      if (tag && h.tag !== tag) return false
      if (!needle) return true
      return [m.display_name, m.id, m.description, m.description_ru, m.description_en, h.hint].filter(Boolean).some((t) => t.toLowerCase().includes(needle))
    })
    if (sort === 'cheap') out = [...out].sort((a, b) => (a.price_in_per_1m + a.price_out_per_1m) - (b.price_in_per_1m + b.price_out_per_1m))
    if (sort === 'fast') out = [...out].sort((a, b) => (a.measured?.typical_latency_ms ?? 1e9) - (b.measured?.typical_latency_ms ?? 1e9))
    return out
  }, [all, q, tag, sort])

  return (
    <>
      <PageHead
        eyebrow={tr("API katalogi")}
        title={[tr("Modellar"), tr("va narxlar")]}
        lead={tr("Barcha mavjud modellar va ularning narxlari. Loyihangiz uchun eng mos modelni tanlang.")}
        action={
          <div className="note-card">
            <div>
              <b>{tr("Bitta API — ko'p imkoniyat")}</b>
              <p>{tr("Matn, kod, tahlil va ko'p bosqichli vazifalar uchun zamonaviy modellar.")}</p>
            </div>
          </div>
        }
      />
      <ErrorNote error={error} retry={reload} />
      {loading && !data && <Loading rows={4} />}

      {all.length > 0 && (
        <div className="filter-bar">
          <label className="search-field">
            <IconSearch />
            <input className="input" value={q} onChange={(e) => setQ(e.target.value)}
                   placeholder={tr("Model nomi yoki tavsif bo'yicha qidirish...")} aria-label={tr("Modellarni qidirish")} />
          </label>
          <div className="chips" style={{ marginBottom: 0 }}>
            <button className={`chip ${tag === '' ? 'is-on' : ''}`} onClick={() => setTag('')}>{tr("Barchasi")}</button>
            {tags.map((t) => <button key={t} className={`chip ${tag === t ? 'is-on' : ''}`} onClick={() => setTag(t)}>{tr(t)}</button>)}
          </div>
          <select className="input" style={{ width: 'auto' }} value={sort} onChange={(e) => setSort(e.target.value)} aria-label={tr("Saralash")}>
            {SORTS.map(([v, l]) => <option key={v} value={v}>{tr(l)}</option>)}
          </select>
        </div>
      )}

      {all.length > 0 && list.length === 0 && (
        <div className="empty"><b>{tr("Mos model topilmadi")}</b><span>{tr("Qidiruv so'zini yoki filtrni o'zgartiring.")}</span></div>
      )}

      {list.length > 0 && (
        <div className="price-list">
          {list.map((m) => {
            const h = hintOf(m)
            const b = (bench.data?.items || []).find((x) => x.id === m.id)
            const free = freeMode || (m.price_in_per_1m === 0 && m.price_out_per_1m === 0)
            return (
              <Link className="price-row price-row--link" key={m.id} to={`/app/catalog/${m.id}`}>
                <div style={{ minWidth: 0 }}>
                  <div className="hstack" style={{ gap: 9, marginBottom: 3 }}>
                    <span className="price-model">{tr(m.display_name)}</span>
                    {h.tag && <span className="tag tag--ok">{tr(h.tag)}</span>}
                    {m.maintenance && <span className="tag tag--warn">{tr("Texnik ishlar")}</span>}
                  </div>
                  <code>{m.id}</code>
                  <p className="muted small" style={{ marginTop: 7, maxWidth: '30rem' }}>{h.hint === m.description ? modelDescription(m) : tr(h.hint)}</p>
                  <div className="hstack" style={{ gap: 7, marginTop: 9 }}>
                    {CAP_TAGS.filter(([k, label]) => m.capabilities?.[k] && tr(label) !== h.tag).map(([k, label]) => <span className="tag" key={k}>{tr(label)}</span>)}
                    {m.capabilities?.reasoning_levels?.length > 0 && <span className="tag">{tr("Fikrlash:")} {m.capabilities.reasoning_levels.join(', ')}</span>}
                  </div>
                </div>
                <div className="model-metrics">
                  <div className="mm">
                    <span className="mm-top">{tr("Kontekst")}</span>
                    <b>{fmtNumber(m.context_window)}</b><span className="sub">{tr("token")}</span>
                  </div>
                  <div className="mm">
                    <span className="mm-top">{tr("Tezlik")}</span>
                    <SpeedFigure measured={m.measured} />
                  </div>
                  <div className="mm">
                    <span className="mm-top">{tr("Sinov natijasi")}</span>
                    <b>{tr(b ? `${Math.round(b.ratio * 100)}%` : '—')}</b><span className="sub">{tr("(bizning sinov)")}</span>
                  </div>
                </div>
                {free
                  ? <div className="price-figs"><div><div><b>{tr("Bepul")}</b><span>{tr(freeMode ? tr("hamkorlar uchun") : tr("limit bilan"))}</span></div></div></div>
                  : (
                    <div className="price-figs">
                      <span className="price-figs__cap">{tr("Narx (1M token uchun)")}</span>
                      <div>
                        <div><b>{fmtNumber(m.price_in_per_1m / MICRO)} {tr("so'm")}</b><span>{tr("Kiruvchi tokenlar")}</span></div>
                      </div>
                      <div>
                        <div><b>{fmtNumber(m.price_out_per_1m / MICRO)} {tr("so'm")}</b><span>{tr("Chiquvchi tokenlar")}</span></div>
                      </div>
                    </div>
                  )}
                <span className="price-row__go" aria-hidden="true">&rsaquo;</span>
              </Link>
            )
          })}
        </div>
      )}

      <div className="block">
        <div className="block-head"><h2>{tr("Narx qanday hisoblanadi")}</h2></div>
        <div className="panel price-explain">
          <ul className="price-rules">
            <li>{tr("Narx =")} <b>{tr("kirish tokenlar")}</b> {tr("× kirish narxi +")} <b>{tr("chiqish tokenlar")}</b> {tr("× chiqish narxi.")}</li>
            <li>{tr("So'rov boshida eng ko'p sarflanishi mumkin summa vaqtincha band qilinadi, javobdan keyin haqiqiy summa yechiladi.")}</li>
            <li>{tr("Model javob bermasa yoki xato qaytarsa, pul yechilmaydi.")}</li>
            <li>{tr("Ba'zi modellarda so'rov mazmuni javob tayyorlash uchun hamkor xizmatga ham yuboriladi. Matnlar bizda saqlanmaydi; qaysi modelda shundayligi model sahifasida yozilgan.")}</li>
            {limits && <li>{tr("Standart chegara: daqiqasiga")} {tr(limits.default_rpm)} {tr("ta so'rov, bir javobda ko'pi bilan")} {fmtNumber(limits.max_max_tokens)} {tr("token.")}</li>}
          </ul>
          {/* One average request, printed as a receipt: what each model would charge for it */}
          <div className="receipt price-explain__receipt">
            <div className="receipt-head"><b>{tr("Bitta so‘rov narxi")}</b><span>{tr("300 kirish + 500 chiqish token")}</span></div>
            {all.map((m) => {
              const avg = (300 * (m.price_in_per_1m / MICRO) + 500 * (m.price_out_per_1m / MICRO)) / 1_000_000
              return <div className="rline" key={m.id}><span>{m.id}</span><span>{tr(avg === 0 ? tr("bepul") : `~${avg.toFixed(0)} ${tr("so'm")}`)}</span></div>
            })}
          </div>
        </div>
      </div>
    </>
  )
}
