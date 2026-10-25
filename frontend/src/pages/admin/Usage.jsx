import { IconChart, IconLayers, IconCoins, IconClock, IconActivity } from '../../components/icons.jsx'
import { useEffect, useState } from 'react'
import { api, qs } from '../../api.js'
import { ErrorNote, Loading, Pager, StatCard, useLoad } from '../../components/ui.jsx'
import RequestAttribution from '../../components/RequestAttribution.jsx'
import UsageAnalytics from '../../components/UsageAnalytics.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../../format.js'
import { translateText as tr, useLang } from '../../i18n/index.jsx'

const LIMIT = 50
export default function AdminUsage() {
  useLang()
  const [days, setDays] = useState(7)
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [failed, setFailed] = useState(false)
  const [client, setClient] = useState('')
  const [project, setProject] = useState('')
  const [tool, setTool] = useState('')
  const [keyId, setKeyId] = useState('')
  useEffect(() => {
    const id = setTimeout(() => { setQ(search); setOffset(0) }, 300)
    return () => clearTimeout(id)
  }, [search])
  const { data, error, loading, reload } = useLoad(() => api.get('/portal/admin/usage' + qs({ days, offset, limit: LIMIT, q, failed, client, project, tool, key_id: keyId })),
    [days, offset, q, failed, client, project, tool, keyId], { refreshMs: 30_000 })
  const totals = data?.totals
  const filter = (setter) => (e) => { setter(e.target.value); setOffset(0) }
  return <>
    <div className="page-head"><div><h1>{tr('So‘rovlar va tokenlar')}</h1><p>{tr('Barcha foydalanuvchilar, loyihalar va vositalar bo‘yicha sarf. Matnlar va vosita argumentlari saqlanmaydi.')}</p></div>
      <button className="btn" onClick={reload}>{tr('Yangilash')}</button></div>
    <ErrorNote error={error} retry={reload} />
    {totals && <div className="metric-grid request-stats">
      <StatCard icon={<IconChart />} label={tr('So‘rovlar')} value={fmtInt(totals.requests)} />
      <StatCard icon={<IconLayers />} label={tr('Tokenlar')} value={fmtInt(totals.prompt_tokens + totals.completion_tokens)} />
      <StatCard icon={<IconCoins />} label={tr('Jami sarf')} value={fmtSom(totals.cost)} />
      <StatCard icon={<IconActivity />} label={tr('Xatolar')} value={fmtInt(totals.errors)} />
      <StatCard icon={<IconClock />} label={tr('Javob vaqti p95')} value={totals.p95_ms == null ? '—' : `${(totals.p95_ms / 1000).toFixed(1)} s`} />
    </div>}
    <UsageAnalytics admin keyId={keyId} onKeyChange={setKeyId} />
    <div className="filter-bar block">
      <input className="input" value={search} onChange={e => setSearch(e.target.value)} placeholder={tr('Email, kalit, model yoki so‘rov ID')} aria-label={tr('So‘rov qidirish')} />
      <select className="input" value={days} onChange={e => { setDays(Number(e.target.value)); setOffset(0) }} aria-label={tr('Davr')}>
        {[1, 7, 30, 90].map(n => <option key={n} value={n}>{n} {tr('kun')}</option>)}
      </select>
      <select className="input" value={client} onChange={filter(setClient)} aria-label={tr('Mijoz dasturi')}>
        <option value="">{tr('Barcha mijozlar')}</option>
        {['opencode', 'codex', 'claude', 'cursor', 'continue', 'openai-python', 'openai-node', 'curl', 'other'].map(c => <option key={c}>{c}</option>)}
      </select>
      <input className="input" value={project} onChange={filter(setProject)} placeholder={tr('Loyiha nomi')} aria-label={tr('Loyiha nomi')} />
      <input className="input" value={tool} onChange={filter(setTool)} placeholder={tr('Vosita nomi')} aria-label={tr('Vosita nomi')} />
      <label className="check"><input type="checkbox" checked={failed} onChange={e => { setFailed(e.target.checked); setOffset(0) }} />{tr('Faqat muammolar')}</label>
    </div>
    <p className="hint">{tr('Loyiha va maqsad X-SF-Project hamda X-SF-Purpose sarlavhalarida beriladi. Vosita yonidagi tokenlar butun so‘rovga tegishli; alohida vosita narxi emas.')}</p>
    {loading && !data && <Loading rows={5} />}
    {data && <section className="panel"><div className="table-wrap"><table className="table table--requests">
      <thead><tr>{[tr("Sana"), tr("Foydalanuvchi"), tr("Model"), tr("Tokenlar"), tr("Sarf"), tr("Holat"), tr("Tafsilotlar")].map(s => <th key={s}>{tr(s)}</th>)}</tr></thead>
      <tbody>{data.items.map(r => <tr key={r.id}>
        <td data-label={tr("Sana")} className="nowrap">{fmtDateTime(r.created_at)}</td><td data-label={tr("Foydalanuvchi")}>{r.email}<small className="stack dim">{r.key_name}</small></td>
        <td data-label={tr("Model")}><code>{r.model}</code></td><td data-label={tr("Tokenlar")}>{fmtInt(r.prompt_tokens)} ↑ / {fmtInt(r.completion_tokens)} ↓{r.estimated && <small className="stack dim">{tr('Taxminiy')}</small>}</td>
        <td data-label={tr("Sarf")}>{fmtSom(r.cost)}</td><td data-label={tr("Holat")}><span className={`tag ${r.status_code >= 400 || r.error ? 'tag--bad' : 'tag--ok'}`}>{r.status_code}</span><small className="stack dim">{r.error}</small></td>
        <td className="request-details-cell"><RequestAttribution item={r} admin /></td>
      </tr>)}</tbody>
    </table></div>{data.items.length === 0 && <p className="empty">{tr('Mos so‘rov topilmadi')}</p>}
      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </section>}
  </>
}
